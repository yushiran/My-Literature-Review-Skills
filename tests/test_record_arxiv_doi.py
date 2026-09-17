"""search.record: the arXiv id is recovered from the arXiv DataCite DOI when no source states it.

Seen 2026-09-17: `--seed arxiv:2503.11043` resolved through OpenAlex to a record with
doi 10.48550/arxiv.2503.11043 and arxiv '', so fetch.py had no arXiv route and left the paper
`selected` behind the stale institutional proxy.
"""
import search as S


def test_arxiv_id_comes_from_the_datacite_doi():
    r = S.record("openalex", title="t", doi="https://doi.org/10.48550/arXiv.2503.11043")
    assert r["doi"] == "10.48550/arxiv.2503.11043"
    assert r["arxiv"] == "2503.11043"


def test_a_stated_arxiv_id_wins_and_other_dois_add_nothing():
    assert S.record("s2", title="t", doi="10.48550/arxiv.2503.11043", arxiv="2503.11043v2")["arxiv"] == "2503.11043"
    assert S.record("openalex", title="t", doi="10.1109/TPAMI.2026.3676894")["arxiv"] == ""
    assert S.record("openalex", title="t")["arxiv"] == ""


def _hits(monkeypatch, titles):
    """openalex_by_title's one request, answered with these titles in citation order."""
    works = [{"id": f"https://openalex.org/W{i}", "title": t, "display_name": t, "cited_by_count": 100 - i}
             for i, t in enumerate(titles)]
    monkeypatch.setattr(S, "get_json_retry", lambda url, name: {"results": works})


def test_a_title_seed_is_never_a_guess(monkeypatch, capsys):
    """Both wrong seeds of 2026-09-17: a most-cited hit sharing a word or two is not the paper."""
    _hits(monkeypatch, ["Development and validation of a measure of emotional intelligence"])
    assert S.openalex_by_title("On the Measure of Intelligence", {}) is None
    assert "skipped" in capsys.readouterr().err
    _hits(monkeypatch, ["Structural models of primary cell walls in flowering plants: consistency of molecular "
                        "structure with the physical properties of the walls during growth"])
    assert S.openalex_by_title("Consistency Models", {}) is None


def test_a_near_identical_title_variant_is_accepted(monkeypatch):
    _hits(monkeypatch, ["NoProp: Training Neural Networks without Full Back-propagation or Full Forward-propagation"])
    r = S.openalex_by_title("NoProp: Training Neural Networks without Back-propagation or Forward-propagation", {})
    assert r and r["title"].startswith("NoProp")
    _hits(monkeypatch, ["Flow Matching for Generative Modeling"])
    assert S.openalex_by_title("Flow Matching for Generative Modeling", {})["title"] == "Flow Matching for Generative Modeling"
