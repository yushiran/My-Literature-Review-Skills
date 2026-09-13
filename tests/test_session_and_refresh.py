import sys
from pathlib import Path
import manifest as M
import access as A
import pipeline as P

# What rank.py prints; candidates= is its count after every filter and the --top cap.
RANK_LINE = "ranked=9 candidates=3 written=/x/candidates.md titles=/x/candidates_titles.md"


def test_session_alive_detects_login_redirect(monkeypatch):
    monkeypatch.setenv("LITREV_EZPROXY_HOST", "bris.idm.oclc.org")
    monkeypatch.setenv("LITREV_COOKIES", "/nonexistent")
    assert A.session_alive(lambda url: "https://login.bris.idm.oclc.org/login?url=x") is False
    assert A.session_alive(lambda url: "https://onlinelibrary-wiley-com.bris.idm.oclc.org/") is True
    monkeypatch.delenv("LITREV_EZPROXY_HOST")
    assert A.session_alive(lambda url: url) is None


def seed(args):
    """A library with one stored query, created in 2024, and one paper found today."""
    m = M.load(args)
    m["queries"] = ["q1"]
    m["created"] = "2024-03-01"
    m["papers"]["fresh"] = M.new_paper("fresh", title="fresh", status="found")
    m["papers"]["fresh"]["found_date"] = M.today()
    M.save(args, m)


def fake_run(ran, rank_stdout):
    """subprocess.run stand-in: records each command, gives rank.py the stdout under test."""
    def run(cmd, **kw):
        ran.append(cmd)
        return type("R", (), {"returncode": 0,
                              "stdout": rank_stdout if Path(cmd[1]).name == "rank.py" else "ok"})()
    return run


def test_refresh_runs_search_snowball_rank_and_stamps(args, monkeypatch, capsys):
    seed(args)
    ran = []
    monkeypatch.setattr(P.subprocess, "run", fake_run(ran, RANK_LINE))
    sys.argv = ["pipeline.py", "--topic", args.topic, "--root", args.root, "--refresh"]
    assert P.main() == M.EXIT_OK
    names = [Path(c[1]).name for c in ran]
    assert names == ["search.py", "snowball.py", "rank.py"]
    assert "--since" in ran[0] and ran[0][ran[0].index("--since") + 1] == "2024"
    assert "--new-only" in ran[2]
    assert M.load(args)["refreshed"] == M.today()
    # 3 is rank.py's own count; the manifest holds 1, so this cannot be a recomputation
    assert "refresh: 3 new candidates" in capsys.readouterr().out


def test_refresh_counts_from_the_manifest_when_rank_prints_no_count(args, monkeypatch, capsys):
    seed(args)
    ran = []
    monkeypatch.setattr(P.subprocess, "run", fake_run(ran, "ok"))
    sys.argv = ["pipeline.py", "--topic", args.topic, "--root", args.root, "--refresh"]
    assert P.main() == M.EXIT_OK
    assert "refresh: 1 new candidates" in capsys.readouterr().out
