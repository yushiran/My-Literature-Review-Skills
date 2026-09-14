"""The credential file must not reach the pytest process, on any machine.

The import-time guard in test_session_and_refresh.py proves that only where a real
~/.config/litrev/access.env happens to exist. This plants a sentinel one in a fake
HOME and runs that guard again as a subprocess, so the proof is machine-independent.
"""
import os
import subprocess
import sys
from pathlib import Path

import manifest as M

TESTS = Path(__file__).resolve().parent
GUARD = ("test_session_and_refresh.py"
         "::test_no_credential_reached_this_process_before_the_fixtures_ran")


def test_a_credential_file_in_home_never_reaches_the_pytest_process(tmp_path):
    home = tmp_path / "home"
    (home / ".config" / "litrev").mkdir(parents=True)
    env_file = home / ".config" / "litrev" / "access.env"
    env_file.write_text("".join(f"{k}=sentinel-{k}\n" for k in M.SECRETS))
    env_file.chmod(0o600)   # load_env ignores a group- or world-readable file
    env = {k: v for k, v in os.environ.items() if k not in M.SECRETS}
    env.pop("LITREV_ENV_FILE", None)   # conftest must set this itself, not inherit it
    env["HOME"] = str(home)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", GUARD],
                       cwd=TESTS, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
