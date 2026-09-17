import sys
import manifest as M
import convert as C


def one_paper(args, pid="p1", **fields):
    """A `pdf` paper with an arXiv id and a file on disk, so every route has a source."""
    m = M.load(args)
    d = M.topic_dir(args)
    (d / "pdf").mkdir(exist_ok=True)
    m["papers"][pid] = M.new_paper(pid, title=pid, status="pdf", arxiv="2501.00001", **fields)
    (d / "pdf" / f"{pid}.pdf").write_bytes(b"%PDF-1.4 fake")
    M.save(args, m)
    return m


def argv(args, *extra):
    sys.argv = ["convert.py", "--topic", args.topic, "--root", args.root, "--jobs", "1", *extra]


def recorder(calls, succeed_on=("local",)):
    def fake_convert_one(pid, mode, source, out_dir, a, abort):
        calls.append(mode)
        if mode in succeed_on:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{pid}.md").write_text("x" * 5000)
            r = {"id": pid, "status": "md", "error": "", "stderr": ""}
            if mode == "local":
                r["backend"] = "local-mineru"
            return r
        return {"id": pid, "status": "failed", "error": "502 bad gateway", "stderr": ""}
    return fake_convert_one


def test_the_local_backend_runs_after_the_hosted_routes_fail(args, monkeypatch):
    one_paper(args)
    monkeypatch.setattr(C, "token_gate", lambda: "")
    monkeypatch.setattr(C.ML, "is_setup", lambda root=None: True)
    calls = []
    monkeypatch.setattr(C, "convert_one", recorder(calls))
    argv(args)
    assert C.main() == 0
    assert calls == ["url", "upload", "local"]          # local is last, never first
    p = M.load(args)["papers"]["p1"]
    assert p["status"] == "md" and p["conversion"] == "local-mineru"


def test_no_token_goes_straight_to_the_local_backend(args, monkeypatch):
    one_paper(args)
    monkeypatch.setattr(C, "token_gate", lambda: C.TOKEN_MSG)
    monkeypatch.setattr(C.ML, "is_setup", lambda root=None: True)
    calls = []
    monkeypatch.setattr(C, "convert_one", recorder(calls))
    argv(args)
    assert C.main() == 0
    assert calls == ["local"]                            # no hosted attempt is made at all
    assert M.load(args)["papers"]["p1"]["conversion"] == "local-mineru"


def test_no_token_and_no_local_install_is_exit_3(args, monkeypatch, capsys):
    one_paper(args)
    monkeypatch.setattr(C, "token_gate", lambda: C.TOKEN_MSG)
    monkeypatch.setattr(C.ML, "is_setup", lambda root=None: False)
    monkeypatch.setattr(C, "convert_one", recorder([]))
    argv(args)
    assert C.main() == M.EXIT_TOKEN
    err = capsys.readouterr().err
    assert "mineru.net/apiManage/token" in err and "mineru_local.py --setup" in err


def test_an_uninstalled_local_backend_is_named_once_and_the_run_goes_on(args, monkeypatch, capsys):
    for pid in ("p1", "p2"):
        one_paper(args, pid)
    monkeypatch.setattr(C, "token_gate", lambda: "")
    monkeypatch.setattr(C.ML, "is_setup", lambda root=None: False)
    calls = []
    monkeypatch.setattr(C, "convert_one", recorder(calls, succeed_on=()))
    monkeypatch.setattr(C, "local_text_md", lambda pdf, md, t: (
        md.parent.mkdir(parents=True, exist_ok=True), md.write_text("x" * 5000), "")[2])
    argv(args, "--local-text-fallback")
    assert C.main() == 0
    assert calls == ["url", "upload", "url", "upload"]   # no local route was offered
    err = capsys.readouterr().err
    assert err.count("mineru_local.py --setup") == 1     # said once a run, not once a paper
    m = M.load(args)
    assert all(m["papers"][p]["conversion"] == "local-text" for p in ("p1", "p2"))


def test_backend_hosted_ignores_an_installed_local_backend(args, monkeypatch):
    one_paper(args)
    monkeypatch.setattr(C, "token_gate", lambda: "")
    monkeypatch.setattr(C.ML, "is_setup", lambda root=None: True)
    calls = []
    monkeypatch.setattr(C, "convert_one", recorder(calls, succeed_on=("upload",)))
    argv(args, "--backend", "hosted")
    assert C.main() == 0
    assert calls == ["url", "upload"]
    assert "conversion" not in M.load(args)["papers"]["p1"]


def test_backend_hosted_without_a_token_still_exits_3(args, monkeypatch):
    one_paper(args)
    monkeypatch.setattr(C, "token_gate", lambda: C.TOKEN_MSG)
    monkeypatch.setattr(C.ML, "is_setup", lambda root=None: True)   # installed, but not asked for
    argv(args, "--backend", "hosted")
    assert C.main() == M.EXIT_TOKEN


def test_backend_local_skips_the_hosted_routes(args, monkeypatch):
    one_paper(args)
    monkeypatch.setattr(C, "token_gate", lambda: (_ for _ in ()).throw(
        AssertionError("--backend local must not ask about the token")))
    monkeypatch.setattr(C.ML, "is_setup", lambda root=None: True)
    calls = []
    monkeypatch.setattr(C, "convert_one", recorder(calls))
    argv(args, "--backend", "local")
    assert C.main() == 0
    assert calls == ["local"]


def test_a_token_rejected_mid_run_hands_the_paper_to_the_local_backend(args, monkeypatch):
    one_paper(args)
    monkeypatch.setattr(C, "token_gate", lambda: "")
    monkeypatch.setattr(C.ML, "is_setup", lambda root=None: True)
    calls = []

    def fake_convert_one(pid, mode, source, out_dir, a, abort):
        calls.append(mode)
        if mode == "local":
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{pid}.md").write_text("x" * 5000)
            return {"id": pid, "status": "md", "error": "", "stderr": "", "backend": "local-mineru"}
        return {"id": pid, "status": "token", "error": "401 unauthorized", "stderr": ""}
    monkeypatch.setattr(C, "convert_one", fake_convert_one)
    argv(args)
    assert C.main() == 0                                 # not exit 3: the local backend works
    assert calls[-1] == "local"
    assert M.load(args)["papers"]["p1"]["status"] == "md"
