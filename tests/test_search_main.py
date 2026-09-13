import sys
import search as S


def fake_openalex(query, since, limit, n=1):
    return [dict(source="openalex", title=f"paper {i} about {query}", authors=["A B"], year=2025, venue="ICML",
                 doi="", arxiv="", openalex=f"W{i}", s2="", citations=3, abstract="an abstract", pdf_url="",
                 relevance=1.0) for i in range(n)]


def run(args, argv, monkeypatch, calls, n=1):
    monkeypatch.setattr(S, "search_openalex", lambda q, s, l: (calls.append(("openalex", q)), fake_openalex(q, s, l, n))[1])
    monkeypatch.setattr(S, "search_arxiv", lambda q, s, l: (calls.append(("arxiv", q)), [])[1])
    monkeypatch.setattr(S, "search_s2", lambda q, s, l: (calls.append(("s2", q)), [])[1])
    monkeypatch.setattr(S, "arxiv_backfill", lambda m, cap=200: 0)
    sys.argv = ["search.py", "--topic", args.topic, "--root", args.root] + argv
    try:
        S.main()
    except SystemExit as e:
        return e.code


def test_arxiv_auto_skips_when_openalex_has_enough_hits(args, monkeypatch):
    calls = []
    assert run(args, ["--query", "q1", "--query", "q2"], monkeypatch, calls, n=20) == 0
    assert ("arxiv", "q1") not in calls and ("s2", "q1") not in calls
    assert sum(1 for c in calls if c[0] == "openalex") == 2


def test_arxiv_auto_runs_when_openalex_is_thin(args, monkeypatch):
    calls = []
    assert run(args, ["--query", "q1"], monkeypatch, calls, n=1) == 0
    assert ("arxiv", "q1") in calls


def test_arxiv_on_and_s2_flag(args, monkeypatch):
    calls = []
    assert run(args, ["--query", "q1", "--arxiv", "on", "--s2"], monkeypatch, calls) == 0
    assert ("arxiv", "q1") in calls and ("s2", "q1") in calls
