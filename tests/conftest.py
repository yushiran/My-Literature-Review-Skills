import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import manifest as M  # noqa: E402

# access.py runs load_env() at import, and a test module that imports access.py is
# imported at collection time, before any fixture can run. This line runs earlier still:
# pytest imports the conftest of a directory before it collects the tests in it.
NO_ENV_FILE = Path(__file__).resolve().parent / "no-such.env"
assert not NO_ENV_FILE.exists(), f"{NO_ENV_FILE} must not exist: it is the tests' empty env file"
os.environ["LITREV_ENV_FILE"] = str(NO_ENV_FILE)


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
    no test may read the real ~/.config/litrev/access.env into this process.
    Also drop any genuinely exported key: pytest renders os.environ on a failed
    membership assertion, and truncation keeps the tail where a fresh key lands.
    The module-level NO_ENV_FILE above covers import time, which this cannot reach."""
    monkeypatch.setenv("LITREV_ENV_FILE", str(tmp_path / "no-such.env"))
    for k in M.SECRETS:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture(autouse=True)
def _mineru_root(tmp_path, monkeypatch):
    """No test may see this machine's real local MinerU. is_setup() decides convert.py's
    route order, so a developer who has run --setup would otherwise run other code."""
    monkeypatch.setenv("LITREV_MINERU_HOME", str(tmp_path / "mineru-root"))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """A forgotten mock must fail loudly here, not make a real request that is slow and
    passes only because the remote happened to answer with an error that day."""
    def blocked(*args, **kwargs):
        # socket.socket.connect(self, address); create_connection(address); getaddrinfo(host, ...)
        target = args[1] if args and isinstance(args[0], socket.socket) else (args[0] if args else "?")
        raise RuntimeError(f"test tried to reach the network: {target}")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
