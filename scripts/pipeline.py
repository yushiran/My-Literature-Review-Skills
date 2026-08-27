# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Run fetch -> convert -> index once for a topic.

Each step is a subprocess of the same interpreter. Exit 4 from fetch or
convert means nothing was waiting and is not an error. Exit 3 from convert
(MinerU token) stops the pipeline and propagates. Any other non-zero exit
stops and propagates. Contract: references/workflow.md.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as M  # noqa: E402

STEPS = ("fetch", "convert", "index")
OK_CODES = (M.EXIT_OK, M.EXIT_NOTHING)


def state_counts(manifest: dict) -> str:
    return " ".join(f"{s}={len(M.papers_in(manifest, s))}" for s in M.STATES)


def main() -> int:
    parser = M.base_parser("Run fetch -> convert -> index once.")
    parser.add_argument("--jobs", type=int, default=4, help="parallel jobs for fetch and convert (default 4)")
    parser.add_argument("--retry-failed", action="store_true", help="pass --retry-failed to convert")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    scripts = {step: here / f"{step}.py" for step in STEPS}
    missing = [str(p) for p in scripts.values() if not p.is_file()]
    if missing:
        M.log("pipeline: missing script(s): " + ", ".join(missing))
        return M.EXIT_USAGE

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
        M.log(f"pipeline: {step} exited {r.returncode}, stopping")
        return r.returncode

    print("pipeline: done", flush=True)
    print("states: " + state_counts(M.load(args)), flush=True)
    return M.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
