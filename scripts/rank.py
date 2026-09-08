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
    return tier_w * recency(year, current_year) * (0.35 + 0.35 * cites + 0.30 * rel)


def write_candidates(path: Path, manifest: dict, ranked: list, topic_dir: Path) -> None:
    lines = [f"# {manifest['topic']}: {len(ranked)} candidates listed"]
    questions = manifest.get("questions") or []
    if questions:
        lines.append("Research questions:")
        lines.extend(f"- {q}" for q in questions)
    lines.append("")
    for rank, p in enumerate(ranked, 1):
        has_pdf = bool(p.get("pdf_url")) or (topic_dir / p.get("pdf", "")).is_file()
        venue = p.get("venue") or "(no venue)"
        year = p.get("year") if p.get("year") is not None else "n.d."
        sources = ",".join(p.get("sources") or []) or "-"
        lines.append(f"## {rank}. {p['id']}")
        lines.append(f"**{p.get('title') or '(no title)'}**")
        lines.append(
            f"{venue} (tier {p['venue_tier']}) · {year} · {int(p.get('citations') or 0)} citations"
            f" · score {p['score']:.2f} · sources: {sources}"
        )
        lines.append(f"pdf: {'yes' if has_pdf else 'no'}")
        lines.append("")
        lines.append(p.get("abstract") or "(no abstract)")
        lines.append("")
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = M.base_parser("Score papers and write candidates.md for the scout.")
    parser.add_argument("--top", type=int, default=100, help="how many candidates to list (default 100)")
    parser.add_argument("--venues", default=str(DEFAULT_VENUES), help="venue tier file (default references/venues.yaml)")
    args = parser.parse_args()
    if args.top < 1:
        M.log("rank: --top must be >= 1")
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
    candidates.sort(key=lambda p: (-p["score"], -int(p.get("citations") or 0), p["id"]))
    ranked = candidates[: args.top]

    tdir = M.topic_dir(args)
    out = tdir / "candidates.md"
    write_candidates(out, manifest, ranked, tdir)
    M.save(args, manifest)
    M.log(f"rank: scored {len(papers)} papers, {len(candidates)} candidates, listed {len(ranked)}")
    print(f"ranked={len(candidates)} candidates={len(ranked)} written={out}")
    if not ranked:
        M.log("rank: no paper in found/selected/rejected, nothing for the scout")
        return M.EXIT_NOTHING
    return M.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
