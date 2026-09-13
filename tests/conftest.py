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
