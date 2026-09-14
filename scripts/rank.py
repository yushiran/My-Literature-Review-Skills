# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Score every paper in the manifest and write candidates.md for the scout.

score = tier_weight * recency * (0.35 + 0.35*cites + 0.30*rel), normalised to [0,1].
cites and rel are each normalised to [0,1] by their max over the manifest first;
rel falls back to 0.5 (neutral) when a paper has no OpenAlex relevance score.
Only papers in found/selected/rejected are listed as candidates.
"""
import datetime as _dt
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as M  # noqa: E402

DEFAULT_VENUES = Path(__file__).resolve().parent.parent / "references" / "venues.yaml"
PREPRINT_MARKERS = ("arxiv", "biorxiv", "medrxiv", "ssrn")
# One-page conference abstracts. A society's proceedings title often contains a
# journal name -- ISMRM's contains "Magnetic Resonance in Medicine" -- so the
# substring rule below would otherwise score them as that journal. Keep the
# markers narrow: "annual meeting" alone would also catch ACL, whose full papers
# are tier 1.
ABSTRACT_MARKERS = ("scientific meeting", "proceedings on cd-rom",
                    "book of abstracts", "abstract supplement", "meeting abstracts")
CANDIDATE_STATES = ("found", "selected", "rejected")
# A triage.json must carry at least one of these. select.py --triage checks the same set,
# so a renamed key is one diagnosis whichever script the user ran first.
TRIAGE_KEYS = ("keep", "drop", "undecided")


def parse_venues(path: Path) -> dict:
    """Parse the fixed-shape venues.yaml without pyyaml: {tier: {weight, venues}}."""
    tiers, current = {}, None
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line == "tiers:" or line.startswith("venues:"):
            continue
        if line.endswith(":") and line[:-1].strip().isdigit():
            current = int(line[:-1])
            tiers[current] = {"weight": 1.0, "venues": []}
        elif current is None:
            raise ValueError(f"unexpected line before any tier: {raw!r}")
        elif line.startswith("weight:"):
            tiers[current]["weight"] = float(line.split(":", 1)[1])
        elif line.startswith("- "):
            tiers[current]["venues"].append(line[2:].strip())
        else:
            raise ValueError(f"unexpected line: {raw!r}")
    if not tiers:
        raise ValueError("no tiers found")
    return tiers


def venue_tier(venue: str, tiers: dict) -> int:
    """Lowest tier number whose venue list has a substring match; else 4 for preprints, 3 otherwise.
    A workshop is not the main conference, and an abstract volume is not its society's
    journal, so both are forced to tier 3 regardless of a match."""
    v = (venue or "").lower().strip()
    if "workshop" in v or any(m in v for m in ABSTRACT_MARKERS):
        return 3
    # acronyms (AAAI, TMI) match as a whole word; one-word names (Nature, Science) must equal
    # the venue; multi-word names match as substrings; the longest match wins across tiers
    best = None
    for tier in sorted(tiers):
        for name in tiers[tier]["venues"]:
            n = name.lower()
            if name.isupper():
                hit = re.search(rf"\b{re.escape(n)}\b", v) is not None
            elif len(n.split()) == 1:
                hit = v == n
            else:
                hit = n in v
            if hit and (best is None or len(n) > best[0]):
                best = (len(n), tier)
    if best:
        return best[1]
    if not v or any(m in v for m in PREPRINT_MARKERS):
        return 4
    return 3


def recency(year, current_year: int) -> float:
    if year is None:
        return 0.4
    if year >= current_year - 1:
        return 1.0
    if year >= current_year - 3:
        return 0.7
    return 0.4


def cites_raw(paper: dict, current_year: int) -> float:
    year = paper.get("year")
    citations = int(paper.get("citations") or 0)
    # Unknown year: treat as one year old for the per-year rate.
    age = max(1, current_year - year + 1) if year is not None else 1
    return math.log1p(citations / age)


def raw_score(paper: dict, tiers: dict, current_year: int, max_cites: float, max_relevance: float) -> float:
    year = paper.get("year")
    tier_w = tiers.get(paper["venue_tier"], {}).get("weight", 0.6)
    cites = cites_raw(paper, current_year) / max_cites if max_cites > 0 else 0.0
    relevance = paper.get("relevance")
    if relevance is None:
        rel = 0.5  # unknown, neutral
    else:
        rel = relevance / max_relevance if max_relevance > 0 else 0.0
    # Recency is a proxy for continuing relevance; citations per year measure it
    # directly. A paper the field still cites heavily is not stale, so let its
    # citation rate floor the age discount. Landmark papers older than the window
    # keep their place; old and little-cited ones are unaffected.
    age_factor = max(recency(year, current_year), cites)
    hits = min(int(paper.get("snowball_hits") or 0), 3)
    # return tier_w * age_factor * (0.35 + 0.35 * cites + 0.30 * rel)                          # old: no graph signal
    # return tier_w * age_factor * (0.35 + 0.35 * cites + 0.30 * rel) * (1 + 1.0 * hits / 3)  # old: hits>=1 repeats found_via
    # /2 is cap(3)-1: if the min(hits, 3) cap above ever becomes 4, this must become /3
    return tier_w * age_factor * (0.35 + 0.35 * cites + 0.30 * rel) * (1 + max(0, hits - 1) / 2)


FIRST_WORDS = 30


def meta_line(p):
    venue = p.get("venue") or "(no venue)"
    year = p.get("year") if p.get("year") is not None else "n.d."
    via = ",".join(p.get("found_via") or ["query"])
    hits = int(p.get("snowball_hits") or 0)
    return (f"{venue} (tier {p['venue_tier']}) · {year} · {int(p.get('citations') or 0)} citations"
            f" · score {p['score']:.2f} · via:{via}" + (f" · hits:{hits}" if hits else ""))


def write_titles(path: Path, manifest: dict, ranked: list) -> None:
    """Cheap first pass for the scout: title + venue/year/citations/via + a 30-word snippet,
    no abstract, no pdf line -- keeps the file small enough to always fit in one read."""
    lines = [f"# {manifest['topic']}: {len(ranked)} candidates, titles only (first {FIRST_WORDS} words of each abstract)"]
    # old: lines.extend(f"- {q}" for q in manifest.get("questions") or [])
    lines.extend(f"- {q}" for q in M.as_list(manifest.get("questions")))   # hand-edited field: a bare string is one question, not N characters
    lines.append("")
    for rank, p in enumerate(ranked, 1):
        words = (p.get("abstract") or "").split()
        # snippet = " ".join(words[:FIRST_WORDS]) + (" …" if len(words) > FIRST_WORDS else "")  # old: blank on empty abstract, not "(no abstract)"
        snippet = (" ".join(words[:FIRST_WORDS]) + (" …" if len(words) > FIRST_WORDS else "")) if words else "(no abstract)"
        lines += [f"## {rank}. {p['id']}", f"**{p.get('title') or '(no title)'}**", meta_line(p), snippet, ""]
    path.write_text("\n".join(lines) + "\n")


def write_parts(path: Path, manifest: dict, ranked: list, parts: int, writer, *extra) -> list:
    """Split `ranked` into `parts` files beside `path` (candidates_titles.1.md, .2.md, ...) for as many
    scouts to read side by side; round-robin, so every part spans the ranking. Stale parts of an
    earlier run with more parts are removed first. Returns the paths written."""
    for stale in path.parent.glob(f"{path.stem}.[0-9]*{path.suffix}"):
        stale.unlink()
    if parts < 2:
        return []
    written = []
    for k in range(parts):
        part_path = path.with_name(f"{path.stem}.{k + 1}{path.suffix}")
        writer(part_path, manifest, ranked[k::parts], *extra)
        written.append(part_path)
    return written


def write_candidates(path: Path, manifest: dict, ranked: list, topic_dir: Path) -> None:
    lines = [f"# {manifest['topic']}: {len(ranked)} candidates listed"]
    # old: questions = manifest.get("questions") or []   # hand-edited field: a bare string became N one-character questions
    questions = M.as_list(manifest.get("questions"))
    if questions:
        lines.append("Research questions:")
        lines.extend(f"- {q}" for q in questions)
    lines.append("")
    for rank, p in enumerate(ranked, 1):
        has_pdf = bool(p.get("pdf_url")) or (topic_dir / p.get("pdf", "")).is_file()
        lines.append(f"## {rank}. {p['id']}")
        lines.append(f"**{p.get('title') or '(no title)'}**")
        # venue = p.get("venue") or "(no venue)"                                      # old: hand-built meta line
        # year = p.get("year") if p.get("year") is not None else "n.d."               # old: hand-built meta line
        # sources = ",".join(p.get("sources") or []) or "-"                           # old: hand-built meta line
        # lines.append(                                                               # old: hand-built meta line
        #     f"{venue} (tier {p['venue_tier']}) · {year} · {int(p.get('citations') or 0)} citations"
        #     f" · score {p['score']:.2f} · sources: {sources}"
        # )
        lines.append(meta_line(p))
        lines.append(f"pdf: {'yes' if has_pdf else 'no'}")
        lines.append("")
        lines.append(p.get("abstract") or "(no abstract)")
        lines.append("")
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = M.base_parser("Score papers and write candidates.md for the scout.")
    parser.add_argument("--top", type=int, default=100, help="how many candidates to list (default 100)")
    parser.add_argument("--venues", default=str(DEFAULT_VENUES), help="venue tier file (default references/venues.yaml)")
    parser.add_argument("--only", help="triage.json: list only its keep and undecided ids in candidates.md")
    parser.add_argument("--new-only", action="store_true", help="list only papers found since manifest.refreshed")
    parser.add_argument("--parts", type=int, default=1,
                        help="also split the listing into K files, candidates_titles.<k>.md (candidates.<k>.md under --only), "
                             "one per scout to triage or select side by side (default 1: no parts)")
    args = parser.parse_args()
    if args.top < 1:
        M.log("rank: --top must be >= 1")
        return M.EXIT_USAGE
    if args.parts < 1:
        M.log("rank: --parts must be >= 1")
        return M.EXIT_USAGE

    venues_path = Path(args.venues)
    if not venues_path.is_file():
        M.log(f"rank: venues file not found: {venues_path}")
        return M.EXIT_USAGE
    try:
        tiers = parse_venues(venues_path)
    except ValueError as e:
        M.log(f"rank: cannot parse {venues_path}: {e}")
        return M.EXIT_USAGE

    manifest = M.load(args)
    papers = list(manifest["papers"].values())
    if not papers:
        M.log(f"rank: no papers in manifest for topic {args.topic}")
        return M.EXIT_NOTHING

    current_year = _dt.date.today().year
    for p in papers:
        p["venue_tier"] = venue_tier(p.get("venue", ""), tiers)
    max_cites = max((cites_raw(p, current_year) for p in papers), default=0.0)
    max_relevance = max((p["relevance"] for p in papers if p.get("relevance") is not None), default=0.0)
    for p in papers:
        p["score"] = raw_score(p, tiers, current_year, max_cites, max_relevance)
    top_score = max(p["score"] for p in papers)
    for p in papers:
        p["score"] = round(p["score"] / top_score, 4) if top_score > 0 else 0.0

    candidates = M.papers_in(manifest, *CANDIDATE_STATES)
    n_pool = len(candidates)          # for the empty-result log below: was there ever anything at all?
    tdir = M.topic_dir(args)
    out = tdir / "candidates.md"
    if args.new_only and manifest.get("refreshed"):
        candidates = [p for p in candidates if (p.get("found_date") or "") >= manifest["refreshed"]]
    # candidates.sort(key=lambda p: (-p["score"], -int(p.get("citations") or 0), p["id"]))    # old: no new-only filter, no titles file
    # ranked = candidates[: args.top]                                                          # old: see above
    # old: ranked = sorted(candidates, key=lambda p: (-p["score"], -int(p.get("citations") or 0), p["id"]))[: args.top]   # cut ran before --only, dropping papers the scout kept
    ranked = sorted(candidates, key=lambda p: (-p["score"], -int(p.get("citations") or 0), p["id"]))
    titles_out = tdir / "candidates_titles.md"
    # old: write_titles(titles_out, manifest, ranked)   # --only overwrote the list the scout triaged
    if not args.only:                 # --only's job is candidates.md; the scout has read the titles already
        write_titles(titles_out, manifest, ranked[: args.top])
        for part in write_parts(titles_out, manifest, ranked[: args.top], args.parts, write_titles):
            M.log(f"rank: wrote {part}")
    n_before_only = len(ranked)       # for the empty-result log below: did --only empty it?
    if args.only:
        try:
            tri = json.loads(Path(args.only).read_text())
            # Before any .get: `.get(k) or []` substitutes an empty list, so the guard below
            # cannot tell a renamed key from an empty one, and a typo silently keeps nothing.
            if not isinstance(tri, dict) or not set(TRIAGE_KEYS) & set(tri):
                raise ValueError("no keep, drop or undecided key; not a triage.json")
            keep, undecided = tri.get("keep") or [], tri.get("undecided") or []
            if not isinstance(keep, list) or not isinstance(undecided, list):
                raise ValueError("'keep' and 'undecided' must be lists")
            allowed = set(keep) | set(undecided)
        # old: except (OSError, json.JSONDecodeError, AttributeError, ValueError) as e:
        except (OSError, TypeError, json.JSONDecodeError, AttributeError, ValueError) as e:   # same tuple select.py uses on this file
            M.log(f"rank: cannot read {args.only}: {e}")
            return M.EXIT_USAGE
        ranked = [p for p in ranked if p["id"] in allowed]
    ranked = ranked[: args.top]       # cut last: a paper the scout kept must survive the filter first
    write_candidates(out, manifest, ranked, tdir)
    if args.only:                     # parts of the abstracts file are for SELECT, so only the triaged list is split
        for part in write_parts(out, manifest, ranked, args.parts, write_candidates, tdir):
            M.log(f"rank: wrote {part}")
    M.save(args, manifest)
    M.log(f"rank: scored {len(papers)} papers, {len(candidates)} candidates, listed {len(ranked)}")
    # old: print(f"ranked={len(candidates)} candidates={len(ranked)} written={out}")                          # before the titles file existed
    # old: print(f"ranked={len(candidates)} candidates={len(ranked)} written={out} titles={titles_out}")      # claims a write --only no longer makes
    # pipeline.py parses candidates= off this line; keep the field name and the count it holds.
    print(f"ranked={len(candidates)} candidates={len(ranked)} written={out} "
          f"titles={titles_out}{' (unchanged)' if args.only else ''}")
    if not ranked:
        # M.log("rank: no paper in found/selected/rejected, nothing for the scout")  # old: --only/--new-only can empty ranked too
        if args.new_only and n_pool and not candidates:
            M.log(f"rank: --new-only found nothing since manifest.refreshed ({manifest.get('refreshed')}), nothing for the scout")
        elif args.only and n_before_only:
            M.log("rank: --only (triage.json) kept none of the ranked candidates, nothing for the scout")
        else:
            M.log("rank: no paper in found/selected/rejected, nothing for the scout")
        return M.EXIT_NOTHING
    return M.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
