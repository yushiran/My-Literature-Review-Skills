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
    assert run(args, []) == 0   # step 1 writes the titles file; step 2 triages it, as SKILL.md runs them
    (d / "triage.json").write_text(json.dumps({"keep": ["2025-a-p0"], "drop": ["2025-a-p1"], "undecided": ["2025-a-p2"]}))
    assert run(args, ["--only", str(d / "triage.json")]) == 0
    c = (d / "candidates.md").read_text()
    assert "2025-a-p1" not in c and "2025-a-p0" in c and "2025-a-p2" in c
    assert c.index("2025-a-p0") < c.index("2025-a-p2")
    t = (d / "candidates_titles.md").read_text()
    assert "2025-a-p1" in t   # --only restricts candidates.md only; the titles file stays unfiltered


def test_only_rejects_a_triage_file_with_no_recognisable_key(args):
    """A renamed key used to keep nothing silently: rank wrote an empty candidates.md and
    exited 4, while select.py over the same file rejected nothing and exited 0. It is now a
    usage error naming the file. A file carrying `drop` alone is still a valid triage that
    legitimately empties candidates.md, so the check must not fire on it."""
    lib(args)
    d = M.topic_dir(args)
    assert run(args, []) == 0
    before = (d / "candidates.md").read_text()
    bad = d / "triage.json"
    bad.write_text(json.dumps({"kept": ["2025-a-p0"], "dropped": ["2025-a-p1"]}))
    assert run(args, ["--only", str(bad)]) == M.EXIT_USAGE
    assert (d / "candidates.md").read_text() == before   # it refuses before writing anything
    bad.write_text(json.dumps({"drop": ["2025-a-p0", "2025-a-p1", "2025-a-p2"]}))
    assert run(args, ["--only", str(bad)]) == M.EXIT_NOTHING   # a real triage that kept nothing


def test_only_keeps_a_paper_ranked_below_the_top_cut(args):
    """SKILL.md ranks 120 titles for the scout and runs the post-triage command at the
    default 100, so a paper the scout read at rank 103 and kept must still reach
    candidates.md: the cut bounds what the scout reads, not what it may keep."""
    m = M.load(args)
    for i in range(105):
        p = M.new_paper(f"2025-a-p{i:03d}", title=f"Paper {i}", venue="ICML", year=2025, citations=i)
        m["papers"][p["id"]] = p
    M.save(args, m)
    d = M.topic_dir(args)
    assert run(args, ["--top", "120"]) == 0
    assert "## 103. 2025-a-p002" in (d / "candidates_titles.md").read_text()   # what the scout read
    (d / "triage.json").write_text(json.dumps({"keep": ["2025-a-p002"], "drop": [], "undecided": []}))
    assert run(args, ["--only", str(d / "triage.json")]) == 0
    assert "2025-a-p002" in (d / "candidates.md").read_text()


def test_only_leaves_the_titles_file_a_refresh_wrote_untouched(args):
    """pipeline.py --refresh ranks with --new-only, so candidates_titles.md holds only the
    new papers. SKILL.md's post-triage command carries --only but not --new-only, so it must
    not rewrite the file the scout has already triaged with the whole library."""
    m = lib(args)
    m["papers"]["2025-a-p0"]["found_date"] = "2000-01-01"
    m["refreshed"] = M.today()
    M.save(args, m)
    d = M.topic_dir(args)
    assert run(args, ["--new-only"]) == 0
    before = (d / "candidates_titles.md").read_bytes()
    assert b"2025-a-p0" not in before          # the refresh left the old paper out
    (d / "triage.json").write_text(json.dumps({"keep": ["2025-a-p1"], "drop": ["2025-a-p2"], "undecided": []}))
    assert run(args, ["--only", str(d / "triage.json")]) == 0
    assert (d / "candidates_titles.md").read_bytes() == before
    assert "2025-a-p1" in (d / "candidates.md").read_text()


def test_new_only_uses_refreshed_date(args):
    m = lib(args)
    m["papers"]["2025-a-p0"]["found_date"] = "2000-01-01"
    m["refreshed"] = M.today()
    M.save(args, m)
    assert run(args, ["--new-only"]) == 0
    assert "2025-a-p0" not in (M.topic_dir(args) / "candidates.md").read_text()
