"""The three ways the merge-on-save lost data, each found by review after it shipped."""
import manifest as M


def test_an_identical_rerun_still_records_its_round(args):
    """`rounds` is an append-only log and a byte-identical re-run is the saturated case."""
    entry = {"date": "2026-09-14", "kind": "search", "new": 0, "total_before": 3}
    for _ in range(2):
        m = M.load(args)
        m.setdefault("rounds", []).append(dict(entry))
        M.save(args, m)
    assert len(M.load(args)["rounds"]) == 2   # value-dedup kept only one


def test_two_writers_adding_one_id_keep_both_sides(args):
    """make_id is deterministic, so overlapping query axes land the same paper twice."""
    first, second = M.load(args), M.load(args)
    second["papers"]["p"] = M.new_paper("p", title="x", openalex="W1",
                                        snowball_hits=3, found_via=["snowball-back"])
    M.save(args, second)
    first["papers"]["p"] = M.new_paper("p", title="x", doi="10.1/x", found_via=["query"])
    M.save(args, first)

    p = M.load(args)["papers"]["p"]
    assert p["openalex"] == "W1"          # the loser's whole record used to replace the winner's
    assert p["snowball_hits"] == 3
    assert p["doi"] == "10.1/x"
    assert sorted(p["found_via"]) == ["query", "snowball-back"]


def test_a_list_edited_in_place_after_a_save_still_reaches_disk(args):
    """The snapshot must not alias the live value, or the next diff compares it to itself."""
    m = M.load(args)
    m["papers"]["q"] = M.new_paper("q", title="y")
    M.save(args, m)
    m["papers"]["q"]["found_via"] = ["query"]
    M.save(args, m)
    m["papers"]["q"]["found_via"].append("snowball-back")   # search.merge()'s idiom
    M.save(args, m)

    assert M.load(args)["papers"]["q"]["found_via"] == ["query", "snowball-back"]
