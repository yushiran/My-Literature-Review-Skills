"""fetch.py routing: which hosts are throttled, and what a paper with no identifier does."""
import threading

import access as A
import fetch as F
import manifest as M

PROXY = "bris.idm.oclc.org"
WILEY = "https://onlinelibrary.wiley.com/doi/pdf/10.1002/nbm.1234"
SCIENCEDIRECT = "https://www.sciencedirect.com/science/article/pii/S000000/pdf"


def throttled(url):
    """True when fetch.py would hold this URL to one paced request at a time."""
    return isinstance(F.host_slot(F.host_key(url)), threading.Semaphore)


def test_the_proxied_route_is_throttled_like_the_publisher_it_fronts(monkeypatch):
    """The credential is the whole institution's, so the proxied route is the one that
    must not go out four at a time."""
    monkeypatch.setenv("LITREV_EZPROXY_HOST", PROXY)
    assert throttled(WILEY)
    assert throttled(A.ezproxy_host_rewrite(WILEY, PROXY))


def test_every_proxied_publisher_shares_one_bucket(monkeypatch):
    monkeypatch.setenv("LITREV_EZPROXY_HOST", PROXY)
    keys = {F.host_key(A.ezproxy_host_rewrite(u, PROXY)) for u in (WILEY, SCIENCEDIRECT)}
    assert keys == {"ezproxy"}
    assert F.HOST_LIMITS["ezproxy"] == 1 and F.HOST_PAUSE["ezproxy"] >= 1.0


def test_an_unrelated_host_is_still_unthrottled(monkeypatch):
    monkeypatch.setenv("LITREV_EZPROXY_HOST", PROXY)
    assert F.host_key("https://example.org/paper.pdf") == "example.org"
    assert not throttled("https://example.org/paper.pdf")


def test_without_a_proxy_configured_the_hostname_is_the_key():
    assert F.host_key(WILEY) == "onlinelibrary.wiley.com"
    assert F.host_key("https://example.org/paper.pdf") == "example.org"


def test_a_paper_with_no_identifier_reaches_the_no_oa_link_outcome(args, tmp_path):
    """No pdf_url, no arXiv id, no DOI: the candidate loop never runs, and the fallback
    chain in access.py must still be offered the URLs actually tried."""
    paper = M.new_paper("2025-x-none", title="No identifiers")
    r = F.fetch_one("2025-x-none", paper, tmp_path / "2025-x-none.pdf", 5)
    assert r["status"] == "no-pdf"
    assert r["error"] == "no OA link", r["error"]
