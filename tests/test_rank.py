import json
import sys
import manifest as M
import rank as R


def lib(args, n=3):
    m = M.load(args)
    for i in range(n):
        p = M.new_paper(f"2025-a-p{i}", title=f"Paper {i}", venue="ICML", year=2025, citations=i,
                        abstract=" ".join(f"w{j}" for j in range(60)))
        m["papers"][p["id"]] = p
    M.save(args, m)
    return m


def run(args, argv):
    sys.argv = ["rank.py", "--topic", args.topic, "--root", args.root] + argv
    return R.main()


def test_titles_file_has_first_thirty_words_and_via(args):
    lib(args)
    assert run(args, []) == 0
    t = (M.topic_dir(args) / "candidates_titles.md").read_text()
    assert "## 1. " in t and "via:query" in t
    first = t.split("**Paper")[1]
    assert "w29" in first and "w30" not in first


def test_only_restricts_candidates_and_snowball_bonus_orders(args):
    m = lib(args)
    m["papers"]["2025-a-p0"]["snowball_hits"] = 3     # least cited, but most connected
    M.save(args, m)
    d = M.topic_dir(args)
    (d / "triage.json").write_text(json.dumps({"keep": ["2025-a-p0"], "drop": ["2025-a-p1"], "undecided": ["2025-a-p2"]}))
    assert run(args, ["--only", str(d / "triage.json")]) == 0
    c = (d / "candidates.md").read_text()
    assert "2025-a-p1" not in c and "2025-a-p0" in c and "2025-a-p2" in c
    assert c.index("2025-a-p0") < c.index("2025-a-p2")


def test_new_only_uses_refreshed_date(args):
    m = lib(args)
    m["papers"]["2025-a-p0"]["found_date"] = "2000-01-01"
    m["refreshed"] = M.today()
    M.save(args, m)
    assert run(args, ["--new-only"]) == 0
    assert "2025-a-p0" not in (M.topic_dir(args) / "candidates.md").read_text()
