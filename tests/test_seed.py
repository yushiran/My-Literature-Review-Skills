import sys
import manifest as M
import search as S

WORK = {"id": "https://openalex.org/W99", "doi": "https://doi.org/10.1/blau", "ids": {"doi": "https://doi.org/10.1/blau"},
        "title": "The Perception-Distortion Tradeoff", "publication_year": 2018, "cited_by_count": 1500,
        "authorships": [{"author": {"display_name": "Yochai Blau"}}], "primary_location": {"source": {"display_name": "CVPR"}},
        "best_oa_location": None, "locations": [], "abstract_inverted_index": {"We": [0], "prove": [1]}, "relevance_score": None,
        "referenced_works": []}


def run(args, argv, monkeypatch, urls):
    def fake_get(url, headers=None, name="x"):
        urls.append(url)
        return {"results": [WORK]} if "works?" in url else WORK
    monkeypatch.setattr(S, "get_json_retry", fake_get)
    monkeypatch.setattr(S, "arxiv_backfill", lambda m, cap=200: 0)
    sys.argv = ["search.py", "--topic", args.topic, "--root", args.root] + argv
    try:
        S.main()
    except SystemExit as e:
        return e.code


def test_seed_by_title_is_selected_and_exempt_from_since(args, monkeypatch):
    urls = []
    assert run(args, ["--seed", "The Perception-Distortion Tradeoff", "--since", "2024"], monkeypatch, urls) == 0
    m = M.load(args)
    (p,) = m["papers"].values()
    assert p["status"] == "selected" and p["found_via"] == ["seed"] and p["year"] == 2018
    assert m["seeds"] == ["The Perception-Distortion Tradeoff"]
    assert any("title.search" in u for u in urls)


def test_seed_by_doi_uses_the_works_endpoint(args, monkeypatch):
    urls = []
    assert run(args, ["--seed", "doi:10.1/blau"], monkeypatch, urls) == 0
    assert any(u.endswith("/works/https://doi.org/10.1/blau") or "doi.org/10.1/blau" in u for u in urls)


WORK2 = {"id": "https://openalex.org/W100", "doi": "", "ids": {},
         "title": "A Second Canonical Paper", "publication_year": 2019, "cited_by_count": 500,
         "authorships": [{"author": {"display_name": "Some Author"}}], "primary_location": {"source": {"display_name": "ICML"}},
         "best_oa_location": None, "locations": [], "abstract_inverted_index": {"An": [0], "abstract": [1]}, "relevance_score": None}


def test_seed_promotes_rejected_paper_but_leaves_advanced_status_alone(args, monkeypatch):
    m = M.load(args)
    # "a" was found by an earlier query round and rejected in triage; the seed must resurrect it.
    m["papers"]["a"] = M.new_paper("a", title="The Perception-Distortion Tradeoff",
                                    status="rejected", found_via=["query"])
    # "b" already went all the way to fetched markdown; a seed match must not revert its status.
    m["papers"]["b"] = M.new_paper("b", title="A Second Canonical Paper",
                                    status="md", found_via=["query"])
    M.save(args, m)

    def fake_get(url, headers=None, name="x"):
        if "works?" in url:
            return {"results": [WORK2]} if "Second" in url else {"results": [WORK]}
        return WORK
    monkeypatch.setattr(S, "get_json_retry", fake_get)
    monkeypatch.setattr(S, "arxiv_backfill", lambda m, cap=200: 0)
    sys.argv = ["search.py", "--topic", args.topic, "--root", args.root,
                "--seed", "The Perception-Distortion Tradeoff", "--seed", "A Second Canonical Paper"]
    try:
        S.main()
    except SystemExit as e:
        assert e.code == 0

    m = M.load(args)
    a, b = m["papers"]["a"], m["papers"]["b"]
    assert a["status"] == "selected" and a["why"] == "seed: named in the brief"
    assert set(a["found_via"]) == {"query", "seed"}
    assert b["status"] == "md"


def openalex_work(id_, title, **extra):
    """A minimal fake OpenAlex work dict for the arxiv: chain tests below."""
    base = {"id": id_, "doi": "", "ids": {}, "title": title, "publication_year": 2020,
            "cited_by_count": 1, "authorships": [], "primary_location": None,
            "best_oa_location": None, "locations": [], "abstract_inverted_index": None,
            "relevance_score": None}
    base.update(extra)
    return base


ARXIV_ENTRY_XML = (b'<feed xmlns="http://www.w3.org/2005/Atom">'
                    b'<entry><title>Denoising Diffusion Probabilistic Models</title></entry></feed>')
ARXIV_EMPTY_FEED_XML = b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'


def test_seed_arxiv_doi_hit_never_queries_arxiv(args, monkeypatch):
    doi_urls = []

    def fake_get(url, headers=None, name="x"):
        assert "works/https://doi.org/10.48550/arxiv.2010.02502" in url
        doi_urls.append(url)
        return openalex_work("https://openalex.org/W200", "Denoising Diffusion Probabilistic Models")

    def fake_http_get(url, headers=None):
        raise AssertionError(f"arXiv should not be queried when the DOI attempt already hit: {url}")

    monkeypatch.setattr(S, "get_json_retry", fake_get)
    monkeypatch.setattr(S, "http_get", fake_http_get)
    monkeypatch.setattr(S, "arxiv_backfill", lambda m, cap=200: 0)
    sys.argv = ["search.py", "--topic", args.topic, "--root", args.root, "--seed", "arxiv:2010.02502"]
    try:
        S.main()
    except SystemExit as e:
        assert e.code == 0
    (p,) = M.load(args)["papers"].values()
    assert p["status"] == "selected" and p["openalex"] == "W200"
    assert len(doi_urls) == 1


def test_seed_arxiv_doi_404_falls_through_to_arxiv_title(args, monkeypatch):
    # This is the case that was broken: get_json_retry raises SourceDown for OpenAlex's 404 on
    # an unindexed DOI, and that must not abort the chain before the arXiv-title step runs.
    def fake_get(url, headers=None, name="x"):
        if "works/https://doi.org/" in url:
            raise S.SourceDown("openalex 404 for 10.48550/arxiv.2010.02502")
        assert "Denoising" in url  # the title search must use arXiv's own title
        return {"results": [openalex_work("https://openalex.org/W201", "Denoising Diffusion Probabilistic Models")]}

    def fake_http_get(url, headers=None):
        assert "id_list=2010.02502" in url
        return ARXIV_ENTRY_XML

    monkeypatch.setattr(S, "get_json_retry", fake_get)
    monkeypatch.setattr(S, "http_get", fake_http_get)
    monkeypatch.setattr(S, "arxiv_backfill", lambda m, cap=200: 0)
    sys.argv = ["search.py", "--topic", args.topic, "--root", args.root, "--seed", "arxiv:2010.02502"]
    try:
        S.main()
    except SystemExit as e:
        assert e.code == 0
    (p,) = M.load(args)["papers"].values()
    assert p["status"] == "selected" and p["openalex"] == "W201"


def test_seed_arxiv_doi_404_and_arxiv_title_miss_is_a_clean_not_found(args, monkeypatch, capsys):
    def fake_get(url, headers=None, name="x"):
        if "works/https://doi.org/" in url:
            raise S.SourceDown("openalex 404 for 10.48550/arxiv.2010.02502")
        raise AssertionError(f"no title to search with, should not be reached: {url}")

    def fake_http_get(url, headers=None):
        return ARXIV_EMPTY_FEED_XML  # arXiv itself has no such id

    monkeypatch.setattr(S, "get_json_retry", fake_get)
    monkeypatch.setattr(S, "http_get", fake_http_get)
    monkeypatch.setattr(S, "arxiv_backfill", lambda m, cap=200: 0)
    sys.argv = ["search.py", "--topic", args.topic, "--root", args.root, "--seed", "arxiv:2010.02502"]
    try:
        S.main()
    except SystemExit as e:
        assert e.code == M.EXIT_NOTHING  # no query and the one seed came up empty
    assert M.load(args)["papers"] == {}
    assert "not found on OpenAlex" in capsys.readouterr().err


def test_seed_arxiv_doi_rate_limited_propagates_without_arxiv_fallback(args, monkeypatch, capsys):
    def fake_get(url, headers=None, name="x"):
        raise S.RateLimited(url)

    def fake_http_get(url, headers=None):
        raise AssertionError(f"arXiv should not be queried after a rate limit, that is not a miss: {url}")

    monkeypatch.setattr(S, "get_json_retry", fake_get)
    monkeypatch.setattr(S, "http_get", fake_http_get)
    monkeypatch.setattr(S, "arxiv_backfill", lambda m, cap=200: 0)
    sys.argv = ["search.py", "--topic", args.topic, "--root", args.root, "--seed", "arxiv:2010.02502"]
    try:
        S.main()
    except SystemExit as e:
        assert e.code == M.EXIT_NOTHING
    assert M.load(args)["papers"] == {}
    assert "lookup failed" in capsys.readouterr().err
