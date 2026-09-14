"""Hand-edited manifest fields, where a list is expected and a string is what is there.

A bare string iterates one character at a time, so `queries` as a string becomes one
--query flag per character on a subprocess command line, and `q not in queries` becomes
a substring test. Also the seed promotion, which matched an empty OpenAlex id.
"""
import sys
from pathlib import Path
import manifest as M
import pipeline as P
import search as S

SEED_WORK = {"id": "", "doi": "", "ids": {}, "title": "The Perception-Distortion Tradeoff",
             "publication_year": 2018, "cited_by_count": 1500,
             "authorships": [{"author": {"display_name": "Yochai Blau"}}],
             "primary_location": {"source": {"display_name": "CVPR"}}, "best_oa_location": None,
             "locations": [], "abstract_inverted_index": {"We": [0]}, "relevance_score": None}


def fake_run(ran, stdout=""):
    def run(cmd, **kw):
        ran.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": stdout})()
    return run


def run_search(args, argv, monkeypatch, results=()):
    def fake_get(url, headers=None, name="x"):
        return {"results": list(results), "meta": {"count": len(results)}}
    monkeypatch.setattr(S, "get_json_retry", fake_get)
    sys.argv = ["search.py", "--topic", args.topic, "--root", args.root, "--arxiv", "off"] + argv
    try:
        S.main()
    except SystemExit as e:
        return e.code


def test_refresh_treats_a_string_queries_field_as_one_query(args, monkeypatch):
    m = M.load(args)
    m["queries"] = "flow matching mri"     # hand-edited: a string where a list belongs
    m["created"] = "2024-03-01"
    M.save(args, m)
    ran = []
    monkeypatch.setattr(P.subprocess, "run", fake_run(ran))
    sys.argv = ["pipeline.py", "--topic", args.topic, "--root", args.root, "--refresh"]
    assert P.main() in (M.EXIT_OK, M.EXIT_NOTHING)
    search_cmd = ran[0]
    assert Path(search_cmd[1]).name == "search.py"
    assert search_cmd.count("--query") == 1, search_cmd
    assert "flow matching mri" in search_cmd


def test_search_stores_a_string_queries_field_without_splitting_it(args, monkeypatch):
    m = M.load(args)
    m["queries"] = "flow matching"
    M.save(args, m)
    assert run_search(args, ["--query", "new axis", "--since", "2020"], monkeypatch) == M.EXIT_NOTHING
    assert M.load(args)["queries"] == ["flow matching", "new axis"]


def test_search_stores_a_string_seeds_field_without_splitting_it(args, monkeypatch):
    m = M.load(args)
    m["seeds"] = "an earlier seed"
    M.save(args, m)
    assert run_search(args, ["--seed", "The Perception-Distortion Tradeoff"], monkeypatch, [SEED_WORK]) == 0
    assert M.load(args)["seeds"] == ["an earlier seed", "The Perception-Distortion Tradeoff"]


def test_a_seed_without_an_openalex_id_does_not_promote_a_stranger(args, monkeypatch):
    """An empty id compared equal to the empty id of the first paper that had none, and
    that paper was promoted to selected with the seed's reason."""
    m = M.load(args)
    m["papers"]["u"] = M.new_paper("u", title="An Unrelated Paper", status="found")
    m["papers"]["t"] = M.new_paper("t", title="The Perception-Distortion Tradeoff", status="found")
    M.save(args, m)
    assert run_search(args, ["--seed", "The Perception-Distortion Tradeoff"], monkeypatch, [SEED_WORK]) == 0
    m = M.load(args)
    assert m["papers"]["u"]["status"] == "found", "a paper nobody seeded was promoted"
    assert m["papers"]["t"]["status"] == "selected"
    assert m["papers"]["t"]["why"] == "seed: named in the brief"
