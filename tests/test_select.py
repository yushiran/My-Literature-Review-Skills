import json
import sys
import importlib.util
from pathlib import Path

import manifest as M

# stdlib 'select' is a compiled-in builtin here, unshadowable via sys.path;
# load scripts/select.py under that name so the import below binds to it.
_select_spec = importlib.util.spec_from_file_location(
    "select", Path(__file__).resolve().parents[1] / "scripts" / "select.py")
sys.modules["select"] = importlib.util.module_from_spec(_select_spec)
_select_spec.loader.exec_module(sys.modules["select"])
import select as SEL


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
    assert m["papers"]["a"]["status"] == "selected"   # was rejected before the fix
    assert m["papers"]["b"]["status"] == "rejected" and m["papers"]["c"]["status"] == "selected"


def test_triage_drop_list_is_rejected(args):
    d = make_lib(args, {"a": "found", "b": "found"})
    (d / "sel.json").write_text(json.dumps([{"id": "a", "why": "w"}]))
    (d / "triage.json").write_text(json.dumps({"keep": ["a"], "drop": ["b"], "undecided": []}))
    (d / "candidates.md").write_text("## 1. a\n")          # b is not even in candidates.md
    assert run(args, ["--file", str(d / "sel.json"), "--triage", str(d / "triage.json"), "--target", "1"]) == 0
    assert M.load(args)["papers"]["b"]["status"] == "rejected"
