"""Two more the merge-on-save got wrong, both found by review after the first round of fixes."""
import json
import manifest as M


def test_a_decision_survives_a_concurrent_creator(args):
    """Filling would keep disk's `found` and silently demote a paper this run seeded."""
    mine, theirs = M.load(args), M.load(args)
    theirs["papers"]["p"] = M.new_paper("p", title="DDPM", status="found",
                                        found_via=["snowball-back"])
    M.save(args, theirs)
    mine["papers"]["p"] = M.new_paper("p", title="DDPM", status="selected",
                                      why="seed: named in the brief", found_via=["seed"])
    M.save(args, mine)

    p = M.load(args)["papers"]["p"]
    assert p["status"] == "selected"                    # used to come back `found`
    assert p["why"] == "seed: named in the brief"       # while still carrying the seed's why
    assert sorted(p["found_via"]) == ["seed", "snowball-back"]


def test_a_manifest_hand_edited_mid_run_is_replaced_not_walked(args):
    """load() rejects a papers that is not a dict; the re-read inside save() must too."""
    m = M.load(args)
    M.save(args, m)
    path = M.manifest_path(args)

    for bad in (None, [], "x"):
        raw = json.loads(path.read_text())
        raw["papers"] = bad
        path.write_text(json.dumps(raw))
        m["papers"]["z"] = M.new_paper("z", title="z")
        M.save(args, m)                                  # used to raise AttributeError
        assert "z" in M.load(args)["papers"]
