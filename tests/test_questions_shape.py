"""`questions` is the one manifest field no script writes, so a hand edit can leave a
string where the schema says list. Both agent-facing renderers iterate it."""
import re
import sys

import index as IX
import manifest as M
import rank as R

STRING_Q = "How does the prior interact with the operator?"


def lib(args, questions, status="found"):
    """Three papers whose `questions` is whatever was passed. rank.py ranks `found`
    papers; index.py lists only papers past selection, so it needs `md`."""
    m = M.load(args)
    m["questions"] = questions
    for i in range(3):
        p = M.new_paper(f"2025-a-p{i}", title=f"Paper {i}", venue="ICML", year=2025,
                        citations=i, abstract=" ".join(f"w{j}" for j in range(60)), status=status)
        m["papers"][p["id"]] = p
    M.save(args, m)


def test_as_list_normalises_the_shapes_a_hand_edit_produces():
    assert M.as_list(["a", "b"]) == ["a", "b"]
    assert M.as_list(STRING_Q) == [STRING_Q]
    assert M.as_list("") == []
    assert M.as_list(None) == []
    assert M.as_list({"rq1": "x"}) == []   # anything else is not a question list


def test_index_renders_a_string_question_as_one_question(args, capsys):
    lib(args, STRING_Q, status="md")
    sys.argv = ["index.py", "--topic", args.topic, "--root", args.root, "--dump-abstracts"]
    assert IX.main() == 0
    numbered = [ln for ln in capsys.readouterr().out.splitlines() if re.match(r"^\d+\. ", ln)]
    assert numbered == [f"1. {STRING_Q}"]


def test_rank_renders_a_string_question_as_one_bullet(args):
    lib(args, STRING_Q)
    sys.argv = ["rank.py", "--topic", args.topic, "--root", args.root]
    assert R.main() == 0
    d = M.topic_dir(args)
    for name in ("candidates_titles.md", "candidates.md"):
        bullets = [ln for ln in (d / name).read_text().splitlines() if ln.startswith("- ")]
        assert bullets == [f"- {STRING_Q}"], name


def test_a_proper_list_of_questions_is_untouched(args, capsys):
    lib(args, ["RQ1 first", "RQ2 second"], status="md")
    sys.argv = ["index.py", "--topic", args.topic, "--root", args.root, "--dump-abstracts"]
    assert IX.main() == 0
    out = capsys.readouterr().out
    assert "1. RQ1 first" in out and "2. RQ2 second" in out
