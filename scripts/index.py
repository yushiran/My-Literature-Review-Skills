# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Write INDEX.md for a topic from manifest.json.

Lists every paper past selection (selected, pdf, no-pdf, md, failed) as a
table plus one abstract block each. Selected/no-pdf/failed papers and any
local-text conversion are called out in a header block and, in the dump,
an inline marker. The header also counts the `found` papers nothing ever
triaged, the one state no other listing reaches. A reading guide sits between
<!-- guide --> markers at the top: --guide replaces it, otherwise the block
already in INDEX.md is kept, otherwise a placeholder. --dump-abstracts prints
the questions and abstracts for the librarian and writes nothing;
--new-only then restricts the dump to papers found since manifest.refreshed,
and exits 4 with a reason when that leaves nothing.
Contract: references/workflow.md.
"""
import datetime as _dt
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as M  # noqa: E402

LISTED_STATES = ("selected", "pdf", "no-pdf", "md", "failed")
UNREAD_STATES = ("selected", "no-pdf", "failed")  # statuses whose guide entry rests on the abstract only
GUIDE_OPEN, GUIDE_CLOSE = "<!-- guide -->", "<!-- /guide -->"
NO_GUIDE = "_No reading guide yet._"
GUIDE_BLOCK = re.compile(re.escape(GUIDE_OPEN) + r"\n(.*?)\n?" + re.escape(GUIDE_CLOSE), re.S)
MAX_AUTHORS = 3
# Lines index.py itself writes into the guide block. They are stripped before every render and
# recomputed from the manifest as it stands, so a count in INDEX.md is never a stale copy.
SCOPE_TAG = "_Guide scope:"
CHECKED_TAG = "checked:"
TERMS_LINE = re.compile(r"^\s*terms:\s*(.+?)\s*$", re.I)   # the librarian ends each gap with one
ID_RE = re.compile(r"(?<![\w-])(?:(?:19|20)\d{2}|nd)-[a-z][a-z0-9]*-[a-z0-9][a-z0-9-]*")
MAX_HITS = 12                                              # ids shown on one checked: line
STATE_ORDER = {s: i for i, s in enumerate(("md", "pdf", "no-pdf", "selected", "failed", "found", "rejected"))}


def sort_key(p: dict):
    year = p.get("year")
    # Unknown year sorts last.
    return (year is None, -(year or 0), int(p.get("venue_tier") or 3), -int(p.get("citations") or 0), p["id"])


def cell(text) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def existing_guide(index_path: Path) -> str:
    """Guide block content of the current INDEX.md, or '' if absent or placeholder."""
    if not index_path.is_file():
        return ""
    m = GUIDE_BLOCK.search(index_path.read_text())
    if not m:
        return ""
    # old: body = m.group(1).strip()
    body = strip_generated(m.group(1))   # scope and checked: lines are recomputed on every render
    return "" if body == NO_GUIDE else body


def authors_line(authors: list) -> str:
    names = [a for a in (authors or []) if a]
    if not names:
        return "(no authors)"
    head = ", ".join(names[:MAX_AUTHORS])
    return head + (" et al." if len(names) > MAX_AUTHORS else "")


def files_cell(p: dict, tdir: Path) -> str:
    links = []
    pdf_rel = p.get("pdf") or f"pdf/{p['id']}.pdf"
    md_rel = p.get("md") or f"md/{p['id']}/{p['id']}.md"
    if (tdir / pdf_rel).is_file():
        links.append(f"[pdf]({pdf_rel})")
    if (tdir / md_rel).is_file():
        links.append(f"[md]({md_rel})")
    return " ".join(links) or "—"


def questions_lines(manifest: dict) -> list:
    # old: qs = [q for q in (manifest.get("questions") or []) if q]
    qs = [q for q in M.as_list(manifest.get("questions")) if q]   # hand-edited field: a bare string is one question, not N characters
    if not qs:
        return []
    return ["Research questions:"] + [f"{i}. {q}" for i, q in enumerate(qs, 1)] + [""]


def unread_lines(papers: list) -> list:
    """Header lines naming which listed papers the guide can only draw from an abstract."""
    unread = [p for p in papers if p.get("status") in UNREAD_STATES]
    textonly = [p["id"] for p in papers if p.get("conversion") == "local-text"]
    lines = []
    if unread:
        lines.append(f"Selected but unread ({len(unread)}): the guide below rests on their abstracts only.")
        for p in unread:
            # .get with fallback: an UNREAD_STATES entry missing here must not KeyError and lose INDEX.md
            reason = p.get("error") or {"selected": "not fetched yet", "no-pdf": "no open-access copy",
                                        "failed": "conversion failed"}.get(p["status"], "reason unknown")
            lines.append(f"- {p['id']} — {reason}")
        lines.append("")
    if textonly:
        lines.append("Text only (no figures or tables): " + ", ".join(textonly))
        lines.append("")
    return lines


def marker(p: dict) -> str:
    """Bracket tag for a dump entry: unread status takes priority over text-only."""
    if p.get("status") in UNREAD_STATES:
        return f" [unread: {p['status']}]"
    if p.get("conversion") == "local-text":
        return " [text-only]"
    return ""


def strip_generated(guide: str) -> str:
    """The guide without the lines index.py added to it, so a re-render recomputes them."""
    keep = [l for l in guide.splitlines()
            if not (l.startswith(SCOPE_TAG) or l.lstrip().startswith(CHECKED_TAG))]
    return "\n".join(keep).strip()


def guide_ids(text: str) -> list:
    """Every paper id the text names, in id form (year-surname-slug), deduplicated and sorted."""
    return sorted({m.group(0).rstrip("-") for m in ID_RE.finditer(text)})


def check_ids(manifest: dict, text: str, listed: set) -> tuple:
    """(ids cited, ids not in the manifest, ids in it but outside the set the guide was given)."""
    ids = guide_ids(text)
    unknown = [i for i in ids if i not in manifest["papers"]]
    outside = [i for i in ids if i in manifest["papers"] and i not in listed]
    return ids, unknown, outside


def _norm(text) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def parse_terms(spec: str) -> list:
    """'perception-distortion; frontier|tradeoff' -> [['perception distortion'], ['frontier', 'tradeoff']]:
    a paper must carry every group, and one alternative of a group is enough. A trailing `*`
    survives normalisation and marks a prefix (characteri* takes characterise and characterization)."""
    groups = []
    for phrase in spec.split(";"):
        alts = []
        for raw in phrase.split("|"):
            alt = _norm(raw)
            if alt:
                alts.append(alt + "*" if raw.strip().endswith("*") else alt)
        if alts:
            groups.append(alts)
    return groups


def term_pattern(alt: str) -> str:
    """One alternative as a regex over normalised text. Tokens may sit fused, spaced or hyphenated
    in the paper (tradeoff, trade off, trade-off all match trade-off); a plain term must end at a
    word boundary, plural allowed, so `fid` does not take every paper that says fidelity; `*` lifts
    the boundary."""
    prefix = alt.endswith("*")
    tokens = alt.rstrip("*").split()
    return r"\b" + r" ?".join(re.escape(t) for t in tokens) + ("" if prefix else r"(?:s|es)?\b")


def corpus(manifest: dict) -> dict:
    """id -> normalised title plus abstract, for every paper in every state, built once."""
    return {pid: _norm(f"{p.get('title') or ''} {p.get('abstract') or ''}")
            for pid, p in manifest["papers"].items()}


def matches(manifest: dict, groups: list, texts: dict = None) -> list:
    """Papers, in any state, whose title or abstract carries every term group. This is a screen
    for what the librarian never saw, so the reader will open every id it prints: whole words,
    not run-ons, or `fid` names half the library."""
    texts = texts if texts is not None else corpus(manifest)
    pats = [re.compile("(?:" + "|".join(term_pattern(a) for a in alts) + ")") for alts in groups]
    return [manifest["papers"][pid] for pid, text in texts.items() if all(p.search(text) for p in pats)]


def annotate_gaps(manifest: dict, guide: str, listed: set) -> tuple:
    """The guide with one checked: line under every terms: line, and how many terms lines it had.
    The search runs over every abstract in the manifest, the ones the librarian was not shown
    included, so a gap the guide claims is tested against the papers it could not have read."""
    texts = corpus(manifest)
    out, n_terms = [], 0
    for line in guide.splitlines():
        out.append(line)
        m = TERMS_LINE.match(line)
        if not m:
            continue
        groups = parse_terms(m.group(1))
        if not groups:
            out.append(f"{CHECKED_TAG} the terms line names nothing; nothing searched")
            continue
        n_terms += 1
        hits = matches(manifest, groups, texts)
        inside = sum(1 for p in hits if p["id"] in listed)
        outside = sorted((p for p in hits if p["id"] not in listed), key=sort_key)
        if outside:
            shown = ", ".join(f"{p['id']} ({p.get('status')})" for p in outside[:MAX_HITS])
            more = f", +{len(outside) - MAX_HITS} more" if len(outside) > MAX_HITS else ""
            tail = f"{len(outside)} outside it: {shown}{more}"
        else:
            tail = "none outside it"
        out.append(f"{CHECKED_TAG} {len(texts)} papers searched; {inside} in the guide's set carry "
                   f"these terms, {tail}")
    return "\n".join(out), n_terms


def scope_line(manifest: dict, papers: list) -> str:
    """What the guide was written from, and what it was not, in the manifest's own numbers."""
    n_found, n_rej = len(M.papers_in(manifest, "found")), len(M.papers_in(manifest, "rejected"))
    return (f"{SCOPE_TAG} written from the abstracts of the {len(papers)} papers past selection; "
            f"{n_found} found (never triaged) and {n_rej} rejected papers were not read for it, so "
            f"every gap below is a claim about those {len(papers)} only._")


def guide_block(manifest: dict, papers: list, guide: str) -> str:
    """The guide as INDEX.md carries it: the scope line on top, a checked: line under each terms: line."""
    if not guide:
        return NO_GUIDE
    text, n_terms = annotate_gaps(manifest, guide, {p["id"] for p in papers})
    if not n_terms:
        M.log("index: the guide has no `terms:` line, so no gap was searched against the library")
    return scope_line(manifest, papers) + "\n\n" + text


def coverage_lines(manifest: dict, dumped: list, note: str = "") -> list:
    """Header of the dump: what the librarian is shown and what it is not, as counts."""
    total = len(manifest["papers"])
    by = Counter(p.get("status") for p in manifest["papers"].values())
    in_dump = ", ".join(f"{n} {s}" for s, n in sorted(Counter(p.get("status") for p in dumped).items()))
    with_abs = sum(1 for p in manifest["papers"].values() if p.get("abstract"))
    unseen = total - len(dumped)
    return [f"Coverage: this dump holds {len(dumped)} of the {total} papers in the library "
            f"({in_dump}){note}.",
            f"Not shown: {by['found']} found (never triaged) and {by['rejected']} rejected; "
            f"{with_abs} of the {total} carry an abstract.",
            f"A gap is a claim about the abstracts below only. End each gap with a `terms:` line "
            f"and index.py searches the other {unseen} for it.", ""]


def grep_main(manifest: dict, spec: str) -> int:
    """--grep: every paper in any state that carries the terms, one line each; exit 4 on none."""
    groups = parse_terms(spec)
    if not groups:
        M.log("index: --grep needs at least one term")
        return M.EXIT_USAGE
    hits = sorted(matches(manifest, groups),
                  key=lambda p: (STATE_ORDER.get(p.get("status"), 9),) + sort_key(p))
    for p in hits:
        year = p.get("year") if p.get("year") is not None else "n.d."
        print(f"{p['id']}\t{p.get('status')}\t{year}\t{p.get('title') or '(no title)'}")
    by = Counter(p.get("status") for p in hits)
    tail = f" ({', '.join(f'{n} {s}' for s, n in sorted(by.items()))})" if hits else ""
    M.log(f"index: --grep matched {len(hits)} of {len(manifest['papers'])} papers{tail}")
    return M.EXIT_OK if hits else M.EXIT_NOTHING


def render_index(manifest: dict, papers: list, guide: str, tdir: Path) -> str:
    counts = {s: sum(1 for p in papers if p.get("status") == s) for s in LISTED_STATES}
    lines = [f"# {manifest['topic']} — literature index"]
    lines.append(
        f"Generated {_dt.date.today().isoformat()}. {len(papers)} papers: "
        f"{counts['md']} converted, {counts['pdf']} pdf only, {counts['no-pdf']} abstract only, "
        f"{counts['failed']} failed, {counts['selected']} pending."
    )
    lines.append("")
    # `found` is not a listed state, and after a refresh rank --new-only skips anything found
    # before the stamp, so this count is the only place an untriaged paper is ever mentioned.
    n_found = len(M.papers_in(manifest, "found"))
    if n_found:
        lines.extend([f"{n_found} papers found but never triaged.", ""])
    lines.extend(unread_lines(papers))
    lines.extend(questions_lines(manifest))
    # old: lines.extend([GUIDE_OPEN, guide or NO_GUIDE, GUIDE_CLOSE, ""])
    lines.extend([GUIDE_OPEN, guide_block(manifest, papers, guide), GUIDE_CLOSE, ""])

    lines.append("## Papers")
    lines.append("")
    lines.append("| id | year | venue (tier) | cites | status | files |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for p in papers:
        year = p.get("year") if p.get("year") is not None else "n.d."
        venue = f"{cell(p.get('venue') or '(no venue)')} ({int(p.get('venue_tier') or 3)})"
        lines.append(
            f"| {p['id']} | {year} | {venue} | {int(p.get('citations') or 0)} "
            f"| {p.get('status')} | {files_cell(p, tdir)} |"
        )
    lines.append("")

    lines.append("## Abstracts")
    lines.append("")
    for p in papers:
        year = p.get("year") if p.get("year") is not None else "n.d."
        meta = [
            authors_line(p.get("authors")),
            p.get("venue") or "(no venue)",
            str(year),
            f"{int(p.get('citations') or 0)} citations",
        ]
        if p.get("doi"):
            meta.append(f"DOI {p['doi']}")
        if p.get("arxiv"):
            meta.append(f"arXiv {p['arxiv']}")
        lines.append(f"### {p['id']}")
        lines.append(f"**{p.get('title') or '(no title)'}**")
        lines.append(" · ".join(meta))
        if p.get("why"):
            lines.append(f"why: {p['why']}")
        status = f"status: {p.get('status')}"
        if p.get("status") in ("failed", "no-pdf") and p.get("error"):
            status += f", error: {p['error']}"
        if p.get("conversion") == "local-text":
            status += ", text only (no figures or tables)"
        lines.append(status)
        lines.append("")
        lines.append(p.get("abstract") or "(no abstract)")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


# old: def render_dump(manifest: dict, papers: list) -> str:
# old:     lines = questions_lines(manifest)
def render_dump(manifest: dict, papers: list, head: list = ()) -> str:
    lines = questions_lines(manifest) + list(head)   # head: the coverage block, before any abstract
    for p in papers:
        year = p.get("year") if p.get("year") is not None else "n.d."
        # old: lines.append(f"### {p['id']}")   # reverting drops the [unread]/[text-only] tags,
        # old: so the librarian cannot tell which entries rest on an abstract alone
        lines.append(f"### {p['id']}{marker(p)}")
        lines.append(p.get("title") or "(no title)")
        lines.append(f"{p.get('venue') or '(no venue)'} · {year} · {int(p.get('citations') or 0)} citations")
        lines.append(p.get("abstract") or "(no abstract)")
        lines.append("")
    return "\n".join(lines) + "\n"


def atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".INDEX-", suffix=".md")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o666 & ~M.UMASK)  # mkstemp files start at 0600
    os.replace(tmp, path)


def main() -> int:
    parser = M.base_parser("Write INDEX.md from manifest.json, or dump abstracts for the librarian.")
    parser.add_argument("--guide", help="guide.md to fold into INDEX.md between the guide markers")
    parser.add_argument("--dump-abstracts", action="store_true",
                        help="print questions and abstracts to stdout and exit without writing")
    parser.add_argument("--new-only", action="store_true",
                        help="with --dump-abstracts: only papers found since manifest.refreshed")
    parser.add_argument("--ids", nargs="+", metavar="ID",
                        help="with --dump-abstracts: only these ids, whatever their status")
    parser.add_argument("--grep", metavar="TERMS",
                        help="print every paper, in any state, whose title or abstract carries the "
                             "terms ('a; b|c' = a and one of b, c) and exit without writing")
    args = parser.parse_args()
    if args.new_only and not args.dump_abstracts:
        M.log("index: --new-only applies to --dump-abstracts only, ignored")
    if args.ids and not args.dump_abstracts:
        M.log("index: --ids applies to --dump-abstracts only, ignored")

    guide_text = None
    if args.guide:
        guide_path = Path(args.guide)
        if not guide_path.is_file():
            M.log(f"index: guide file not found: {guide_path}")
            return M.EXIT_USAGE
        # old: guide_text = guide_path.read_text().strip()
        guide_text = strip_generated(guide_path.read_text())   # a pasted-back INDEX block re-folds cleanly

    manifest = M.load(args)
    tdir = M.topic_dir(args)
    if args.grep:
        return grep_main(manifest, args.grep)                  # any state, writes nothing
    if args.dump_abstracts and args.ids:
        # Papers the checked: lines named, handed to the librarian whatever their status.
        missing = [i for i in args.ids if i not in manifest["papers"]]
        if missing:
            M.log(f"index: --ids not in the manifest: {', '.join(missing)}")
            return M.EXIT_USAGE
        chosen = sorted((manifest["papers"][i] for i in dict.fromkeys(args.ids)), key=sort_key)
        sys.stdout.write(render_dump(manifest, chosen, coverage_lines(manifest, chosen, ", chosen by --ids")))
        M.log(f"index: dumped {len(chosen)} abstracts by id")
        return M.EXIT_OK
    papers = sorted(M.papers_in(manifest, *LISTED_STATES), key=sort_key)
    # old: if args.new_only and manifest.get("refreshed"):
    # old:     papers = [p for p in papers if (p.get("found_date") or "") >= manifest["refreshed"]]
    # old: ^ reverting these two silently truncates INDEX.md to the papers found since the stamp
    if not papers:
        M.log(f"index: no paper past selection for topic {args.topic}, nothing to index")
        return M.EXIT_NOTHING

    if guide_text is not None:
        # The librarian's one hard rule, enforced: an id the manifest does not hold is invented.
        ids, unknown, outside = check_ids(manifest, guide_text, {p["id"] for p in papers})
        if unknown:
            M.log(f"index: the guide cites {len(unknown)} id(s) not in the manifest, refused: "
                  f"{', '.join(unknown[:10])}")
            return M.EXIT_USAGE
        if outside:
            M.log(f"index: the guide cites {len(outside)} id(s) outside the set it was given "
                  f"(found/rejected), kept: {', '.join(outside[:10])}")
        M.log(f"index: the guide cites {len(ids)} ids, all in the manifest")

    if args.dump_abstracts:
        dump_papers = papers  # local to the dump: INDEX.md below must always see every paper
        if args.new_only and manifest.get("refreshed"):
            dump_papers = [p for p in papers if (p.get("found_date") or "") >= manifest["refreshed"]]
            # rank.py exits 4 and says why for the same condition. Exiting 0 with an empty
            # dump hands the librarian nothing while the pipeline reports success.
            if not dump_papers:
                M.log(f"index: --new-only found nothing since manifest.refreshed "
                      f"({manifest.get('refreshed')}), nothing for the librarian")
                return M.EXIT_NOTHING
        # old: sys.stdout.write(render_dump(manifest, papers))       # pre-release: --new-only was
        # old: M.log(f"index: dumped {len(papers)} abstracts")       # not applied to the dump at all
        # old: sys.stdout.write(render_dump(manifest, dump_papers))
        note = f", found since {manifest['refreshed']}" if args.new_only and manifest.get("refreshed") else ""
        sys.stdout.write(render_dump(manifest, dump_papers, coverage_lines(manifest, dump_papers, note)))
        M.log(f"index: dumped {len(dump_papers)} abstracts")
        return M.EXIT_OK

    index_path = tdir / "INDEX.md"
    if guide_text is None:
        guide_text = existing_guide(index_path)
        if guide_text:
            M.log("index: kept the guide block already in INDEX.md")
    atomic_write(index_path, render_index(manifest, papers, guide_text, tdir))

    n = {s: sum(1 for p in papers if p.get("status") == s) for s in LISTED_STATES}
    M.log(f"index: {len(papers)} papers indexed, {n['selected']} still pending")
    print(
        f"indexed={len(papers)} md={n['md']} pdf={n['pdf']} no-pdf={n['no-pdf']} "
        f"failed={n['failed']} written={index_path}"
    )
    return M.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
