import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def args(tmp_path):
    """A --topic/--root namespace pointing at an empty temporary library root."""
    return SimpleNamespace(topic="t", root=str(tmp_path / "references"))


@pytest.fixture(autouse=True)
def _lock_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LITREV_LOCK_DIR", str(tmp_path / "locks"))


@pytest.fixture(autouse=True)
def _no_real_credentials(tmp_path, monkeypatch):
    """search.main() calls M.load_env(), so point it at a file that does not exist:
    no test may read the real ~/.config/litrev/access.env into this process."""
    monkeypatch.setenv("LITREV_ENV_FILE", str(tmp_path / "no-such.env"))
