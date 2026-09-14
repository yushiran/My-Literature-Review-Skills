"""What the citation graph does with ids that are not the ones we asked for.

Merged works, null references, papers already in the library, and candidates the
merge joins onto a paper carrying a different OpenAlex id.
"""
import sys
import urllib.parse
import manifest as M
import snowball as SB


def work(wid, title, refs=(), year=2025):
    return {"id": f"https://openalex.org/{wid}", "doi": "", "ids": {}, "title": title, "publication_year": year,
            "cited_by_count": 5, "authorships": [{"author": {"display_name": "A B"}}],
            "primary_location": {"source": {"display_name": "ICML"}}, "best_oa_location": None, "locations": [],
            "abstract_inverted_index": {"x": [0]}, "relevance_score": None,
            "referenced_works": [f"https://openalex.org/{r}" for r in refs]}


def source(pid, wid, title):
    return M.new_paper(pid, title=title, openalex=wid, status="md")


def run(args):
    sys.argv = ["snowball.py", "--topic", args.topic, "--root", args.root, "--since", "2000"]
    return SB.main()


def test_a_work_answered_under_its_canonical_id_still_lands(args, monkeypatch):
    """W9old was asked for and W9can came back, because OpenAlex merged the two. The
    record belongs to neither kind under the id it carries, and used to vanish."""
    m = M.load(args)
    m["papers"]["2025-ab-source"] = source("2025-ab-source", "W1", "Source")
    M.save(args, m)

    def fake_get(url, headers=None, name="x"):
        url = urllib.parse.unquote(url)
        if "referenced_works" in url:
            return {"results": [work("W1", "Source", refs=["W9old"])]}
        if "cites:W1" in url:
            return {"results": []}
        if "openalex:W9old" in url:
            return {"results": [work("W9can", "Merged Classic", year=2016)]}
        raise AssertionError(url)

    monkeypatch.setattr(SB, "get_json_retry", fake_get)
    assert run(args) == 0
    titles = {p["title"]: p for p in M.load(args)["papers"].values()}
    assert "Merged Classic" in titles, "the record answered under another id was dropped"
    assert titles["Merged Classic"]["found_via"] == ["snowball-back"]
    assert titles["Merged Classic"]["snowball_hits"] == 1


def test_a_known_paper_crossing_two_hits_is_counted_and_reported(args, monkeypatch, capsys):
    """One hit in round one, a second source in round two. The paper is in the library by
    then, so it is never re-fetched, and the round's own report used to omit it."""
    m = M.load(args)
    m["papers"]["2025-a-source"] = source("2025-a-source", "W1", "Source A")
    M.save(args, m)

    def fake_get(url, headers=None, name="x"):
        url = urllib.parse.unquote(url)
        if "referenced_works" in url:
            asked_for = [w for w in ("W1", "W2") if f"openalex:{w}" in url or f"|{w}" in url]
            return {"results": [work(w, f"Source {w}", refs=["W50"]) for w in asked_for]}
        if "cites:W1" in url or "cites:W2" in url:
            return {"results": []}
        if "openalex:W50" in url:
            return {"results": [work("W50", "Shared Classic", year=2015)]}
        raise AssertionError(url)

    monkeypatch.setattr(SB, "get_json_retry", fake_get)
    assert run(args) == 0
    capsys.readouterr()                       # round one lands W50 with one hit

    m = M.load(args)
    m["papers"]["2025-b-source"] = source("2025-b-source", "W2", "Source B")
    M.save(args, m)
    assert run(args) == 0

    out = capsys.readouterr().out
    assert "hits2plus=1" in out, out
    md = (M.topic_dir(args) / "snowball.md").read_text()
    assert "- 2 hits · W50 · Shared Classic" in md, md
    shared = [p for p in M.load(args)["papers"].values() if p["title"] == "Shared Classic"]
    assert shared[0]["snowball_hits"] == 2


def test_a_source_with_no_references_is_fetched_once(args, monkeypatch):
    """An empty reference list is an answer, not a missing one; asking again every round
    spends a credit per source per round for nothing."""
    m = M.load(args)
    m["papers"]["2025-ab-source"] = source("2025-ab-source", "W1", "Source")
    M.save(args, m)
    meta_calls = []

    def fake_get(url, headers=None, name="x"):
        url = urllib.parse.unquote(url)
        if "referenced_works" in url:
            meta_calls.append(url)
            return {"results": [work("W1", "Source", refs=[])]}
        if "cites:W1" in url:
            return {"results": []}
        raise AssertionError(url)

    monkeypatch.setattr(SB, "get_json_retry", fake_get)
    assert run(args) == 0
    assert run(args) == 0
    assert len(meta_calls) == 1, "a source with no references is re-fetched every round"


def test_a_null_reference_creates_no_empty_id(args, monkeypatch):
    """A null inside referenced_works used to shorten to "", which then collected hits and
    handed them to every paper that carries no OpenAlex id."""
    m = M.load(args)
    m["papers"]["2025-ab-source"] = source("2025-ab-source", "W1", "Source")
    m["papers"]["2025-xy-nooa"] = M.new_paper("2025-xy-nooa", title="No OpenAlex Id", status="found")
    M.save(args, m)
    asked = []

    def fake_get(url, headers=None, name="x"):
        url = urllib.parse.unquote(url)
        asked.append(url)
        if "referenced_works" in url:
            w = work("W1", "Source")
            w["referenced_works"] = [None, "", "https://openalex.org/W10"]
            return {"results": [w]}
        if "cites:W1" in url:
            return {"results": []}
        if "W10" in url:
            return {"results": [work("W10", "Classic", year=2018)]}
        raise AssertionError(url)

    monkeypatch.setattr(SB, "get_json_retry", fake_get)
    assert run(args) == 0
    m = M.load(args)
    assert m["papers"]["2025-ab-source"]["refs"] == ["W10"]
    assert m["papers"]["2025-xy-nooa"]["snowball_hits"] == 0
    assert not [u for u in asked if "openalex:|" in u or u.rstrip("&").endswith("openalex:")], asked


def test_hits_reach_a_paper_merged_under_a_different_openalex_id(args, monkeypatch):
    """The library already holds the reference under a duplicate OpenAlex record, so the
    merge joins by title and the paper keeps its own id. The hits must follow it."""
    m = M.load(args)
    m["papers"]["2025-ab-source"] = source("2025-ab-source", "W1", "Source")
    m["papers"]["2015-cd-classic"] = M.new_paper("2015-cd-classic", title="Shared Classic",
                                                 openalex="W_DUP", year=2015, status="found")
    M.save(args, m)

    def fake_get(url, headers=None, name="x"):
        url = urllib.parse.unquote(url)
        if "referenced_works" in url:
            return {"results": [work("W1", "Source", refs=["W50"])]}
        if "cites:W1" in url:
            return {"results": []}
        if "openalex:W50" in url:
            return {"results": [work("W50", "Shared Classic", year=2015)]}
        raise AssertionError(url)

    monkeypatch.setattr(SB, "get_json_retry", fake_get)
    assert run(args) == 0
    m = M.load(args)
    assert len(m["papers"]) == 2, "the reference should merge onto the existing paper"
    assert m["papers"]["2015-cd-classic"]["snowball_hits"] == 1
