import os
import manifest as M


def test_load_env_sets_recognised_keys_without_overriding(tmp_path, monkeypatch):
    f = tmp_path / "access.env"
    f.write_text("OPENALEX_API_KEY=fromfile\nS2_API_KEY=s2file\nNOT_A_SECRET=nope\n")
    f.chmod(0o600)
    # setenv first so monkeypatch owns the name: delenv on an absent name records nothing
    # to undo, so the value load_env sets would leak into every later test in the process.
    monkeypatch.setenv("OPENALEX_API_KEY", "owned-by-monkeypatch")
    monkeypatch.delenv("OPENALEX_API_KEY")
    monkeypatch.setenv("S2_API_KEY", "exported")
    monkeypatch.delenv("NOT_A_SECRET", raising=False)
    M.load_env(str(f))
    assert os.environ["OPENALEX_API_KEY"] == "fromfile"     # filled
    assert os.environ["S2_API_KEY"] == "exported"           # a real export wins
    assert "NOT_A_SECRET" not in os.environ                 # unrecognised keys ignored


def test_load_env_refuses_a_readable_file(tmp_path, monkeypatch, capsys):
    f = tmp_path / "access.env"
    f.write_text("OPENALEX_API_KEY=leaked\n")
    f.chmod(0o644)
    monkeypatch.setenv("OPENALEX_API_KEY", "owned-by-monkeypatch")   # so a regression here cannot leak
    monkeypatch.delenv("OPENALEX_API_KEY")
    M.load_env(str(f))
    assert "OPENALEX_API_KEY" not in os.environ
    # a diagnostic belongs on stderr: search.py's stdout is its machine-readable result line
    assert "readable by others" in capsys.readouterr().err


def test_load_env_is_silent_when_the_file_is_absent(tmp_path):
    M.load_env(str(tmp_path / "nothing.env"))               # must not raise


def test_load_env_strips_an_inline_comment_only_when_the_value_is_unquoted(tmp_path, monkeypatch):
    f = tmp_path / "access.env"
    f.write_text('OPENALEX_API_KEY=abc  # from openalex\nLITREV_COOKIES="/tmp/a#b/cookies.txt"\n')
    f.chmod(0o600)
    for k in ("OPENALEX_API_KEY", "LITREV_COOKIES"):
        monkeypatch.setenv(k, "owned-by-monkeypatch")
        monkeypatch.delenv(k)
    M.load_env(str(f))
    assert os.environ["OPENALEX_API_KEY"] == "abc"                      # comment not part of the key
    assert os.environ["LITREV_COOKIES"] == "/tmp/a#b/cookies.txt"       # a quoted value is verbatim


def test_load_env_survives_the_file_vanishing_after_the_isfile_check(tmp_path, monkeypatch):
    def gone(path, *a, **kw):
        raise FileNotFoundError(2, "No such file or directory", str(path))

    # isfile must be forced True as well: it calls os.stat itself, so patching stat alone
    # makes isfile return False and the test never reaches the line it is about.
    monkeypatch.setattr(M.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(M.os, "stat", gone)
    M.load_env(str(tmp_path / "access.env"))                # must not raise
