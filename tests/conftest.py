import hashlib
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from tests.fakes import FakeModel

REPO = Path(__file__).resolve().parent.parent
STATE_DIRS = ("registry", "queue", "runs")
REGISTRY_DIGEST = pytest.StashKey[str]()


def _digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != ".DS_Store":
            h.update(str(path.relative_to(root)).encode())
            h.update(path.read_bytes())
    return h.hexdigest()


def pytest_sessionstart(session):
    session.config.stash[REGISTRY_DIGEST] = _digest(REPO / "registry")


def pytest_sessionfinish(session, exitstatus):
    if _digest(REPO / "registry") != session.config.stash[REGISTRY_DIGEST]:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter:
            reporter.ensure_newline()
            reporter.write_line(
                "FAIL: the real registry/ changed during the test run. Tests must use the `home` fixture."
                " (A creature run in parallel also triggers this.)",
                red=True,
            )


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def pytest_collection_modifyitems(config, items):
    docker_items = [item for item in items if item.get_closest_marker("docker")]
    if docker_items and not _docker_available():
        skip = pytest.mark.skip(reason="Docker daemon not reachable: sandbox tests NOT run")
        for item in docker_items:
            item.add_marker(skip)


@pytest.fixture
def home(tmp_path) -> Path:
    """Empty creature home with its own registry/, queue/ and runs/."""
    root = tmp_path / "home"
    for name in STATE_DIRS:
        (root / name).mkdir(parents=True)
    return root


@pytest.fixture
def fake_model() -> FakeModel:
    return FakeModel()


class _GuardedPopen(subprocess.Popen):
    def __init__(self, args, *rest, **kwargs):
        program = args if isinstance(args, str | bytes) else args[0]
        if Path(os.fsdecode(program).split()[0]).name == "claude":
            raise RuntimeError("live model call in an offline test: use fake_model or mark the test slow")
        super().__init__(args, *rest, **kwargs)


@pytest.fixture(autouse=True)
def _isolated(request, monkeypatch, home):
    """Every test gets a temp home and no secrets; offline tests also get no network and no model."""
    monkeypatch.setenv("CREATURE_HOME", str(home))
    monkeypatch.delenv("CREATURE_SECRETS", raising=False)
    # local Whisper is off unless a test points at a model: the suite must not depend on a 1.5 GB
    # file on the host, and a transcript that differs by machine makes the perceive tests flaky
    monkeypatch.setenv("CREATURE_WHISPER_MODEL", str(home / "no-whisper-model.bin"))
    if request.node.get_closest_marker("slow"):
        return

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def refuse(real):
        def guarded(sock, address):
            if sock.family == socket.AF_UNIX:
                return real(sock, address)
            raise RuntimeError(f"network access in an offline test ({address!r}): mark it slow")

        return guarded

    monkeypatch.setattr(socket.socket, "connect", refuse(real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", refuse(real_connect_ex))
    monkeypatch.setattr(subprocess, "Popen", _GuardedPopen)
