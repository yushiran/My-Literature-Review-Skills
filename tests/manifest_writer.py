"""One writer process for test_manifest_concurrency, run as a real subprocess.

Two roles whose read-modify-write windows deliberately overlap: `slow` loads
first and saves last, so its whole window contains `quick`'s entire run. That is
the shape workflow.md recommends -- pipeline.py in a background shell while the
scout works the next batch -- and the shape that used to lose everything `quick`
did. Usage: manifest_writer.py ROLE ROOT TOPIC SIGNAL_DIR
"""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import manifest as M

TIMEOUT = 60.0   # generous: a loaded test machine must not fail this on scheduling alone


def wait_for(flag: Path) -> None:
    end = time.monotonic() + TIMEOUT
    while not flag.exists():
        if time.monotonic() > end:
            raise SystemExit(f"manifest_writer: timed out waiting for {flag}")
        time.sleep(0.01)


def main() -> None:
    role, root, topic, sig = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4])
    args = SimpleNamespace(topic=topic, root=root)
    if role == "slow":
        m = M.load(args)
        (sig / "slow_loaded").touch()
        wait_for(sig / "quick_saved")     # quick now has loaded, changed and saved, all inside our window
        m["papers"]["a"]["status"] = "selected"      # same paper as quick, different fields
        m["papers"]["a"]["why"] = "slow picked it"
        m.setdefault("rounds", []).append({"date": "2026-09-13", "kind": "search", "new": 1})
        m.setdefault("seeds", []).append("slow-seed")
        m["queries"].append("slow-query")
        M.save(args, m)
    elif role == "blocked":
        m = M.load(args)
        m["papers"]["a"]["status"] = "selected"
        (sig / "about_to_save").touch()   # the parent holds the lock; this save must wait for it
        M.save(args, m)
        (sig / "saved").touch()
    else:
        wait_for(sig / "slow_loaded")     # start only once slow is holding a copy of the file
        m = M.load(args)
        m["papers"]["a"]["snowball_hits"] = 7
        m["papers"]["b"]["status"] = "pdf"
        m["papers"]["c"] = M.new_paper("c", title="Added by quick")
        m.setdefault("rounds", []).append({"date": "2026-09-13", "kind": "snowball", "new": 2})
        m.setdefault("seeds", []).append("quick-seed")
        m["queries"].append("quick-query")
        m["refreshed"] = "2026-09-13"
        M.save(args, m)
        (sig / "quick_saved").touch()


main()
