import os
import sys
import urllib.error
from pathlib import Path
import manifest as M
import access as A
import fetch as F
import pipeline as P

# access.py runs load_env() at import, and the imports above run at collection time,
# before any fixture can. Snapshot what that import left in this process.
ENV_AT_IMPORT = dict(os.environ)

# What rank.py prints; candidates= is its count after every filter and the --top cap.
RANK_LINE = "ranked=9 candidates=3 written=/x/candidates.md titles=/x/candidates_titles.md"
REFRESH_SCRIPTS = ("search.py", "snowball.py", "rank.py")


def test_no_credential_reached_this_process_before_the_fixtures_ran():
    """conftest's autouse fixture cannot cover this: collection imports access.py, which
    reads ~/.config/litrev/access.env, before the first fixture runs."""
    leaked = sorted(k for k in M.SECRETS if k in ENV_AT_IMPORT)
    assert not leaked, f"import-time load_env() put {leaked} into the test process"


def test_session_alive_detects_login_redirect(monkeypatch):
    monkeypatch.setenv("LITREV_EZPROXY_HOST", "bris.idm.oclc.org")
    monkeypatch.setenv("LITREV_COOKIES", "/nonexistent")
    assert A.session_alive(lambda url: "https://login.bris.idm.oclc.org/login?url=x") is False
    assert A.session_alive(lambda url: "https://onlinelibrary-wiley-com.bris.idm.oclc.org/") is True
    monkeypatch.delenv("LITREV_EZPROXY_HOST")
    assert A.session_alive(lambda url: url) is None


def raises_403_from(endpoint):
    """A caller answering like a host behind Cloudflare: a real response, code 403."""
    def call(url):
        raise urllib.error.HTTPError(endpoint, 403, "Forbidden", {}, None)
    return call


def test_session_alive_survives_a_403_but_not_a_dead_connection(monkeypatch):
    monkeypatch.setenv("LITREV_EZPROXY_HOST", "bris.idm.oclc.org")
    # A response came back from the proxied host, so the session is not what failed.
    assert A.session_alive(raises_403_from("https://onlinelibrary-wiley-com.bris.idm.oclc.org/")) is True
    # The same code served from the login page still means the cookie jar is stale.
    assert A.session_alive(raises_403_from("https://login.bris.idm.oclc.org/login?url=x")) is False

    def dead(url):
        raise OSError("no route to host")

    assert A.session_alive(dead) is False
    assert A.session_alive(lambda url: "") is False   # no endpoint is not evidence of a live session


def seed(args):
    """A library with one stored query, created in 2024, and one paper found today."""
    m = M.load(args)
    m["queries"] = ["q1"]
    m["created"] = "2024-03-01"
    m["papers"]["fresh"] = M.new_paper("fresh", title="fresh", status="found")
    m["papers"]["fresh"]["found_date"] = M.today()
    M.save(args, m)


def all_scripts_exist(monkeypatch):
    """snowball.py lands on a sibling branch, so the existence check must not gate these tests."""
    real = P.Path.is_file
    monkeypatch.setattr(P.Path, "is_file", lambda self: self.name in REFRESH_SCRIPTS or real(self))


def fake_run(ran, rank_stdout):
    """subprocess.run stand-in: records each command, gives rank.py the stdout under test."""
    def run(cmd, **kw):
        ran.append(cmd)
        return type("R", (), {"returncode": 0,
                              "stdout": rank_stdout if Path(cmd[1]).name == "rank.py" else "ok"})()
    return run


def test_refresh_runs_search_snowball_rank_and_stamps(args, monkeypatch, capsys):
    seed(args)
    all_scripts_exist(monkeypatch)
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
    all_scripts_exist(monkeypatch)
    ran = []
    monkeypatch.setattr(P.subprocess, "run", fake_run(ran, "ok"))
    sys.argv = ["pipeline.py", "--topic", args.topic, "--root", args.root, "--refresh"]
    assert P.main() == M.EXIT_OK
    assert "refresh: 1 new candidates" in capsys.readouterr().out


def test_refresh_defers_a_partial_search_and_still_ranks(args, monkeypatch, capsys):
    seed(args)
    all_scripts_exist(monkeypatch)
    ran = []

    def run(cmd, **kw):
        ran.append(cmd)
        name = Path(cmd[1]).name
        # search.py returns 2 after saving what it got, and prints nothing of its own
        code = M.EXIT_NETWORK if name == "search.py" else M.EXIT_OK
        return type("R", (), {"returncode": code,
                              "stdout": RANK_LINE if name == "rank.py" else ""})()

    monkeypatch.setattr(P.subprocess, "run", run)
    sys.argv = ["pipeline.py", "--topic", args.topic, "--root", args.root, "--refresh"]
    assert P.main() == M.EXIT_NETWORK
    assert [Path(c[1]).name for c in ran] == list(REFRESH_SCRIPTS)
    assert "refreshed" not in M.load(args)   # an incomplete search must not advance the window
    out = capsys.readouterr().out
    assert "search: exit 2" in out           # the empty-stdout fallback
    assert "refresh: 3 new candidates" in out


def test_refresh_exits_cleanly_on_ctrl_c(args, monkeypatch):
    seed(args)
    all_scripts_exist(monkeypatch)
    ran = []

    def run(cmd, **kw):
        ran.append(cmd)
        if Path(cmd[1]).name == "snowball.py":
            raise KeyboardInterrupt
        return type("R", (), {"returncode": M.EXIT_OK, "stdout": "ok"})()

    monkeypatch.setattr(P.subprocess, "run", run)
    sys.argv = ["pipeline.py", "--topic", args.topic, "--root", args.root, "--refresh"]
    assert P.main() == M.EXIT_USAGE
    assert [Path(c[1]).name for c in ran] == ["search.py", "snowball.py"]   # rank never starts
    assert "refreshed" not in M.load(args)


def test_refresh_stops_when_a_step_script_is_missing(args, monkeypatch):
    seed(args)
    real = P.Path.is_file
    monkeypatch.setattr(P.Path, "is_file", lambda self: False if self.name == "rank.py" else real(self))
    ran = []
    monkeypatch.setattr(P.subprocess, "run", fake_run(ran, RANK_LINE))
    sys.argv = ["pipeline.py", "--topic", args.topic, "--root", args.root, "--refresh"]
    assert P.main() == M.EXIT_USAGE
    assert ran == []                         # nothing is run when a step is missing


def test_refresh_survives_an_unusable_date_in_the_manifest(args, monkeypatch):
    seed(args)
    all_scripts_exist(monkeypatch)
    m = M.load(args)
    m["created"] = "n/a"
    M.save(args, m)
    ran = []
    monkeypatch.setattr(P.subprocess, "run", fake_run(ran, RANK_LINE))
    sys.argv = ["pipeline.py", "--topic", args.topic, "--root", args.root, "--refresh"]
    assert P.main() == M.EXIT_OK
    assert ran[0][ran[0].index("--since") + 1] == M.today()[:4]


def test_a_fetch_network_exit_names_no_cause(args, monkeypatch, capsys):
    """fetch returns 2 for a stale proxy session and for a dead network alike, and prints
    the right message for each itself. The pipeline cannot tell them apart, so it must not try."""
    def run(cmd, **kw):
        code = M.EXIT_NETWORK if Path(cmd[1]).name == "fetch.py" else M.EXIT_OK
        return type("R", (), {"returncode": code, "stdout": ""})()

    monkeypatch.setattr(P.subprocess, "run", run)
    sys.argv = ["pipeline.py", "--topic", args.topic, "--root", args.root]
    assert P.main() == M.EXIT_NETWORK
    cap = capsys.readouterr()
    text = cap.out + cap.err
    assert "credential" not in text, text      # a cause the exit code does not determine
    assert "converting what arrived" in text
    assert "left selected" in text


def test_openalex_meta_sends_the_api_key_only_when_it_is_set(monkeypatch):
    seen = []
    monkeypatch.setattr(F, "get_json", lambda url, timeout, what: (seen.append(url), {})[1])
    monkeypatch.setenv("OPENALEX_API_KEY", "k123")
    F.openalex_meta("10.1/x", 10, "w")
    monkeypatch.delenv("OPENALEX_API_KEY")
    F.openalex_meta("10.1/x", 10, "w")
    assert "api_key=k123" in seen[0]      # once per paper, so the anonymous pool runs out
    assert "api_key" not in seen[1]
