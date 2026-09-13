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


def test_one_hop_back_and_forward(args, monkeypatch):
    m = M.load(args)
    src = M.new_paper("2025-ab-source", title="Source", openalex="W1", status="md")
    m["papers"][src["id"]] = src
    M.save(args, m)

    def fake_get(url, headers=None, name="x"):
        # urlencode percent-encodes ":" and "|" in the filter value; decode before matching.
        url = urllib.parse.unquote(url)
        # Batch check first: "openalex:W1" is a substring of "openalex:W10|W11", so the
        # single-id branch below must not run first or it would also catch the batch call.
        if "openalex:W10|W11" in url or "openalex:W11|W10" in url:  # backward metadata
            return {"results": [work("W10", "Classic A", year=2018), work("W11", "Classic B", year=2012)]}
        if "openalex:W1" in url:                          # fetch the source's reference list
            return {"results": [work("W1", "Source", refs=["W10", "W11"])]}
        if "cites:W1" in url:                              # forward
            return {"results": [work("W20", "Follower", refs=["W1"])], "meta": {"count": 1}}
        raise AssertionError(url)
    monkeypatch.setattr(SB, "get_json_retry", fake_get)
    sys.argv = ["snowball.py", "--topic", args.topic, "--root", args.root, "--since", "2000"]
    assert SB.main() == 0
    m = M.load(args)
    titles = {p["title"]: p for p in m["papers"].values()}
    assert titles["Classic A"]["found_via"] == ["snowball-back"] and titles["Classic A"]["status"] == "found"
    assert titles["Follower"]["found_via"] == ["snowball-forward"]
    assert titles["Classic A"]["snowball_hits"] == 1
    assert m["papers"]["2025-ab-source"]["refs"] == ["W10", "W11"]
    assert (M.topic_dir(args) / "snowball.md").is_file()
    assert m["rounds"][-1]["kind"] == "snowball"
