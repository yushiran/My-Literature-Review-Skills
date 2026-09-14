"""hits2plus must count papers, not ids.

A quarter of the ids in referenced_works resolve to nothing, measured on a live run,
and merge() drops a record with no title. Counting the id makes the two-hit gate
report a candidate that reached no paper at all.
"""
import sys
import urllib.parse
import manifest as M
import snowball as SB

from test_snowball_graph import work, source, run


def two_sources(args):
    m = M.load(args)
    for pid, wid in (("2025-a-source", "W1"), ("2025-b-source", "W2")):
        m["papers"][pid] = source(pid, wid, f"Source {wid}")
    M.save(args, m)


def both_reference(w50_answer):
    """Both sources cite W50; the caller decides what OpenAlex says about W50."""
    def fake_get(url, headers=None, name="x"):
        url = urllib.parse.unquote(url)
        if "referenced_works" in url:
            asked = [w for w in ("W1", "W2") if f"openalex:{w}" in url or f"|{w}" in url]
            return {"results": [work(w, f"Source {w}", refs=["W50"]) for w in asked]}
        if "cites:W1" in url or "cites:W2" in url:
            return {"results": []}
        if "openalex:W50" in url:
            return {"results": w50_answer}
        raise AssertionError(url)
    return fake_get


def test_an_unresolvable_id_is_not_counted_as_a_two_hit_candidate(args, monkeypatch, capsys):
    two_sources(args)
    monkeypatch.setattr(SB, "get_json_retry", both_reference([]))   # OpenAlex knows nothing about W50
    assert run(args) == 0

    assert "hits2plus=0" in capsys.readouterr().out    # used to say 1, for a paper that does not exist
    assert all((p.get("snowball_hits") or 0) == 0 for p in M.load(args)["papers"].values())


def test_a_titleless_record_is_not_counted_either(args, monkeypatch, capsys):
    """merge() refuses a record with no title, so it never becomes a paper."""
    two_sources(args)
    monkeypatch.setattr(SB, "get_json_retry", both_reference([work("W50", "", year=2015)]))
    assert run(args) == 0

    assert "hits2plus=0" in capsys.readouterr().out


def test_a_candidate_that_did_land_is_still_counted(args, monkeypatch, capsys):
    """The gate must keep working; this is the case the count exists for."""
    two_sources(args)
    monkeypatch.setattr(SB, "get_json_retry", both_reference([work("W50", "Shared Classic", year=2015)]))
    assert run(args) == 0

    assert "hits2plus=1" in capsys.readouterr().out
    landed = [p for p in M.load(args)["papers"].values() if p.get("title") == "Shared Classic"]
    assert landed and landed[0]["snowball_hits"] == 2


def test_two_ids_resolving_to_one_paper_count_once(args, monkeypatch, capsys):
    """OpenAlex merges works, so one paper can carry two ids. hits2plus was routed
    through pid_of to count papers, but still iterated ids, so it counted them twice."""
    two_sources(args)
    m = M.load(args)
    # One library paper already carries W50; the graph reaches it under W50 and W51.
    # `found` keeps it out of DEFAULT_FROM, so it is a candidate and not a third source.
    m["papers"]["2015-x-shared"] = M.new_paper("2015-x-shared", title="Shared Classic",
                                               openalex="W50", status="found")
    M.save(args, m)

    def fake_get(url, headers=None, name="x"):
        url = urllib.parse.unquote(url)
        if "referenced_works" in url:
            asked = [w for w in ("W1", "W2") if f"openalex:{w}" in url or f"|{w}" in url]
            return {"results": [work(w, f"Source {w}", refs=["W50", "W51"]) for w in asked]}
        if "cites:W1" in url or "cites:W2" in url:
            return {"results": []}
        if "openalex:W51" in url or "openalex:W50" in url:
            return {"results": [work("W51", "Shared Classic", year=2015)]}   # merges onto the same paper by title
        raise AssertionError(url)

    monkeypatch.setattr(SB, "get_json_retry", fake_get)
    assert run(args) == 0
    assert "hits2plus=1" in capsys.readouterr().out   # two ids, one paper: counted 2 before
