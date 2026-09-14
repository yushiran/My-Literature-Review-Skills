"""index.py: the empty --new-only dump, the untriaged count, and the ignored flag."""
import sys

import index as IX
import manifest as M


def run(args, argv):
    sys.argv = ["index.py", "--topic", args.topic, "--root", args.root] + argv
    return IX.main()


def seed(args, found=0):
    """One indexed paper older than the refresh stamp, plus `found` untriaged papers."""
    m = M.load(args)
    p = M.new_paper("a", title="a", status="md", abstract="abs")
    p["found_date"] = "2000-01-01"
    m["papers"]["a"] = p
    for i in range(found):
        m["papers"][f"f{i}"] = M.new_paper(f"f{i}", title=f"f{i}", status="found")
    m["refreshed"] = M.today()
    M.save(args, m)


def test_an_empty_new_only_dump_says_so_instead_of_exiting_ok(args, capsys):
    """rank.py exits 4 with a reason for the same condition. A silent 0 hands the librarian
    an empty delta while the pipeline reports success."""
    seed(args)
    assert run(args, ["--dump-abstracts", "--new-only"]) == M.EXIT_NOTHING
    assert "--new-only" in capsys.readouterr().err


def test_untriaged_found_papers_are_counted_in_the_header(args):
    """`found` is not a listed state, and after a refresh rank --new-only skips anything
    found before the stamp, so nothing else in the loop ever mentions these papers."""
    seed(args, found=3)
    assert run(args, []) == M.EXIT_OK
    assert "3 papers found but never triaged" in (M.topic_dir(args) / "INDEX.md").read_text()


def test_no_untriaged_line_when_every_paper_was_triaged(args):
    seed(args)
    assert run(args, []) == M.EXIT_OK
    assert "never triaged" not in (M.topic_dir(args) / "INDEX.md").read_text()


def test_new_only_without_dump_abstracts_says_it_is_ignored(args, capsys):
    seed(args)
    assert run(args, ["--new-only"]) == M.EXIT_OK
    assert "--dump-abstracts" in capsys.readouterr().err
