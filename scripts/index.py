# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Write INDEX.md for a topic from manifest.json.

Lists every paper past selection (selected, pdf, no-pdf, md, failed) as a
table plus one abstract block each. Selected/no-pdf/failed papers and any
local-text conversion are called out in a header block and, in the dump,
an inline marker. A reading guide sits between
<!-- guide --> markers at the top: --guide replaces it, otherwise the block
already in INDEX.md is kept, otherwise a placeholder. --dump-abstracts prints
the questions and abstracts for the librarian and writes nothing;
--new-only then restricts the dump to papers found since manifest.refreshed.
Contract: references/workflow.md.
"""
import datetime as _dt
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as M  # noqa: E402

LISTED_STATES = ("selected", "pdf", "no-pdf", "md", "failed")
UNREAD_STATES = ("selected", "no-pdf", "failed")  # statuses whose guide entry rests on the abstract only
GUIDE_OPEN, GUIDE_CLOSE = "<!-- guide -->", "<!-- /guide -->"
NO_GUIDE = "_No reading guide yet._"
GUIDE_BLOCK = re.compile(re.escape(GUIDE_OPEN) + r"\n(.*?)\n?" + re.escape(GUIDE_CLOSE), re.S)
MAX_AUTHORS = 3


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
    body = m.group(1).strip()
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
    qs = [q for q in (manifest.get("questions") or []) if q]
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


def render_index(manifest: dict, papers: list, guide: str, tdir: Path) -> str:
    counts = {s: sum(1 for p in papers if p.get("status") == s) for s in LISTED_STATES}
    lines = [f"# {manifest['topic']} — literature index"]
    lines.append(
        f"Generated {_dt.date.today().isoformat()}. {len(papers)} papers: "
        f"{counts['md']} converted, {counts['pdf']} pdf only, {counts['no-pdf']} abstract only, "
        f"{counts['failed']} failed, {counts['selected']} pending."
    )
    lines.append("")
    lines.extend(unread_lines(papers))
    lines.extend(questions_lines(manifest))
    lines.extend([GUIDE_OPEN, guide or NO_GUIDE, GUIDE_CLOSE, ""])

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


def render_dump(manifest: dict, papers: list) -> str:
    lines = questions_lines(manifest)
    for p in papers:
        year = p.get("year") if p.get("year") is not None else "n.d."
        # old: lines.append(f"### {p['id']}")
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
    args = parser.parse_args()

    guide_text = None
    if args.guide:
        guide_path = Path(args.guide)
        if not guide_path.is_file():
            M.log(f"index: guide file not found: {guide_path}")
            return M.EXIT_USAGE
        guide_text = guide_path.read_text().strip()

    manifest = M.load(args)
    tdir = M.topic_dir(args)
    papers = sorted(M.papers_in(manifest, *LISTED_STATES), key=sort_key)
    # old: if args.new_only and manifest.get("refreshed"):
    # old:     papers = [p for p in papers if (p.get("found_date") or "") >= manifest["refreshed"]]
    if not papers:
        M.log(f"index: no paper past selection for topic {args.topic}, nothing to index")
        return M.EXIT_NOTHING

    if args.dump_abstracts:
        dump_papers = papers  # local to the dump: INDEX.md below must always see every paper
        if args.new_only and manifest.get("refreshed"):
            dump_papers = [p for p in papers if (p.get("found_date") or "") >= manifest["refreshed"]]
        sys.stdout.write(render_dump(manifest, dump_papers))
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
