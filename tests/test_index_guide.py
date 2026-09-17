"""index.py: the coverage block, the terms:/checked: gap search, the id gate on --guide, --grep, --ids.

The defect these guard against, seen on a real library of 2392 papers: the librarian was shown 53
abstracts and wrote "that comparison exists nowhere in this library" while two untriaged abstracts
that matched the claim sat in the manifest. Every count here comes from the manifest, never from
the model.
"""
import sys

import index as IX
import manifest as M


def run(args, argv):
    sys.argv = ["index.py", "--topic", args.topic, "--root", args.root] + argv
    return IX.main()


def lib(args):
    """Two papers past selection, one untriaged and one rejected that both mention 'controlled comparison'."""
    m = M.load(args)
    for pid, st, abstract in [
        ("2021-freirich-theory-distortion", "md", "A theory of the distortion-perception tradeoff."),
        ("2024-ohayon-posterior-mean", "selected", "Posterior-mean rectified flow beats baselines."),
        ("2026-zou-precise-sde", "found", "A controlled comparison of estimators on one prior."),
        ("2025-wu-adaptive-lambda", "rejected", "Controlled comparisons across estimator families."),
        ("2020-nobody-unrelated-work", "found", "Something else entirely."),
    ]:
        m["papers"][pid] = M.new_paper(pid, title=pid.replace("-", " "), status=st, abstract=abstract)
    M.save(args, m)


def write_guide(args, text):
    path = M.topic_dir(args) / "guide.md"
    path.write_text(text)
    return str(path)


def test_dump_opens_with_the_coverage_block(args, capsys):
    lib(args)
    assert run(args, ["--dump-abstracts"]) == 0
    out = capsys.readouterr().out
    assert "Coverage: this dump holds 2 of the 5 papers in the library (1 md, 1 selected)." in out
    assert "Not shown: 2 found (never triaged) and 1 rejected; 5 of the 5 carry an abstract." in out
    assert "searches the other 3 for it" in out
    assert out.index("Coverage:") < out.index("### ")


def test_terms_line_gets_a_checked_line_naming_the_unseen_matches(args):
    lib(args)
    guide = write_guide(args, "## Gaps\n\nNo paper here runs a controlled comparison.\n"
                              "terms: controlled comparison; estimator|prior\n")
    assert run(args, ["--guide", guide]) == 0
    t = (M.topic_dir(args) / "INDEX.md").read_text()
    assert "_Guide scope: written from the abstracts of the 2 papers past selection; 2 found (never triaged) " \
           "and 1 rejected papers were not read for it" in t
    line = [l for l in t.splitlines() if l.startswith("checked:")][0]
    assert line.startswith("checked: 5 papers searched; 0 in the guide's set carry these terms, 2 outside it: ")
    assert "2026-zou-precise-sde (found)" in line and "2025-wu-adaptive-lambda (rejected)" in line
    assert "2020-nobody" not in line


def test_no_match_says_none_and_the_and_semantics_hold(args):
    lib(args)
    guide = write_guide(args, "terms: controlled comparison; sde; lambda\n")   # no paper carries all three
    assert run(args, ["--guide", guide]) == 0
    t = (M.topic_dir(args) / "INDEX.md").read_text()
    assert "checked: 5 papers searched; 0 in the guide's set carry these terms, none outside it" in t


def test_rerender_recomputes_rather_than_stacking_generated_lines(args):
    lib(args)
    guide = write_guide(args, "terms: controlled comparison\n")
    assert run(args, ["--guide", guide]) == 0
    # A plain re-render reads the block back from INDEX.md, drops the generated lines, and regenerates.
    assert run(args, []) == 0
    t = (M.topic_dir(args) / "INDEX.md").read_text()
    assert t.count("checked:") == 1 and t.count("_Guide scope:") == 1
    # The guide file itself is the librarian's and stays untouched.
    assert "checked:" not in open(guide).read()


def test_guide_without_terms_is_folded_with_a_warning(args, capsys):
    lib(args)
    guide = write_guide(args, "## Gaps\n\nNothing here settles RQ1.\n")
    assert run(args, ["--guide", guide]) == 0
    assert "no `terms:` line" in capsys.readouterr().err
    assert "Nothing here settles RQ1." in (M.topic_dir(args) / "INDEX.md").read_text()


def test_an_id_the_manifest_does_not_hold_is_refused(args, capsys):
    lib(args)
    guide = write_guide(args, "See 2021-freirich-theory-distortion and 2023-invented-paper-here.\n")
    assert run(args, ["--guide", guide]) == M.EXIT_USAGE
    err = capsys.readouterr().err
    assert "2023-invented-paper-here" in err and "refused" in err
    assert not (M.topic_dir(args) / "INDEX.md").exists()


def test_ids_outside_the_dumped_set_are_kept_with_a_warning(args, capsys):
    lib(args)
    guide = write_guide(args, "2026-zou-precise-sde argues the same, and nd-anon-untitled is not a paper.\n")
    m = M.load(args)
    m["papers"]["nd-anon-untitled"] = M.new_paper("nd-anon-untitled", title="x", status="found")
    M.save(args, m)
    assert run(args, ["--guide", guide]) == 0
    err = capsys.readouterr().err
    assert "outside the set" in err and "2026-zou-precise-sde" in err and "nd-anon-untitled" in err


def test_grep_lists_every_state_and_exits_4_on_nothing(args, capsys):
    lib(args)
    assert run(args, ["--grep", "controlled comparison"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [l.split("\t")[:2] for l in out] == [["2026-zou-precise-sde", "found"], ["2025-wu-adaptive-lambda", "rejected"]]
    assert run(args, ["--grep", "no such phrase anywhere"]) == M.EXIT_NOTHING
    assert run(args, ["--grep", " ; "]) == M.EXIT_USAGE


def test_dump_ids_takes_any_status_and_refuses_an_unknown_id(args, capsys):
    lib(args)
    assert run(args, ["--dump-abstracts", "--ids", "2025-wu-adaptive-lambda", "2026-zou-precise-sde"]) == 0
    out = capsys.readouterr().out
    assert "### 2025-wu-adaptive-lambda" in out and "### 2026-zou-precise-sde" in out
    assert "chosen by --ids" in out and "### 2021-freirich" not in out
    assert run(args, ["--dump-abstracts", "--ids", "2026-zou-precise-sde", "1999-no-such-id"]) == M.EXIT_USAGE
    assert "1999-no-such-id" in capsys.readouterr().err


def test_parse_terms_and_ids():
    assert IX.parse_terms("Perception-Distortion; frontier | trade-off ;; characteri*") == \
        [["perception distortion"], ["frontier", "trade off"], ["characteri*"]]
    assert IX.guide_ids("cites 2024-sun-perception-distortion-balanced-super*, 2021-2022, and 2020-era-methods-") == \
        ["2020-era-methods", "2024-sun-perception-distortion-balanced-super"]


def test_term_matching_is_whole_word_with_fused_and_plural_forms(args):
    """The first real run of the check matched `fid` against every abstract that said fidelity."""
    m = M.load(args)
    for pid, abstract in [("2020-a-fidelity-paper", "High fidelity samples with bounded error."),
                          ("2021-b-fid-paper", "We report FID and LPIPS on a frontier."),
                          ("2022-c-tradeoff-paper", "The tradeoff is characterized in closed form."),
                          ("2023-d-trade-paper", "Trade-offs between estimators and bounds.")]:
        m["papers"][pid] = M.new_paper(pid, title=pid, status="found", abstract=abstract)
    M.save(args, m)
    m = M.load(args)
    got = lambda spec: sorted(p["id"] for p in IX.matches(m, IX.parse_terms(spec)))
    assert got("fid") == ["2021-b-fid-paper"]                       # not fidelity
    assert got("trade-off") == ["2022-c-tradeoff-paper", "2023-d-trade-paper"]   # fused, spaced, hyphenated
    assert got("characteri*") == ["2022-c-tradeoff-paper"]          # prefix on request only
    assert got("characteri") == []
    assert got("bound") == ["2023-d-trade-paper"]                   # plural yes, `bounded` no
    assert got("estimator; bound|frontier") == ["2023-d-trade-paper"]
