import sys
import manifest as M
import search as S


def rec(title):
    return dict(source="openalex", title=title, authors=["A B"], year=2025, venue="ICML", doi="", arxiv="",
                openalex="", s2="", citations=0, abstract="a", pdf_url="", relevance=None)


def test_round_is_recorded_and_rejected_titles_counted(args, monkeypatch, capsys):
    m = M.load(args)
    S.merge(m, [rec("Old rejected paper")])
    next(iter(m["papers"].values()))["status"] = "rejected"
    M.save(args, m)
    monkeypatch.setattr(S, "search_openalex", lambda q, s, l: [rec("Old Rejected Paper"), rec("Brand new paper")])
    # 2 OpenAlex hits < 20, so --arxiv auto would fire the real arXiv API and pollute recs.
    monkeypatch.setattr(S, "search_arxiv", lambda q, s, l: [])
    monkeypatch.setattr(S, "arxiv_backfill", lambda m, cap=200: 0)
    sys.argv = ["search.py", "--topic", args.topic, "--root", args.root, "--query", "q"]
    try:
        S.main()
    except SystemExit:
        pass
    m = M.load(args)
    (r,) = m["rounds"]
    assert r["new"] == 1 and r["total_before"] == 1 and r["kind"] == "search"
    assert r["new_titles_already_rejected"] == 1
    assert "saturation:" in capsys.readouterr().out
