# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""One hop on the OpenAlex citation graph from the papers already chosen.

Backward: the works they reference (where the canon lives). Forward: the works
that cite them, restricted to --since (where the newest followers live). New
papers enter as `found` with found_via snowball-back / snowball-forward and
snowball_hits = how many source papers they connect to; rank.py rewards hits.
The graph filters by relevance for free: everything here is one edge from a
paper a human already judged relevant. Contract: references/workflow.md.
"""
import collections
import datetime as _dt
import os
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as M  # noqa: E402
from search import OPENALEX_SELECT, MAILTO, SourceDown, RateLimited, get_json_retry, merge, openalex_record  # noqa: E402

BASE = "https://api.openalex.org/works"
BATCH = 100         # measured: 100 ids/filter is accepted for one credit; 101 is refused
DEFAULT_FROM = "selected,pdf,no-pdf,md,failed"


def short(wid):
    return (wid or "").rsplit("/", 1)[-1]


def fetch_by_ids(ids, select):
    """Works for a list of short ids, in batches; returns raw works."""
    out = []
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        # "openalex" is the documented alias for ids.openalex; "openalex_id" is not a real filter.
        params = {"filter": "openalex:" + "|".join(chunk), "per-page": BATCH, "select": select, "mailto": MAILTO}
        if os.environ.get("OPENALEX_API_KEY"):
            params["api_key"] = os.environ["OPENALEX_API_KEY"]
        with M.host_gate("api.openalex.org", 0.1):
            data = get_json_retry(f"{BASE}?{urllib.parse.urlencode(params)}", name="openalex")
        out.extend(data.get("results") or [])
    return out


def fetch_citers(wid, since, cap):
    """Works citing wid, most cited first, at most cap."""
    out, cursor = [], "*"
    while len(out) < cap and cursor:
        params = {"filter": f"cites:{wid},publication_year:>{since - 1}", "per-page": min(200, cap - len(out)),
                  "sort": "cited_by_count:desc", "cursor": cursor, "select": OPENALEX_SELECT, "mailto": MAILTO}
        if os.environ.get("OPENALEX_API_KEY"):
            params["api_key"] = os.environ["OPENALEX_API_KEY"]
        with M.host_gate("api.openalex.org", 0.1):
            data = get_json_retry(f"{BASE}?{urllib.parse.urlencode(params)}", name="openalex")
        out.extend(data.get("results") or [])
        cursor = (data.get("meta") or {}).get("next_cursor")
        if not data.get("results"):
            break
    return out[:cap]


def main() -> int:
    parser = M.base_parser("Add the papers one citation hop from the chosen set.")
    parser.add_argument("--from", dest="from_states", default=DEFAULT_FROM,
                        help=f"comma-separated source states (default {DEFAULT_FROM})")
    parser.add_argument("--since", type=int, default=_dt.date.today().year - 2,
                        help="earliest year for forward (citing) papers; backward has no year limit")
    parser.add_argument("--max-per-paper", type=int, default=200, help="cap on citing papers per source (default 200)")
    parser.add_argument("--no-back", action="store_true")
    parser.add_argument("--no-forward", action="store_true")
    args = parser.parse_args()

    manifest = M.load(args)
    states = tuple(s.strip() for s in args.from_states.split(",") if s.strip())
    sources = [p for p in M.papers_in(manifest, *states) if p.get("openalex")]
    if not sources:
        M.log("snowball: no source paper carries an OpenAlex id; run search.py first")
        return M.EXIT_NOTHING
    known = {p["openalex"] for p in manifest["papers"].values() if p.get("openalex")}
    # old: hits = collections.Counter()       # candidate openalex id -> number of sources connected
    links = collections.defaultdict(set)  # candidate openalex id -> set of source paper ids linking to it
    via = {}                            # candidate id -> snowball-back | snowball-forward
    raw = {}                            # candidate id -> raw work (for the merge)
    report = []
    try:
        if not args.no_back:
            need = [p for p in sources if not p.get("refs")]
            for w in fetch_by_ids([p["openalex"] for p in need], "id,referenced_works"):
                # old: pid = next(p["id"] for p in need if p["openalex"] == short(w["id"]))
                pid = next((p["id"] for p in need if p["openalex"] == short(w["id"])), None)
                if pid:                      # OpenAlex answers a merged work under its canonical id
                    manifest["papers"][pid]["refs"] = [short(r) for r in w.get("referenced_works") or []]
            for p in sources:
                # old: refs = [r for r in p.get("refs") or [] if r not in known]
                refs = p.get("refs") or []    # count every reference; "known" only gates the merge below
                for r in refs:
                    links[r].add(p["id"])
                    via.setdefault(r, "snowball-back")
                report.append((p["id"], len(refs), 0))
            # old: back_ids = [r for r in hits if via[r] == "snowball-back"]
            back_ids = [r for r in links if via[r] == "snowball-back" and r not in known]
            for w in fetch_by_ids(back_ids, OPENALEX_SELECT):
                raw[short(w["id"])] = w
        if not args.no_forward:
            for i, p in enumerate(sources):
                citers = fetch_citers(p["openalex"], args.since, args.max_per_paper)
                for w in citers:
                    wid = short(w["id"])
                    # old: if wid in known: continue
                    links[wid].add(p["id"])
                    via.setdefault(wid, "snowball-forward")
                    if wid not in known:     # known papers still count a hit; no need to re-fetch them
                        raw.setdefault(wid, w)
                # old: if report and report[-1][0] == p["id"] and i < len(report):
                if i < len(report):     # backward appended one row per source, in this same order
                    report[i] = (p["id"], report[i][1], len(citers))
                else:
                    report.append((p["id"], 0, len(citers)))
    except (SourceDown, RateLimited) as e:
        M.save(args, manifest)
        M.log(f"snowball: OpenAlex stopped answering ({e}); partial results saved")
        return M.EXIT_NETWORK

    hits = {w: len(s) for w, s in links.items()}  # distinct source papers linking to each candidate
    total_before = len(manifest["papers"])
    new = 0
    for kind in ("snowball-back", "snowball-forward"):
        recs = [openalex_record(raw[w]) for w in raw if via.get(w) == kind]
        new += merge(manifest, recs, via=kind)
    for p in manifest["papers"].values():
        if p.get("openalex") in hits:
            p["snowball_hits"] = max(int(p.get("snowball_hits") or 0), hits[p["openalex"]])
    hits2 = sum(1 for w, n in hits.items() if n >= 2 and w in raw)
    # Distinct ids reached in each direction: known ones and ones OpenAlex cannot resolve
    # both count here; "new" above is only the subset that resolved and merged as papers.
    back_count = sum(1 for v in via.values() if v == "snowball-back")
    forward_count = sum(1 for v in via.values() if v == "snowball-forward")
    # old: manifest.setdefault("rounds", []).append({"date": M.today(), "kind": "snowball", "sources": len(sources), "new": new, "total_before": total_before, "since": args.since})
    manifest.setdefault("rounds", []).append({"date": M.today(), "kind": "snowball", "sources": len(sources),
                                              "new": new, "total_before": total_before, "since": args.since,
                                              "back": back_count, "forward": forward_count, "hits2plus": hits2})
    M.save(args, manifest)

    lines = [f"# snowball for {manifest['topic']}: {len(sources)} sources, {new} new papers", "",
             "| source | references | citers fetched |", "| --- | ---: | ---: |"]
    lines += [f"| {pid} | {nb} | {nf} |" for pid, nb, nf in report]
    top = sorted(((n, w) for w, n in hits.items() if w in raw), reverse=True)[:20]
    lines += ["", "Most connected new candidates (hits = number of source papers linked):", ""]
    lines += [f"- {n} hits · {short(w)} · {raw[w].get('title')}" for n, w in top]
    (M.topic_dir(args) / "snowball.md").write_text("\n".join(lines) + "\n")
    # old: print(f"snowball: sources={len(sources)} back={sum(1 for v in via.values() if v == 'snowball-back')} " f"forward={sum(1 for v in via.values() if v == 'snowball-forward')} new={new} hits2plus={hits2}")
    print(f"snowball: sources={len(sources)} back={back_count} forward={forward_count} new={new} hits2plus={hits2}")
    return M.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
