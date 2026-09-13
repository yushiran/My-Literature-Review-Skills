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


def test_two_sources_one_candidate_via_both_directions(args, monkeypatch):
    """A candidate reached by both a reference and a citation of the SAME source counts
    once, not twice (Important 1); the report table gets one row per source, not one row
    per hop direction (Important 3)."""
    m = M.load(args)
    a = M.new_paper("2025-a-source", title="Source A", openalex="W1", status="md")
    b = M.new_paper("2025-b-source", title="Source B", openalex="W2", status="md")
    m["papers"][a["id"]] = a
    m["papers"][b["id"]] = b
    M.save(args, m)

    def fake_get(url, headers=None, name="x"):
        url = urllib.parse.unquote(url)
        if "openalex:W1|W2" in url or "openalex:W2|W1" in url:  # both sources' reference lists, one batch
            return {"results": [work("W1", "Source A", refs=["W50"]), work("W2", "Source B", refs=["W60"])]}
        if "openalex:W50|W60" in url or "openalex:W60|W50" in url:  # backward metadata, one batch
            return {"results": [work("W50", "Both Directions", year=2015), work("W60", "Ref Only", year=2016)]}
        if "cites:W1" in url:  # W50 also cites its own reference W1 -- same source, both directions
            return {"results": [work("W50", "Both Directions", refs=["W1"])], "meta": {"count": 1}}
        if "cites:W2" in url:
            return {"results": [work("W70", "Citer Only", refs=["W2"])], "meta": {"count": 1}}
        raise AssertionError(url)
    monkeypatch.setattr(SB, "get_json_retry", fake_get)
    sys.argv = ["snowball.py", "--topic", args.topic, "--root", args.root, "--since", "2000"]
    assert SB.main() == 0

    m = M.load(args)
    titles = {p["title"]: p for p in m["papers"].values()}
    # I1: one source, reached through both directions, still counts as one hit.
    assert titles["Both Directions"]["snowball_hits"] == 1
    assert titles["Both Directions"]["found_via"] == ["snowball-back"]
    assert titles["Ref Only"]["snowball_hits"] == 1
    assert titles["Citer Only"]["snowball_hits"] == 1

    # I3: one row per source, each carrying its own reference count and its own citer count.
    md_text = (M.topic_dir(args) / "snowball.md").read_text()
    table_lines = [ln for ln in md_text.splitlines() if ln.startswith("| 2025-")]
    assert len(table_lines) == 2, table_lines
    rows = {ln.split("|")[1].strip(): [c.strip() for c in ln.split("|")] for ln in table_lines}
    assert rows["2025-a-source"][2:4] == ["1", "1"]  # W1: 1 reference (W50), 1 citer fetched (W50)
    assert rows["2025-b-source"][2:4] == ["1", "1"]  # W2: 1 reference (W60), 1 citer fetched (W70)
