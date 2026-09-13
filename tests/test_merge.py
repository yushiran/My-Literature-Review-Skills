import manifest as M
import search as S


def rec(title, **kw):
    base = dict(source="openalex", title=title, authors=["A Author"], year=2024, venue="ICML",
                doi="", arxiv="", openalex="", s2="", citations=1, abstract="x", pdf_url="", relevance=None)
    base.update(kw)
    return base


def test_new_paper_defaults():
    p = M.new_paper("2024-author-t")
    assert p["found_via"] == [] and p["snowball_hits"] == 0
    assert p["found_date"] == M.today()


def test_merge_sets_and_unions_found_via(args):
    m = M.load(args)
    assert S.merge(m, [rec("Flow matching for X", doi="10.1/a")], via="query") == 1
    (p,) = m["papers"].values()
    assert p["found_via"] == ["query"]
    assert S.merge(m, [rec("Flow matching for X", doi="10.1/a")], via="snowball-back") == 0
    assert p["found_via"] == ["query", "snowball-back"]


def test_fuzzy_title_ignores_hyphens_and_case():
    assert M.fuzzy_title("Multi-Modal Synthetic Data") == M.fuzzy_title("multimodal synthetic data")


def test_merge_joins_preprint_and_published_version(args):
    m = M.load(args)
    S.merge(m, [rec("Multi-Modal Synthetic Data for Fine-Tuning", venue="arXiv", year=2024,
                    authors=["Y Hu"], arxiv="2401.00001")])
    n = S.merge(m, [rec("Multimodal Synthetic Data for Fine-tuning", venue="ICMI", year=2025,
                        authors=["Yu Hu"], doi="10.1145/x")])
    assert n == 0 and len(m["papers"]) == 1
    (p,) = m["papers"].values()
    assert p["venue"] == "ICMI" and p["doi"] == "10.1145/x" and p["arxiv"] == "2401.00001"
