import subprocess

import pytest

from creature import sandbox

pytestmark = pytest.mark.docker


def _leftover_containers() -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=creature-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    )
    return out.stdout.split()


def test_returns_output_of_run():
    result = sandbox.run("def run(x):\n    return {'words': len(x['text'].split())}", {"text": "a b c"})
    assert result.ok and result.output == {"words": 3} and result.killed is None


def test_skill_exception_becomes_error():
    result = sandbox.run("def run(x):\n    raise ValueError('bad input')", {})
    assert not result.ok and "ValueError: bad input" in result.error


def test_missing_run_function():
    result = sandbox.run("x = 1", {})
    assert not result.ok and "no run(input)" in result.error


def test_prints_do_not_break_protocol():
    result = sandbox.run("def run(x):\n    print('noise')\n    return 7", {})
    assert result.ok and result.output == 7


def test_non_json_output_is_error():
    result = sandbox.run("def run(x):\n    return {1, 2}", {})
    assert not result.ok and "TypeError" in result.error


def test_network_is_blocked():
    code = (
        "import socket\n"
        "def run(x):\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=2)\n"
        "    return 'open'"
    )
    result = sandbox.run(code, {})
    assert not result.ok and "OSError" in result.error


def test_filesystem_is_read_only_outside_tmp():
    code = "def run(x):\n    open('/etc/creature', 'w').write('x')\n    return 'written'"
    result = sandbox.run(code, {})
    assert not result.ok and ("OSError" in result.error or "PermissionError" in result.error)


def test_no_secrets_in_environment():
    code = (
        "import os\n"
        "def run(x):\n"
        "    return sorted(k for k, v in os.environ.items() if v and ('KEY' in k or 'TOKEN' in k))"
    )
    result = sandbox.run(code, {})
    assert result.ok and result.output == []


def test_endless_loop_is_killed_and_container_removed():
    result = sandbox.run("def run(x):\n    while True:\n        pass", {}, timeout_s=3)
    assert not result.ok and result.killed == "timeout"
    assert result.duration_s < 10
    assert _leftover_containers() == []


def test_huge_output_is_killed():
    code = "import sys\ndef run(x):\n    sys.__stdout__.write('x' * 5_000_000)\n    return 1"
    result = sandbox.run(code, {}, max_output_bytes=100_000)
    assert not result.ok and result.killed == "output"
    assert _leftover_containers() == []


def test_memory_hog_is_contained():
    result = sandbox.run("def run(x):\n    x = [0] * (10**9)\n    return len(x)", {}, timeout_s=20)
    assert not result.ok
    assert result.killed == "memory" or "MemoryError" in (result.error or "")
    assert _leftover_containers() == []


def test_large_input_does_not_block():
    result = sandbox.run("def run(x):\n    return len(x['text'])", {"text": "a" * 2_000_000})
    assert result.ok and result.output == 2_000_000


def test_input_must_be_json():
    result = sandbox.run("def run(x):\n    return x", {1, 2})
    assert not result.ok and "not JSON" in result.error
