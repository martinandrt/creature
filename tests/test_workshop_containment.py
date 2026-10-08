"""Containment tests for the workshop (creature/workshop.py): the boundary, not the toolset.

QA owns this file per the split with the main session: network, no writes outside /work, noexec
on /work, /work size enforced while the skill runs, no secrets, timeout, process count, output
cap, memory, no leftover containers. Basic behavior (value, files in/out, ffmpeg runs, error
line, tar safety, fonts) lives in test_workshop.py.

Skill code contract: the runner calls `run(input, "/work")`; the skill reads /work/in/*, writes
/work/out/* and returns a JSON value. Every cap comes from authority.json via Limits — tests
start from the shipped authority and override one field at a time, never from constants.
"""

import dataclasses
import subprocess

import pytest

from creature import authority
from tests.conftest import REPO

workshop = pytest.importorskip("creature.workshop")

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module", autouse=True)
def _image():
    # builds the pinned image when missing; the docker marker already skips without a daemon
    workshop.ensure_image()


def _run(code: str, **overrides):
    limits = dataclasses.replace(authority.load(REPO).workshop, **overrides)
    return workshop.run(code, {}, limits=limits)


def _leftover_containers() -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=creature-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    return out.stdout.split()


def test_python_cannot_reach_the_network():
    code = (
        "import socket\n"
        "def run(input, work):\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=3)\n"
        "    return 'reached'\n"
    )
    result = _run(code)
    assert not result.ok and result.value != "reached"
    assert "OSError" in (result.error or "")


def test_programs_cannot_reach_the_network_either():
    # an IP, not a name: DNS failure would pass this test for the wrong reason
    code = (
        "import subprocess\n"
        "def run(input, work):\n"
        "    cmd = ['ffmpeg', '-v', 'error', '-i', 'http://1.1.1.1/x.mp4', '-f', 'null', '-']\n"
        "    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)\n"
        "    return proc.returncode\n"
    )
    result = _run(code)
    assert result.ok and result.value != 0


def test_filesystem_is_read_only_outside_work():
    code = "def run(input, work):\n    open('/etc/creature', 'w').write('x')\n    return 'written'\n"
    result = _run(code)
    assert not result.ok and result.value != "written"


def test_work_is_noexec():
    # a skill may run programs from the image, not programs it wrote itself
    code = (
        "import os, subprocess\n"
        "def run(input, work):\n"
        "    path = work + '/out/hello.sh'\n"
        "    open(path, 'w').write('#!/bin/sh\\necho hello\\n')\n"
        "    os.chmod(path, 0o755)\n"
        "    try:\n"
        "        subprocess.run([path], check=True)\n"
        "        return 'ran'\n"
        "    except OSError as error:\n"
        "        return type(error).__name__\n"
    )
    result = _run(code)
    assert result.ok and result.value != "ran"


def test_write_past_work_size_fails_while_running(tmp_path):
    # Martin's rule: the work folder size is enforced at run time, not checked afterwards
    code = (
        "def run(input, work):\n"
        "    with open(work + '/out/fill.bin', 'wb') as f:\n"
        "        for _ in range(5):\n"
        "            f.write(b'\\0' * 1_000_000)\n"
        "    return 'done'\n"
    )
    result = _run(code, work_mb=4)
    assert not result.ok and result.value != "done"
    assert "OSError" in (result.error or "")
    assert _leftover_containers() == []


def test_no_secrets_in_environment():
    code = (
        "import os\n"
        "def run(input, work):\n"
        "    marks = ('KEY', 'TOKEN', 'SECRET')\n"
        "    return sorted(k for k, v in os.environ.items() if v and any(m in k for m in marks))\n"
    )
    result = _run(code)
    assert result.ok and result.value == []


def test_endless_loop_is_killed_and_container_removed():
    result = _run("def run(input, work):\n    while True:\n        pass\n", timeout_s=3)
    assert not result.ok and result.killed == "timeout"
    assert result.duration_s < 10
    assert _leftover_containers() == []


def test_process_count_is_bounded():
    # the pids cap holds for the whole container: starting 300 sleepers must stop short, and the
    # container must still come down cleanly with the sleepers inside it
    code = (
        "import subprocess\n"
        "def run(input, work):\n"
        "    started = []\n"
        "    try:\n"
        "        for _ in range(300):\n"
        "            started.append(subprocess.Popen(['sleep', '30']))\n"
        "    except OSError:\n"
        "        pass\n"
        "    return len(started)\n"
    )
    result = _run(code, timeout_s=60)
    assert result.ok and 0 < result.value < 300
    assert _leftover_containers() == []


def test_oversized_output_is_refused():
    code = (
        "def run(input, work):\n"
        "    with open(work + '/out/big.bin', 'wb') as f:\n"
        "        for _ in range(20):\n"
        "            f.write(b'\\0' * 1_000_000)\n"
        "    return 'done'\n"
    )
    result = _run(code, output_mb=1)
    assert not result.ok and result.killed == "output"
    assert result.outputs == {}
    assert _leftover_containers() == []


def test_memory_hog_is_killed():
    code = (
        "import numpy as np\n"
        "def run(input, work):\n"
        "    x = np.ones((40_000, 40_000))\n"
        "    return float(x.sum())\n"
    )
    result = _run(code, timeout_s=30)
    assert not result.ok and result.killed == "memory"
    assert _leftover_containers() == []
