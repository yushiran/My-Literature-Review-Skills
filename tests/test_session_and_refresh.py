import sys
from pathlib import Path
import manifest as M
import access as A
import pipeline as P


def test_session_alive_detects_login_redirect(monkeypatch):
    monkeypatch.setenv("LITREV_EZPROXY_HOST", "bris.idm.oclc.org")
    monkeypatch.setenv("LITREV_COOKIES", "/nonexistent")
    assert A.session_alive(lambda url: "https://login.bris.idm.oclc.org/login?url=x") is False
    assert A.session_alive(lambda url: "https://onlinelibrary-wiley-com.bris.idm.oclc.org/") is True
    monkeypatch.delenv("LITREV_EZPROXY_HOST")
    assert A.session_alive(lambda url: url) is None


def test_refresh_runs_search_snowball_rank_and_stamps(args, monkeypatch):
    m = M.load(args)
    m["queries"] = ["q1"]
    m["created"] = "2024-03-01"
    m["papers"]["fresh"] = M.new_paper("fresh", title="fresh", status="found")
    m["papers"]["fresh"]["found_date"] = M.today()
    M.save(args, m)
    ran = []
    monkeypatch.setattr(P.subprocess, "run", lambda cmd, **kw: (ran.append(cmd), type("R", (), {"returncode": 0, "stdout": "ok"})())[1])
    sys.argv = ["pipeline.py", "--topic", args.topic, "--root", args.root, "--refresh"]
    assert P.main() == M.EXIT_OK
    names = [Path(c[1]).name for c in ran]
    assert names == ["search.py", "snowball.py", "rank.py"]
    assert "--since" in ran[0] and ran[0][ran[0].index("--since") + 1] == "2024"
    assert "--new-only" in ran[2]
    assert M.load(args)["refreshed"] == M.today()
