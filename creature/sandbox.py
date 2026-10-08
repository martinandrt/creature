"""Run untrusted skill code in a throwaway Docker container.

The container is the real security boundary: no network, read-only filesystem, no capabilities,
an unprivileged user, and hard limits on memory, CPU, processes, wall-clock time and output size.
The sandbox only returns what the skill produced. Deciding whether that output is correct
happens on the host, in fixed code, never inside the container.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

IMAGE = "python:3.12-slim"
MEMORY = "256m"

# Fixed runner (ours, not generated). Reads {"code", "input"} from stdin, calls run(input),
# writes exactly one JSON line. Prints from the skill are captured so they cannot break the protocol.
RUNNER = r"""
import io, json, sys
real_stdout = sys.stdout
payload = json.loads(sys.stdin.read())
sys.stdout = io.StringIO()
try:
    namespace = {"__name__": "skill"}
    exec(compile(payload["code"], "skill.py", "exec"), namespace)
    run = namespace.get("run")
    if not callable(run):
        raise TypeError("skill defines no run(input) function")
    result = {"output": run(payload["input"])}
    line = json.dumps(result)
except BaseException as error:
    line = json.dumps({"error": f"{type(error).__name__}: {error}"[:2000]})
real_stdout.write(line + "\n")
real_stdout.flush()
"""

Killed = Literal["timeout", "output", "memory"] | None


@dataclass(frozen=True)
class SandboxResult:
    ok: bool
    output: Any | None
    error: str | None
    killed: Killed
    duration_s: float


def docker_command(name: str) -> list[str]:
    return [
        "docker", "run", "--rm", "-i", "--name", name,
        "--network", "none",
        "--read-only", "--tmpfs", "/tmp:rw,size=16m,noexec",
        "--memory", MEMORY, "--memory-swap", MEMORY, "--cpus", "1",
        "--pids-limit", "64", "--ulimit", "nofile=64:64",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", "65534:65534", "--env", "GPG_KEY=",
        IMAGE, "python", "-I", "-c", RUNNER,
    ]  # fmt: skip


def available() -> bool:
    """True when the Docker daemon answers."""
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _remove(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


def run(
    code: str, input: Any, *, timeout_s: float = 10.0, max_output_bytes: int = 1_000_000
) -> SandboxResult:
    """Run `code` (must define run(input)) on `input` inside a fresh container."""
    start = time.monotonic()
    name = f"creature-{uuid.uuid4().hex[:12]}"
    try:
        payload = json.dumps({"code": code, "input": input}).encode()
    except (TypeError, ValueError) as error:
        return SandboxResult(False, None, f"input is not JSON: {error}", None, 0.0)
    if not shutil.which("docker"):
        return SandboxResult(False, None, "docker is not installed", None, 0.0)

    proc = subprocess.Popen(
        docker_command(name), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout = bytearray()
    overflow = threading.Event()

    def read_stdout() -> None:
        while chunk := proc.stdout.read(65536):
            if len(stdout) + len(chunk) > max_output_bytes:
                overflow.set()
                return
            stdout.extend(chunk)

    def read_stderr() -> None:
        proc.stderr.read()  # drained so the container never blocks on a full pipe; content is not trusted

    readers = [threading.Thread(target=target, daemon=True) for target in (read_stdout, read_stderr)]
    for reader in readers:
        reader.start()
    try:
        proc.stdin.write(payload)
        proc.stdin.close()
    except BrokenPipeError:
        pass

    killed: Killed = None
    deadline = start + timeout_s
    while proc.poll() is None:
        if overflow.is_set():
            killed = "output"
            break
        if time.monotonic() > deadline:
            killed = "timeout"
            break
        time.sleep(0.02)

    if killed:
        _remove(name)
        proc.kill()
    proc.wait(timeout=30)
    readers[0].join(timeout=5)
    duration = round(time.monotonic() - start, 3)

    if killed:
        return SandboxResult(False, None, f"killed: {killed}", killed, duration)
    if overflow.is_set():
        return SandboxResult(False, None, "killed: output", "output", duration)
    if proc.returncode == 137:
        return SandboxResult(False, None, "killed: memory", "memory", duration)
    return _parse(bytes(stdout), proc.returncode, duration)


def _parse(stdout: bytes, returncode: int, duration: float) -> SandboxResult:
    lines = [line for line in stdout.decode(errors="replace").splitlines() if line.strip()]
    if not lines:
        return SandboxResult(False, None, f"no result (exit {returncode})", None, duration)
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError:
        return SandboxResult(False, None, "result is not JSON", None, duration)
    if not isinstance(result, dict) or not ({"output", "error"} & result.keys()):
        return SandboxResult(False, None, "result has no output or error", None, duration)
    if "error" in result:
        return SandboxResult(False, None, str(result["error"]), None, duration)
    return SandboxResult(True, result["output"], None, None, duration)
