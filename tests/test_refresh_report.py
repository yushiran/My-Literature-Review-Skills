"""What --refresh tells the operator to do next when the round found nothing."""
import sys
import manifest as M
import pipeline as P


def fake_run(ran, stdout=""):
    def run(cmd, **kw):
        ran.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": stdout})()
    return run


def test_refresh_says_nothing_to_triage_when_no_candidate_is_new(args, monkeypatch, capsys):
    m = M.load(args)
    m["queries"] = ["q1"]
    M.save(args, m)                  # no papers at all, so no candidate can be new
    monkeypatch.setattr(P.subprocess, "run", fake_run([]))
    sys.argv = ["pipeline.py", "--topic", args.topic, "--root", args.root, "--refresh"]
    assert P.main() == M.EXIT_NOTHING
    out = capsys.readouterr().out
    assert "nothing to triage" in out, out
    assert "run the scout" not in out, "0 new candidates is not a reason to run the scout"
