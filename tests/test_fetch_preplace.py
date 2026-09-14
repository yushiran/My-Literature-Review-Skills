"""fetch.py's pre-place pass, and the order it hands tried URLs to the fallback routes."""
import sys

import access as A
import fetch as F
import manifest as M


def run(args, argv=()):
    sys.argv = ["fetch.py", "--topic", args.topic, "--root", args.root] + list(argv)
    return F.main()


def test_a_failed_paper_keeps_its_conversion_error(args):
    """`failed` is convert.py's verdict on a pdf fetch itself downloaded, so reading that
    same file back as hand-placed erases why the conversion died and retries it for ever."""
    m = M.load(args)
    m["papers"]["a"] = M.new_paper("a", title="a", status="failed",
                                   error="mineru: timeout after 600s")
    M.save(args, m)
    tdir = M.topic_dir(args)
    (tdir / "pdf").mkdir(exist_ok=True)
    (tdir / "pdf" / "a.pdf").write_bytes(b"%PDF-1.7\n")
    assert run(args) == M.EXIT_NOTHING
    p = M.load(args)["papers"]["a"]
    assert p["status"] == "failed"
    assert p["error"] == "mineru: timeout after 600s"


def test_a_hand_placed_pdf_for_a_no_pdf_paper_is_still_accepted(args):
    """The pre-place pass keeps the states where a file on disk really is the user's doing."""
    m = M.load(args)
    m["papers"]["a"] = M.new_paper("a", title="a", status="no-pdf", error="paywall")
    M.save(args, m)
    tdir = M.topic_dir(args)
    (tdir / "pdf").mkdir(exist_ok=True)
    (tdir / "pdf" / "a.pdf").write_bytes(b"%PDF-1.7\n")
    assert run(args) == M.EXIT_OK
    p = M.load(args)["papers"]["a"]
    assert p["status"] == "pdf" and p["error"] == ""


def test_the_fallback_routes_get_the_urls_in_the_order_they_were_tried(monkeypatch, tmp_path):
    """access.candidates rewrites known_urls into proxy candidates in order, so an unordered
    container routes the same paper differently per process and records a different pdf_via."""
    urls = [f"https://example.org/{i}.pdf" for i in range(8)]
    monkeypatch.setattr(F, "candidate_urls",
                        lambda paper: [(f"v{i}", u, {}) for i, u in enumerate(urls)])

    def refuse(fn, url, timeout, what):
        raise F.Refused("403")

    monkeypatch.setattr(F, "with_retries", refuse)
    seen = []
    monkeypatch.setattr(A, "candidates", lambda *a, **kw: (seen.append(kw["known_urls"]), [])[1])
    r = F.fetch_one("a", M.new_paper("a"), tmp_path / "a.pdf", 5)
    assert r["status"] == "no-pdf"
    assert list(seen[0]) == urls
