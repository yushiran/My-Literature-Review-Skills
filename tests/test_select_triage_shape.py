"""select.py's triage.json guard: a malformed file must not silently reject nothing."""
import importlib.util
import json
import sys
from pathlib import Path

import manifest as M

# stdlib 'select' is a compiled-in builtin; load scripts/select.py directly instead.
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
    (d / "sel.json").write_text(json.dumps([{"id": "a", "why": "w"}]))
    return d


def run(args, d, triage):
    (d / "triage.json").write_text(json.dumps(triage))
    sys.argv = ["select.py", "--topic", args.topic, "--root", args.root,
                "--file", str(d / "sel.json"), "--triage", str(d / "triage.json"), "--target", "1"]
    return SEL.main()


def test_a_triage_file_with_no_recognised_key_is_a_usage_error(args, capsys):
    """`.get("drop") or []` turns a misspelled key into a valid empty list, so the shape
    guard never sees the malformed value and the run rejects nothing while exiting 0."""
    d = make_lib(args, {"a": "found", "b": "found"})
    assert run(args, d, {"keeps": ["a"], "drops": ["b"]}) == M.EXIT_USAGE
    assert "triage.json" in capsys.readouterr().err
    assert M.load(args)["papers"]["b"]["status"] == "found"   # nothing was written


def test_an_unhashable_entry_in_a_triage_list_is_a_usage_error(args, capsys):
    d = make_lib(args, {"a": "found", "b": "found"})
    assert run(args, d, {"keep": ["a"], "drop": [["b"]], "undecided": []}) == M.EXIT_USAGE
    assert "triage.json" in capsys.readouterr().err


def test_a_triage_file_with_only_an_empty_keep_list_still_runs(args):
    """One recognised key is enough; an empty drop list rejecting nothing is a real answer."""
    d = make_lib(args, {"a": "found", "b": "found"})
    assert run(args, d, {"keep": []}) == M.EXIT_OK
    assert M.load(args)["papers"]["b"]["status"] == "rejected"   # candidates.md still rejects
