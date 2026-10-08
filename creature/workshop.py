"""The workshop: runs skill code in a throwaway container that has tools and one work folder.

Skills are scripts over files. The host streams the code, the input and the input files in as a tar
archive; the skill reads /work/in, writes /work/out and may start the image's programs (ffmpeg). The
host gets /work/out back as a tar archive and checks every entry before keeping it. Nothing on the
host is mounted into the container.

The container is the security boundary: no network, a read-only image, no capabilities, an
unprivileged user, and limits from authority.json on time, memory, CPU, processes, the work folder
(a tmpfs: a write past its size fails while the skill runs) and the size of what comes back.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tarfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from creature.home import resolve as resolve_home

IMAGE = "creature-workshop:v1"
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
STDERR_TAIL = 4000

# Fixed helpers (ours) prepended to the reel's and the clip's frame code: frames become sheets of
# 270x480 tiles, 6 per row, 4 rows per sheet, grey gutters, each tile's time printed under it; cuts
# and the palette are measured the same way on a reel and on a clip, so the two can be compared.
SHEET_CODE = r"""
import os
import re
import subprocess
import numpy as np
from PIL import Image, ImageDraw, ImageFont

TILE_W, TILE_H, LABEL_H, GAP, COLUMNS, PER_SHEET = 270, 480, 30, 8, 6, 24
FIT = (f"scale={TILE_W}:{TILE_H}:force_original_aspect_ratio=decrease,"
       f"pad={TILE_W}:{TILE_H}:(ow-iw)/2:(oh-ih)/2")
LABEL_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

def grab(video, t, path, vf=None):
    # one frame near t; a seek at the very end can come back empty, so step back a little and retry
    for back in (0.0, 0.1, 0.25, 0.5, 1.0):
        args = ["ffmpeg", "-v", "error", "-y", "-ss", str(max(0.0, t - back)), "-i", video, "-frames:v", "1"]
        subprocess.run(args + (["-vf", vf] if vf else []) + [path], check=True)
        if os.path.exists(path):
            return path
    raise RuntimeError(f"no frame could be read near {t} s")

def sheets(tiles, times, prefix):
    font = ImageFont.truetype(LABEL_FONT, 22)
    names = []
    for start in range(0, len(tiles), PER_SHEET):
        chunk = tiles[start:start + PER_SHEET]
        rows, cols = -(-len(chunk) // COLUMNS), min(COLUMNS, len(chunk))
        sheet = Image.new("RGB", (GAP + cols * (TILE_W + GAP), GAP + rows * (TILE_H + LABEL_H + GAP)),
                          (127, 127, 127))
        draw = ImageDraw.Draw(sheet)
        for j, path in enumerate(chunk):
            x, y = GAP + (j % COLUMNS) * (TILE_W + GAP), GAP + (j // COLUMNS) * (TILE_H + LABEL_H + GAP)
            with Image.open(path) as tile:
                sheet.paste(tile.convert("RGB"), (x, y))
            draw.rectangle((x, y + TILE_H, x + TILE_W - 1, y + TILE_H + LABEL_H - 1), fill=(0, 0, 0))
            draw.text((x + 8, y + TILE_H + 3), f"{times[start + j]:.2f} s", fill=(255, 255, 255), font=font)
        name = prefix + ("" if start == 0 else f"-{start // PER_SHEET + 1}") + ".png"
        sheet.save(name)
        names.append(name)
    return names

def cuts_of(video, score):
    found = subprocess.run(["ffmpeg", "-v", "info", "-i", video, "-vf",
                            f"select='gt(scene,{score})',showinfo", "-an", "-f", "null", "-"],
                           capture_output=True, text=True)
    return sorted({round(float(t), 3) for t in re.findall(r"pts_time:([0-9.]+)", found.stderr)})

def palette(paths, size, width, height):
    # only the picture: the tiles are padded to 270x480, and the padding is not a colour of the reel
    scale = min(TILE_W / width, TILE_H / height)
    w, h = max(1, int(width * scale)), max(1, int(height * scale))
    x0, y0 = (TILE_W - w) // 2 + 1, (TILE_H - h) // 2 + 1
    crops = [np.asarray(Image.open(p).convert("RGB"))[y0:y0 + h - 2, x0:x0 + w - 2] for p in paths]
    pixels = np.concatenate([c.reshape(-1, 3)[::37] for c in crops])
    pixels = pixels.astype(np.float32)
    order = np.argsort(pixels.sum(axis=1))
    centers = pixels[order[np.linspace(0, len(order) - 1, size).astype(int)]]
    for _ in range(12):
        nearest = np.argmin(((pixels[:, None, :] - centers[None]) ** 2).sum(axis=2), axis=1)
        centers = np.array([pixels[nearest == k].mean(axis=0) if (nearest == k).any() else centers[k]
                            for k in range(size)])
    shares = np.bincount(nearest, minlength=size) / len(pixels)
    found = []
    for k in np.argsort(-shares):
        color = centers[k]
        if shares[k] < 0.02 or any(np.abs(color - np.array(c)).sum() < 40 for c, _ in found):
            continue
        found.append((color.round().astype(int).tolist(), float(shares[k])))
    return [{"hex": "#%02x%02x%02x" % tuple(c), "share": round(sh, 3)} for c, sh in found]
"""

# Fixed runner (ours, not generated). Unpacks the input archive into /work, calls run(input, "/work"),
# and writes /work/out plus result.json back as one tar archive. Skill prints cannot break the protocol.
RUNNER = r"""
import io, json, os, sys, tarfile, traceback
real_stdout = sys.stdout.buffer
with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read())) as archive:
    archive.extractall("/work", filter="data")
with open("/work/payload.json") as file:
    payload = json.load(file)
os.remove("/work/payload.json")
os.makedirs("/work/out", exist_ok=True)
sys.stdout = io.StringIO()
try:
    namespace = {"__name__": "skill"}
    exec(compile(payload["code"], "skill.py", "exec"), namespace)
    run = namespace.get("run")
    if not callable(run):
        raise TypeError("skill defines no run(input, workdir) function")
    body = json.dumps({"value": run(payload["input"], "/work")})
except BaseException as error:
    lines = [f.lineno for f in traceback.extract_tb(error.__traceback__) if f.filename == "skill.py"]
    where = f" (skill.py line {lines[-1]})" if lines else ""
    body = json.dumps({"error": f"{type(error).__name__}: {error}{where}"[:2000]})
buffer = io.BytesIO()
with tarfile.open(fileobj=buffer, mode="w") as archive:
    if os.path.isdir("/work/out"):
        archive.add("/work/out", arcname="out")
    data = body.encode()
    info = tarfile.TarInfo("result.json")
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))
real_stdout.write(buffer.getvalue())
real_stdout.flush()
"""

Killed = Literal["timeout", "output", "memory"] | None


@dataclass(frozen=True)
class Limits:
    """Workshop limits; the values come from authority.json."""

    timeout_s: float
    memory_mb: int
    cpus: float
    pids: int
    work_mb: int
    tmp_mb: int
    output_mb: int


@dataclass(frozen=True)
class WorkshopResult:
    ok: bool
    value: Any | None
    outputs: dict[str, bytes]  # path under /work/out -> content
    error: str | None
    killed: Killed
    duration_s: float
    log: str = ""  # tail of the container's stderr (an ffmpeg error, say), for the forge


def docker_command(name: str, image: str, limits: Limits) -> list[str]:
    return [
        "docker", "run", "--rm", "-i", "--name", name,
        "--network", "none", "--read-only",
        "--tmpfs", f"/work:rw,size={limits.work_mb}m,noexec,mode=1777",
        "--tmpfs", f"/tmp:rw,size={limits.tmp_mb}m,noexec",
        "--memory", f"{limits.memory_mb}m", "--memory-swap", f"{limits.memory_mb}m",
        "--cpus", str(limits.cpus), "--pids-limit", str(limits.pids), "--ulimit", "nofile=256:256",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", "65534:65534", "--env", "GPG_KEY=", "--env", "HOME=/tmp", "--workdir", "/work",
        image, "python", "-I", "-c", RUNNER,
    ]  # fmt: skip


def image_id(image: str = IMAGE) -> str | None:
    """The immutable ID of a local image, or None when it is not built."""
    proc = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def ensure_image(context: Path | None = None, image: str = IMAGE) -> str:
    """Build the workshop image if missing and return its ID. Default context: $CREATURE_HOME/workshop."""
    found = image_id(image)
    if found:
        return found
    folder = context if context is not None else resolve_home() / "workshop"
    subprocess.run(["docker", "build", "-q", "-t", image, str(folder)], check=True, capture_output=True)
    built = image_id(image)
    if not built:
        raise RuntimeError(f"workshop image {image} did not build")
    return built


def _remove(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


def run(
    code: str,
    input: Any,
    files: dict[str, Path] | None = None,
    *,
    limits: Limits,
    image: str = IMAGE,
) -> WorkshopResult:
    """Run `code` (must define run(input, workdir)) in a fresh workshop container."""
    start = time.monotonic()
    name = f"creature-ws-{uuid.uuid4().hex[:12]}"
    try:
        payload = _pack(code, input, files or {})
    except (TypeError, ValueError, OSError) as error:
        return WorkshopResult(False, None, {}, f"bad input: {error}", None, 0.0)
    if not shutil.which("docker"):
        return WorkshopResult(False, None, {}, "docker is not installed", None, 0.0)

    max_bytes = limits.output_mb * 1_000_000
    proc = subprocess.Popen(
        docker_command(name, image, limits),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = bytearray()
    stderr = bytearray()
    overflow = threading.Event()

    def read_stdout() -> None:
        while chunk := proc.stdout.read(65536):
            if len(stdout) + len(chunk) > max_bytes:
                overflow.set()
                return
            stdout.extend(chunk)

    def read_stderr() -> None:
        while chunk := proc.stderr.read(65536):
            stderr.extend(chunk)
            del stderr[:-STDERR_TAIL]  # keep only the tail

    def write_stdin() -> None:
        # on its own thread: a stalled container must not block us before the deadline loop
        try:
            proc.stdin.write(payload)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    threads = [threading.Thread(target=t, daemon=True) for t in (read_stdout, read_stderr, write_stdin)]
    for thread in threads:
        thread.start()

    killed: Killed = None
    deadline = start + limits.timeout_s
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
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        killed = killed or "timeout"
    if killed:
        _remove(name)  # second pass makes "no container left behind" unconditional
    for thread in threads[:2]:
        thread.join(timeout=5)
    duration = round(time.monotonic() - start, 3)
    log = stderr.decode(errors="replace")

    if killed or overflow.is_set():
        killed = killed or "output"
        return WorkshopResult(False, None, {}, f"killed: {killed}", killed, duration, log)
    if proc.returncode == 137:
        return WorkshopResult(False, None, {}, "killed: memory", "memory", duration, log)
    return _unpack(bytes(stdout), proc.returncode, duration, log)


def _pack(code: str, input: Any, files: dict[str, Path]) -> bytes:
    payload = json.dumps({"code": code, "input": input}).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        _add(archive, "payload.json", payload)
        for name, path in files.items():
            if not FILE_NAME.fullmatch(name):
                raise ValueError(f"input file name {name!r} is not a plain file name")
            _add(archive, f"in/{name}", Path(path).read_bytes())
    return buffer.getvalue()


def _add(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o644
    archive.addfile(info, io.BytesIO(data))


def _unpack(data: bytes, returncode: int, duration: float, log: str) -> WorkshopResult:
    def failed(error: str) -> WorkshopResult:
        return WorkshopResult(False, None, {}, error, None, duration, log)

    if not data:
        return failed(f"no result (exit {returncode})")
    outputs: dict[str, bytes] = {}
    result = None
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    return failed(f"unsafe path in output: {member.name}")
                if member.isdir():
                    continue
                if not member.isfile():
                    return failed(f"output is not a regular file: {member.name}")
                content = archive.extractfile(member).read()
                if member.name == "result.json":
                    result = content
                elif path.parts[0] == "out" and len(path.parts) > 1:
                    outputs[str(PurePosixPath(*path.parts[1:]))] = content
                else:
                    return failed(f"unexpected entry in output: {member.name}")
    except tarfile.TarError:
        return failed("result is not an archive")
    try:
        parsed = json.loads(result) if result is not None else None
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, dict) or not ({"value", "error"} & parsed.keys()):
        return failed("result has no value or error")
    if "error" in parsed:
        return WorkshopResult(False, None, outputs, str(parsed["error"]), None, duration, log)
    return WorkshopResult(True, parsed["value"], outputs, None, None, duration, log)
