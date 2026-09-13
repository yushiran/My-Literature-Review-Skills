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
    assert r["new_titles_already_rejected"] == 1 and r["returned"] == 2
    out = capsys.readouterr().out
    # 1 new of the 2 distinct titles returned: half the round was fresh, so no warning.
    assert "saturation: new=1 of returned=2 (50.0%)" in out
    assert "saturated" not in out


SEED_WORK = {"id": "https://openalex.org/W99", "doi": "", "ids": {}, "title": "A Canonical Seed Paper",
             "publication_year": 2018, "cited_by_count": 1500,
             "authorships": [{"author": {"display_name": "Yochai Blau"}}],
             "primary_location": {"source": {"display_name": "CVPR"}}, "best_oa_location": None,
             "locations": [], "abstract_inverted_index": {"We": [0]}, "relevance_score": None}


def test_seed_only_run_records_no_round(args, monkeypatch, capsys):
    monkeypatch.setattr(S, "get_json_retry",
                        lambda url, headers=None, name="x": {"results": [SEED_WORK]} if "works?" in url else SEED_WORK)
    monkeypatch.setattr(S, "arxiv_backfill", lambda m, cap=200: 0)
    sys.argv = ["search.py", "--topic", args.topic, "--root", args.root, "--seed", "A Canonical Seed Paper"]
    try:
        S.main()
    except SystemExit:
        pass
    m = M.load(args)
    # The seed landed, so the run worked; it just searched no axis, so there is nothing to report.
    assert len(m["papers"]) == 1 and "rounds" not in m
    assert "saturation:" not in capsys.readouterr().out
