import sys
import manifest as M
import index as IX


def lib(args):
    m = M.load(args)
    for pid, st, extra in [("a", "md", {}), ("b", "selected", {}), ("c", "no-pdf", {"error": "paywall"}),
                           ("d", "md", {"conversion": "local-text"})]:
        p = M.new_paper(pid, title=pid, status=st, abstract="abs")
        p.update(extra)
        m["papers"][pid] = p
    m["refreshed"] = M.today()
    m["papers"]["a"]["found_date"] = "2000-01-01"
    M.save(args, m)


def run(args, argv):
    sys.argv = ["index.py", "--topic", args.topic, "--root", args.root] + argv
    return IX.main()


def test_header_lists_unread_and_text_only(args):
    lib(args)
    assert run(args, []) == 0
    t = (M.topic_dir(args) / "INDEX.md").read_text()
    assert "Selected but unread (2)" in t and "c — paywall" in t and "b —" in t
    assert "Text only" in t and "d" in t.split("Text only")[1].splitlines()[0]


def test_dump_marks_unread_and_new_only(args, capsys):
    lib(args)
    assert run(args, ["--dump-abstracts"]) == 0
    out = capsys.readouterr().out
    assert "### c [unread: no-pdf]" in out and "### d [text-only]" in out and "### a\n" in out
    assert run(args, ["--dump-abstracts", "--new-only"]) == 0
    assert "### a" not in capsys.readouterr().out


def test_new_only_without_dump_still_writes_full_index(args):
    lib(args)
    assert run(args, ["--new-only"]) == 0
    t = (M.topic_dir(args) / "INDEX.md").read_text()
    assert "### a" in t
