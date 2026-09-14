# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Run fetch -> convert -> index once for a topic.

Each step is a subprocess of the same interpreter. Exit 4 from fetch or
convert means nothing was waiting and is not an error. Exit 3 from convert
(MinerU token) stops the pipeline and propagates. Exit 2 from fetch means a
network or source was unavailable, which is either a dead connection or a stale
credential; fetch says which. The papers that did arrive still convert and index,
and the 2 is returned at the end so the caller knows to re-run. Any other non-zero
exit stops and propagates.
--refresh is the other mode: re-run the stored queries since the last refresh,
snowball forward, rank only what is new, then stop for the scout. It does not
fetch and does not convert.
Contract: references/workflow.md.
"""
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as M  # noqa: E402

STEPS = ("fetch", "convert", "index")
REFRESH_STEPS = ("search", "snowball", "rank")
OK_CODES = (M.EXIT_OK, M.EXIT_NOTHING)


def state_counts(manifest: dict) -> str:
    return " ".join(f"{s}={len(M.papers_in(manifest, s))}" for s in M.STATES)


def main() -> int:
    parser = M.base_parser("Run fetch -> convert -> index once.")
    parser.add_argument("--jobs", type=int, default=4, help="parallel jobs for fetch and convert (default 4)")
    parser.add_argument("--retry-failed", action="store_true", help="pass --retry-failed to convert")
    parser.add_argument("--refresh", action="store_true",
                        help="re-run the stored queries since the last refresh and stop for the scout")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    # old: scripts = {step: here / f"{step}.py" for step in STEPS}   # KeyError on scripts["search"] under --refresh
    scripts = {step: here / f"{step}.py" for step in (REFRESH_STEPS if args.refresh else STEPS)}
    missing = [str(p) for p in scripts.values() if not p.is_file()]
    if missing:
        M.log("pipeline: missing script(s): " + ", ".join(missing))
        return M.EXIT_USAGE

    if args.refresh:
        manifest = M.load(args)
        # old: queries = manifest.get("queries") or []   # a hand-typed string becomes one --query flag per character
        queries = M.as_list(manifest.get("queries"))
        if not queries:
            M.log("pipeline: --refresh needs stored queries; run search.py first")
            return M.EXIT_USAGE
        prev = manifest.get("refreshed")     # the date rank.py --new-only filters on
        # old: since_year = int((prev or manifest.get("created") or M.today())[:4])   # dies on a hand-edited date
        try:
            since_year = int(str(prev or manifest.get("created") or M.today())[:4])
        except ValueError:   # a hand-edited date must not kill the run before a single step
            M.log("pipeline: unusable date in the manifest; searching from this year")
            since_year = int(M.today()[:4])
        steps = [
            [sys.executable, str(scripts["search"]), "--topic", args.topic, "--root", args.root, "--since", str(since_year)]
            + [x for q in queries for x in ("--query", q)],
            [sys.executable, str(scripts["snowball"]), "--topic", args.topic, "--root", args.root, "--no-back", "--since", str(since_year)],
            [sys.executable, str(scripts["rank"]), "--topic", args.topic, "--root", args.root, "--new-only"],
        ]
        counted, deferred = "", M.EXIT_OK   # a partial search, reported after rank has run
        for cmd in steps:
            step = Path(cmd[1]).stem
            try:
                r = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
            except KeyboardInterrupt:
                M.log(f"pipeline: interrupted during {step}")
                return M.EXIT_USAGE
            lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
            for ln in lines:
                print(f"{step}: {ln}", flush=True)
            if not lines:
                print(f"{step}: exit {r.returncode}", flush=True)
            if step == "rank":
                counted = r.stdout or ""
            if r.returncode in OK_CODES:
                continue
            if step in ("search", "snowball") and r.returncode == M.EXIT_NETWORK:
                # Both save what they got before returning 2, so rank still has something to list.
                M.log(f"pipeline: {step} saved partial results; ranking what arrived")
                deferred = r.returncode
                continue
            M.log(f"pipeline: {step} exited {r.returncode}, stopping")
            return r.returncode
        manifest = M.load(args)
        if deferred == M.EXIT_OK:
            # Stamped after the steps so rank --new-only filters on the previous value, not today's.
            # Skipped after a partial search: the window is not covered, so do not close it.
            manifest["refreshed"] = M.today()
            M.save(args, manifest)
        # old: # rank.py's candidates= is its own count after every filter and the --top cap,
        # old: # so it is what the file holds. Recompute only if that line is gone.
        # rank.py's candidates= is its count after --new-only and the --top cap, which is what
        # candidates_titles.md holds on this path because it never passes --only; --only filters
        # after that file is written. Recompute only if the line is gone.
        m = re.search(r"\bcandidates=(\d+)", counted)
        if m:
            n = int(m.group(1))
        else:
            n = len([p for p in M.papers_in(manifest, "found", "selected", "rejected")
                     if not prev or (p.get("found_date") or "") >= prev])
        # old: print(f"refresh: {n} new candidates in candidates_titles.md; "
        # old:       "run the scout TRIAGE then SELECT, then pipeline.py", flush=True)
        if n:
            print(f"refresh: {n} new candidates in candidates_titles.md; "
                  "run the scout TRIAGE then SELECT, then pipeline.py", flush=True)
        else:
            print("refresh: no new candidates since the last refresh; nothing to triage", flush=True)
        if deferred != M.EXIT_OK:
            M.log("pipeline: the search was incomplete, so the refresh date was not advanced; "
                  "re-run --refresh once the network settles")
            return deferred
        return M.EXIT_OK if n else M.EXIT_NOTHING

    deferred = M.EXIT_OK       # a recoverable fetch failure, reported after the rest has run
    for step in STEPS:
        cmd = [sys.executable, str(scripts[step]), "--topic", args.topic, "--root", args.root]
        if step in ("fetch", "convert"):
            cmd += ["--jobs", str(args.jobs)]
        if step == "convert" and args.retry_failed:
            cmd.append("--retry-failed")
        M.log(f"pipeline: {step} ...")
        try:
            r = subprocess.run(cmd, stdout=subprocess.PIPE, text=True)
        except KeyboardInterrupt:
            M.log(f"pipeline: interrupted during {step}")
            return M.EXIT_USAGE
        lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
        for ln in lines:
            print(f"{step}: {ln}", flush=True)
        if not lines:
            print(f"{step}: exit {r.returncode}", flush=True)
        if r.returncode in OK_CODES:
            continue
        if r.returncode == M.EXIT_TOKEN:
            M.log("pipeline: stopped, MinerU token missing or rejected (see convert.py above)")
            return M.EXIT_TOKEN
        if step == "fetch" and r.returncode == M.EXIT_NETWORK:
            # Exit 2 means a stale credential OR a dead network, and fetch has already
            # printed which. The papers that did arrive are fine, so convert and index
            # them; the rest stay `selected` for a re-run.
            # old: M.log("pipeline: fetch needs a credential refreshed; converting what arrived")
            M.log("pipeline: fetch could not finish (see its message above); converting what arrived")
            deferred = r.returncode
            continue
        M.log(f"pipeline: {step} exited {r.returncode}, stopping")
        return r.returncode

    print("pipeline: done", flush=True)
    print("states: " + state_counts(M.load(args)), flush=True)
    if deferred != M.EXIT_OK:
        # old: M.log("pipeline: refresh the credential and re-run to collect the papers left selected")
        M.log("pipeline: clear the cause above and re-run to collect the papers left selected")
    return deferred


if __name__ == "__main__":
    sys.exit(main())
