import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import manifest as M

WRITER = Path(__file__).resolve().parent / "manifest_writer.py"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
ENV = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(SCRIPTS), "PYTHONHASHSEED": "0"}


def writer(role, args, sig):
    return subprocess.Popen([sys.executable, str(WRITER), role, args.root, args.topic, str(sig)],
                            env=ENV, stderr=subprocess.PIPE, text=True)


def wait_for(flag, timeout=60.0):
    end = time.monotonic() + timeout
    while not flag.exists():
        assert time.monotonic() < end, f"timed out waiting for {flag}"
        time.sleep(0.01)


def seed(args, **scalars):
    """A two-paper library, saved, so both writers start from the same file."""
    m = M.load(args)
    for pid in ("a", "b"):
        m["papers"][pid] = M.new_paper(pid, title=f"Paper {pid}")
    m["queries"].append("seed-query")
    m.update(scalars)
    M.save(args, m)
    return m


def test_two_processes_do_not_lose_each_others_work(args, tmp_path):
    seed(args, questions=["q1"])
    sig = tmp_path / "signals"
    sig.mkdir()
    procs = [writer(role, args, sig) for role in ("slow", "quick")]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err

    m = M.load(args)
    a = m["papers"]["a"]
    assert a["status"] == "selected", "slow's completed selection was lost"
    assert a["why"] == "slow picked it"
    assert a["snowball_hits"] == 7, "quick's snowball_hits on the same paper was lost"
    assert m["papers"]["b"]["status"] == "pdf", "quick's status change was lost"
    assert "c" in m["papers"], "the paper quick added was lost"
    assert {r["kind"] for r in m["rounds"]} == {"search", "snowball"}, m["rounds"]
    assert set(m["seeds"]) == {"slow-seed", "quick-seed"}, m["seeds"]
    assert set(m["queries"]) == {"seed-query", "slow-query", "quick-query"}, m["queries"]
    assert m["refreshed"] == "2026-09-13", "quick's refreshed timestamp was lost"
    assert m["questions"] == ["q1"]


def test_a_save_waits_for_the_lock(args, tmp_path):
    """Merging alone still leaves a read-write window; this is the part that closes it."""
    seed(args)
    sig = tmp_path / "signals"
    sig.mkdir()
    held = os.open(str(M.manifest_path(args)) + ".lock", os.O_RDONLY | os.O_CREAT, 0o666)
    fcntl.flock(held, fcntl.LOCK_EX)
    proc = writer("blocked", args, sig)
    try:
        wait_for(sig / "about_to_save")
        time.sleep(0.5)
        assert proc.poll() is None and not (sig / "saved").exists(), "the save did not wait for the lock"
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
    try:
        _, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    assert proc.returncode == 0, err
    assert M.load(args)["papers"]["a"]["status"] == "selected"


def test_one_paper_two_writers_different_fields(args):
    seed(args)
    first, second = M.load(args), M.load(args)
    first["papers"]["a"]["status"] = "selected"
    second["papers"]["a"]["why"] = "second says so"
    M.save(args, second)
    M.save(args, first)
    p = M.load(args)["papers"]["a"]
    assert p["status"] == "selected" and p["why"] == "second says so"


def test_same_field_twice_is_last_writer_wins(args):
    """The documented limit of merge-on-save, pinned so it cannot change silently."""
    seed(args)
    first, second = M.load(args), M.load(args)
    first["papers"]["a"]["status"] = "rejected"
    second["papers"]["a"]["status"] = "selected"
    M.save(args, first)
    M.save(args, second)
    assert M.load(args)["papers"]["a"]["status"] == "selected"


def test_a_paper_this_process_never_saw_survives(args):
    seed(args)
    stale = M.load(args)
    other = M.load(args)
    other["papers"]["new"] = M.new_paper("new", title="Added meanwhile")
    M.save(args, other)
    stale["papers"]["a"]["status"] = "selected"
    M.save(args, stale)
    assert "new" in M.load(args)["papers"]


def test_removing_a_field_still_removes_it(args):
    """convert.py drops paper['conversion'] when a re-run produces a full md."""
    seed(args)
    m = M.load(args)
    m["papers"]["a"]["conversion"] = "local-text"
    M.save(args, m)
    m = M.load(args)
    m["papers"]["a"].pop("conversion", None)
    M.save(args, m)
    assert "conversion" not in M.load(args)["papers"]["a"]


def test_append_only_lists_keep_both_appends(args):
    seed(args)
    first, second = M.load(args), M.load(args)
    first["queries"].append("qa")
    first.setdefault("rounds", []).append({"kind": "search"})
    second["queries"].append("qb")
    second.setdefault("rounds", []).append({"kind": "snowball"})
    M.save(args, second)
    M.save(args, first)
    m = M.load(args)
    assert set(m["queries"]) == {"seed-query", "qa", "qb"}
    assert {r["kind"] for r in m["rounds"]} == {"search", "snowball"}


def test_set_like_lists_do_not_gain_duplicates(args):
    """search.py already guards its own copy with `if q not in ...`; the merge must too."""
    seed(args)
    first, second = M.load(args), M.load(args)
    first["queries"].append("same")
    second["queries"].append("same")
    first.setdefault("seeds", []).append("s")
    second.setdefault("seeds", []).append("s")
    M.save(args, first)
    M.save(args, second)
    m = M.load(args)
    assert m["queries"].count("same") == 1
    assert m["seeds"].count("s") == 1


def test_repeated_saves_of_one_manifest_do_not_duplicate(args):
    """fetch.py saves the same object every SAVE_EVERY completions."""
    seed(args)
    m = M.load(args)
    m["queries"].append("q")
    m.setdefault("rounds", []).append({"kind": "search"})
    M.save(args, m)
    M.save(args, m)
    m["papers"]["a"]["status"] = "pdf"
    M.save(args, m)
    got = M.load(args)
    assert got["queries"].count("q") == 1 and len(got["rounds"]) == 1
    assert got["papers"]["a"]["status"] == "pdf"


def test_a_field_we_already_wrote_is_not_re_asserted_later(args):
    """fetch.py saves periodically while convert.py moves the same paper on."""
    seed(args)
    m = M.load(args)
    m["papers"]["a"]["status"] = "pdf"
    M.save(args, m)                            # fetch's periodic save
    other = M.load(args)
    other["papers"]["a"]["status"] = "md"      # convert, in parallel, takes it further
    M.save(args, other)
    m["papers"]["b"]["status"] = "pdf"         # fetch works on and saves again
    M.save(args, m)
    assert M.load(args)["papers"]["a"]["status"] == "md"


def test_a_paper_we_added_is_not_re_asserted_later(args):
    """search.py adds the paper; select.py picks it up before search's next save."""
    m = M.load(args)
    m["papers"]["a"] = M.new_paper("a", title="A")
    M.save(args, m)
    other = M.load(args)
    other["papers"]["a"]["status"] = "selected"
    M.save(args, other)
    m["papers"]["a"]["why"] = "a later note"
    M.save(args, m)
    p = M.load(args)["papers"]["a"]
    assert p["status"] == "selected" and p["why"] == "a later note"


def test_scalar_this_process_never_touched_is_not_written_back_stale(args):
    seed(args, refreshed="2026-01-01")
    stale = M.load(args)                       # reads refreshed, never changes it
    other = M.load(args)
    other["refreshed"] = "2026-09-13"
    M.save(args, other)
    stale["papers"]["a"]["status"] = "selected"
    M.save(args, stale)
    assert M.load(args)["refreshed"] == "2026-09-13"


def test_scalar_this_process_did_change_is_written(args):
    seed(args, refreshed="2026-01-01")
    m = M.load(args)
    m["refreshed"] = "2026-09-13"
    m["since"] = 2024
    M.save(args, m)
    got = M.load(args)
    assert got["refreshed"] == "2026-09-13" and got["since"] == 2024


def test_truncated_manifest_exits_cleanly(args, capsys):
    seed(args)
    M.manifest_path(args).write_text('{"papers": {"a": {"id"')
    with pytest.raises(SystemExit) as e:
        M.load(args)
    assert e.value.code == M.EXIT_USAGE
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "manifest.json" in err[0]


def test_manifest_without_papers_exits_cleanly(args, capsys):
    seed(args)
    M.manifest_path(args).write_text(json.dumps({"topic": "t", "queries": []}))
    with pytest.raises(SystemExit) as e:
        M.load(args)
    assert e.value.code == M.EXIT_USAGE
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "papers" in err[0]
