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
    assert "readable by others" in capsys.readouterr().out


def test_load_env_is_silent_when_the_file_is_absent(tmp_path):
    M.load_env(str(tmp_path / "nothing.env"))               # must not raise
