import sys
import urllib.error
import manifest as M
import search as S


def http_429(url, *a, **k):
    raise urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)


def test_a_429_stops_every_arxiv_call_site_for_the_rest_of_the_run(args, monkeypatch, capsys):
    """arXiv meters by egress IP, so one 429 means the whole run is refused."""
    calls = []
    monkeypatch.setattr(S, "http_get", lambda url, *a, **k: calls.append(url) or http_429(url))
    monkeypatch.setattr(S, "search_openalex", lambda q, s, l: [])
    sys.argv = ["search.py", "--topic", args.topic, "--root", args.root,
                "--query", "q1", "--query", "q2", "--arxiv", "on"]
    try:
        S.main()
    except SystemExit:
        pass
    # q1 asks once and is refused; q2 must not ask at all, and neither must the backfill.
    assert len(calls) == 1, calls
    assert "arxiv=rate-limited" in capsys.readouterr().out   # not a bare arxiv=0, which also means "never asked"


def test_search_arxiv_does_not_retry_a_429(monkeypatch):
    calls = []
    monkeypatch.setattr(S, "http_get", lambda url, *a, **k: calls.append(url) or http_429(url))
    try:
        S.search_arxiv("flow matching", 2024, 10)
    except S.ArxivRateLimited:
        pass
    else:
        raise AssertionError("a 429 must raise ArxivRateLimited, not be retried into SourceDown")
    assert len(calls) == 1, f"retried a per-IP limit that retrying cannot clear: {calls}"


def test_arxiv_title_reports_the_limit_rather_than_a_dead_source(monkeypatch):
    """It is reached through lookup_seed's arxiv: chain, so it has to say which failure it is."""
    monkeypatch.setattr(S, "http_get", http_429)
    try:
        S.arxiv_title("2301.00001")
    except S.ArxivRateLimited:
        pass
    else:
        raise AssertionError("arxiv_title turned a 429 into SourceDown, so the run never learns to stop")
