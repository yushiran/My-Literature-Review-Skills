import json
import sys
import importlib.util
from pathlib import Path

import manifest as M

# stdlib 'select' is a compiled-in builtin; load scripts/select.py directly instead of importing it.
_select_spec = importlib.util.spec_from_file_location(
    "scripts.select", Path(__file__).resolve().parents[1] / "scripts" / "select.py")
SEL = importlib.util.module_from_spec(_select_spec)
_select_spec.loader.exec_module(SEL)


def make_lib(args, ids_status):
    m = M.load(args)
    for pid, st in ids_status.items():
        m["papers"][pid] = M.new_paper(pid, title=pid, status=st)
    M.save(args, m)
    d = M.topic_dir(args)
    (d / "candidates.md").write_text("".join(f"## {i}. {pid}\n" for i, pid in enumerate(ids_status, 1)))
    return d


def run(args, argv):
    sys.argv = ["select.py", "--topic", args.topic, "--root", args.root] + argv
    return SEL.main()


def test_selected_but_unfetched_is_left_alone(args):
    d = make_lib(args, {"a": "selected", "b": "found", "c": "found"})
    (d / "sel.json").write_text(json.dumps([{"id": "c", "why": "w"}]))
    assert run(args, ["--file", str(d / "sel.json"), "--target", "1"]) == 0
    m = M.load(args)
    assert m["papers"]["a"]["status"] == "selected"   # a re-run leaves an already-selected, not-yet-fetched paper alone
    assert m["papers"]["b"]["status"] == "rejected" and m["papers"]["c"]["status"] == "selected"


def test_reselecting_refreshes_why_when_new_is_nonempty(args):
    d = make_lib(args, {"a": "selected"})
    m = M.load(args)
    m["papers"]["a"]["why"] = "old reason"
    M.save(args, m)
    (d / "sel.json").write_text(json.dumps([{"id": "a", "why": "new reason"}]))
    assert run(args, ["--file", str(d / "sel.json"), "--target", "1"]) == 0
    p = M.load(args)["papers"]["a"]
    assert p["status"] == "selected" and p["why"] == "new reason"


def test_reselecting_keeps_why_when_new_is_empty(args):
    d = make_lib(args, {"a": "selected"})
    m = M.load(args)
    m["papers"]["a"]["why"] = "old reason"
    M.save(args, m)
    (d / "sel.json").write_text(json.dumps([{"id": "a", "why": ""}]))
    assert run(args, ["--file", str(d / "sel.json"), "--target", "1"]) == 0
    p = M.load(args)["papers"]["a"]
    assert p["status"] == "selected" and p["why"] == "old reason"


def test_triage_drop_list_is_rejected(args):
    d = make_lib(args, {"a": "found", "b": "found"})
    (d / "sel.json").write_text(json.dumps([{"id": "a", "why": "w"}]))
    (d / "triage.json").write_text(json.dumps({"keep": ["a"], "drop": ["b"], "undecided": []}))
    (d / "candidates.md").write_text("## 1. a\n")          # b is not even in candidates.md
    assert run(args, ["--file", str(d / "sel.json"), "--triage", str(d / "triage.json"), "--target", "1"]) == 0
    m = M.load(args)
    assert m["papers"]["b"]["status"] == "rejected"
    assert m["papers"]["a"]["status"] == "selected"   # drop never overrides a paper this run selected


def test_triage_drop_rejects_with_no_candidates_file(args):
    d = make_lib(args, {"a": "found", "b": "found"})
    (d / "sel.json").write_text(json.dumps([{"id": "a", "why": "w"}]))
    (d / "triage.json").write_text(json.dumps({"keep": ["a"], "drop": ["b"], "undecided": []}))
    (d / "candidates.md").write_text("")          # no candidates this run, drop is the only rejection source
    assert run(args, ["--file", str(d / "sel.json"), "--triage", str(d / "triage.json"), "--target", "1"]) == 0
    assert M.load(args)["papers"]["b"]["status"] == "rejected"
