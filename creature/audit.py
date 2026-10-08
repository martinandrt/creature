"""A model-free measure of how much an output clip resembles the reel it was made from.

No model is involved: ffprobe and ffmpeg on the host decode a handful of small frames and numpy compares
them, so the same files always give the same numbers. This reads finished clips only (the sandbox rule is
about generated code) and never writes next to them.

Parts of the measure, for one run:

  coverage   min(1, clip duration / reel duration).
  frame      K_FRAMES times spread over the CLIP's duration; at each time the clip frame and the reel frame
             at the same time (the reel's time modulo its length when the clip is longer) are scaled to one
             small canvas in the REEL's orientation (aspect kept, black bars) and compared by the mean
             absolute RGB difference (0-255). That difference becomes a 0-100 resemblance, 100 = identical.
  colour     the 5 most common colours of each video (channels quantised to 32 levels, frames pooled), and
             the share of clip pixels within PALETTE_REACH of a reel palette colour.
  orientation  portrait, landscape or square on both sides must agree.

The overall score is a weighted blend of the first three; an orientation mismatch caps it.

    python -m creature.audit OUT.html [HOME ...]

writes one page over every finished run that has a clip. A HOME is a creature home (it has a `runs/` folder)
or a folder of homes; with no HOME, every home next to OUT.html is used. Runs without a `run_end` event are
still going and are skipped. The strips go to `audit/` next to OUT.html. Nothing in the homes is written.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import statistics
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import numpy as np

K_FRAMES = 12  # times sampled over the clip for the frame comparison
STRIP_FRAMES = 6  # frames per row in the side-by-side strip on the page
# comparison canvas (width, height) by the reel's orientation: too small for detail, big enough for layout
CANVAS = {"portrait": (54, 96), "landscape": (96, 54), "square": (72, 72)}
STRIP_TILE = {"portrait": (72, 128), "landscape": (128, 72), "square": (96, 96)}
SQUARE_TOLERANCE = 0.05  # |width/height - 1| up to this is "square"

# A clip against frames of other, unrelated reels differs by about 105 on average (median of 1318 pairs from
# the first real run, 2026-10-09; the reels are mostly flat dark or light type designs, so that is a lot).
# A frame that far off resembles nothing, so 0; a linear 0-255 scale would give two unrelated frames about 60.
FRAME_ZERO_MAD = 100.0
LEVELS = 32  # colour quantisation per channel
PALETTE_SIZE = 5
PALETTE_REACH = 60.0  # RGB distance: a clip pixel this close to a reel palette colour is "on palette"

# Weights of the blend, summing to 1. Frames are the only part that sees layout and timing against the reel,
# so they weigh most. Colour is robust to layout and motion but coarse (a palette says little about shape).
# Coverage is a sanity check on length: a clip of any content can reach 1, so it weighs least.
W_FRAME = 0.5
W_COLOUR = 0.3
W_COVERAGE = 0.2
ORIENTATION_CAP = 30.0  # a clip in the wrong orientation cannot score above this, whatever else matches
LOW_SCORE = 40.0  # a BUILT or HAVE run below this is a success that does not resemble its reel
SUCCESS = ("BUILT", "HAVE")
WORKERS = 3  # runs measured at once; each ffmpeg call is tiny and single-threaded

CLIP_FALLBACKS = ("attempt-*/clip.mp4", "have-*/clip.mp4", "montage*/montage.mp4", "joined.mp4")


class AuditError(Exception):
    """A video could not be read or compared; the run is listed as not measured."""


@dataclass(frozen=True)
class Video:
    width: int
    height: int
    duration: float

    @property
    def orientation(self) -> str:
        ratio = self.width / self.height
        if abs(ratio - 1) <= SQUARE_TOLERANCE:
            return "square"
        return "portrait" if ratio < 1 else "landscape"


@dataclass(frozen=True)
class Measure:
    score: float  # 0-100, the blend with the orientation cap applied
    frame: float  # 0-100 resemblance
    frame_mad: float  # mean absolute RGB difference, 0-255, behind it
    colour: float  # 0-1 share of clip pixels on the reel palette
    coverage: float  # 0-1
    orientation_match: bool
    reel: Video
    clip: Video
    reel_palette: tuple[str, ...]
    clip_palette: tuple[str, ...]


def blend(frame: float, colour: float, coverage: float) -> float:
    """The uncapped 0-100 score from frame (0-100), colour (0-1) and coverage (0-1)."""
    return W_FRAME * frame + W_COLOUR * 100 * colour + W_COVERAGE * 100 * coverage


def overall(frame: float, colour: float, coverage: float, orientation_match: bool) -> float:
    score = blend(frame, colour, coverage)
    return score if orientation_match else min(score, ORIENTATION_CAP)


def frame_resemblance(mad: float) -> float:
    return 100.0 * max(0.0, 1.0 - mad / FRAME_ZERO_MAD)


# ---------------------------------------------------------------- video access (ffprobe, ffmpeg)


def _run(cmd: list[str]) -> bytes:
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AuditError(f"{cmd[0]}: {exc}") from exc
    if done.returncode != 0:
        tail = done.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["failed"]
        raise AuditError(f"{cmd[0]}: {tail[0]}")
    return done.stdout


def probe(path: Path) -> Video:
    out = _run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-of", "json", "-show_entries",
         "stream=width,height,duration:stream_side_data=rotation:stream_tags=rotate:format=duration",
         str(path)]
    )  # fmt: skip
    try:
        info = json.loads(out)
        stream = info["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        rotation = stream.get("tags", {}).get("rotate")
        for data in stream.get("side_data_list", []):
            rotation = data.get("rotation", rotation)
        duration = float(info.get("format", {}).get("duration") or stream.get("duration") or 0)
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise AuditError(f"{path.name}: not a readable video ({exc!r})") from exc
    if rotation is not None and int(float(rotation)) % 180 == 90:
        width, height = height, width  # ffmpeg applies the rotation when it decodes
    if width <= 0 or height <= 0 or not math.isfinite(duration) or duration <= 0:
        raise AuditError(f"{path.name}: no usable size or duration")
    return Video(width, height, duration)


def fit_filter(width: int, height: int) -> str:
    """Scale into width x height keeping the aspect, black bars on the rest."""
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=area,format=rgb24,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )


def grab(path: Path, t: float, size: tuple[int, int]) -> np.ndarray:
    """One RGB frame (height, width, 3) at time t; a little earlier when t is past the last frame."""
    width, height = size
    for at in (t, max(0.0, t - 0.2), 0.0):
        raw = _run(
            ["ffmpeg", "-v", "error", "-nostdin", "-threads", "1", "-ss", f"{at:.3f}", "-i", str(path),
             "-an", "-sn", "-frames:v", "1", "-vf", fit_filter(width, height),
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        )  # fmt: skip
        if len(raw) == width * height * 3:
            return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
    raise AuditError(f"{path.name}: no frame at {t:.2f}s")


def sample_times(duration: float, count: int) -> list[float]:
    """The middles of `count` equal slices of the duration."""
    return [(i + 0.5) * duration / count for i in range(count)]


def grab_many(path: Path, times: Iterable[float], size: tuple[int, int]) -> np.ndarray:
    return np.stack([grab(path, t, size) for t in times])


# ---------------------------------------------------------------- the measure itself


def palette(frames: np.ndarray) -> np.ndarray:
    """The PALETTE_SIZE most common quantised colours of the pooled frames, as (n, 3) bin-centre RGB."""
    step = 256 // LEVELS
    q = frames.reshape(-1, 3).astype(np.int64) // step
    counts = np.bincount((q[:, 0] * LEVELS + q[:, 1]) * LEVELS + q[:, 2], minlength=LEVELS**3)
    top = np.argsort(-counts, kind="stable")[:PALETTE_SIZE]  # stable: ties go to the lower bin, always
    top = top[counts[top] > 0]
    rgb = np.stack([top // (LEVELS * LEVELS), (top // LEVELS) % LEVELS, top % LEVELS], axis=1)
    return rgb * step + step // 2


def colour_share(frames: np.ndarray, colours: np.ndarray) -> float:
    """Share of the pixels of `frames` within PALETTE_REACH (RGB distance) of any colour in `colours`."""
    pixels = frames.reshape(-1, 3).astype(np.int32)
    gap = pixels[:, None, :] - colours[None, :, :].astype(np.int32)
    nearest = np.sqrt((gap * gap).sum(axis=2)).min(axis=1)
    return float((nearest <= PALETTE_REACH).mean())


def hex_colours(colours: np.ndarray) -> tuple[str, ...]:
    return tuple(f"#{int(r):02x}{int(g):02x}{int(b):02x}" for r, g, b in colours)


def measure(reel: Path, clip: Path) -> Measure:
    reel_v, clip_v = probe(reel), probe(clip)
    size = CANVAS[reel_v.orientation]
    times = sample_times(clip_v.duration, K_FRAMES)
    clip_frames = grab_many(clip, times, size)
    reel_frames = grab_many(reel, [t % reel_v.duration for t in times], size)
    mad = float(np.abs(clip_frames.astype(np.int16) - reel_frames.astype(np.int16)).mean())
    reel_palette = palette(reel_frames)
    frame = frame_resemblance(mad)
    colour = colour_share(clip_frames, reel_palette)
    coverage = min(1.0, clip_v.duration / reel_v.duration)
    match = clip_v.orientation == reel_v.orientation
    return Measure(
        score=overall(frame, colour, coverage, match),
        frame=frame,
        frame_mad=mad,
        colour=colour,
        coverage=coverage,
        orientation_match=match,
        reel=reel_v,
        clip=clip_v,
        reel_palette=hex_colours(reel_palette),
        clip_palette=hex_colours(palette(clip_frames)),
    )


def write_strip(reel: Path, clip: Path, m: Measure, out: Path) -> None:
    """A JPEG with STRIP_FRAMES reel frames over STRIP_FRAMES clip frames, same times as in the measure."""
    size = STRIP_TILE[m.reel.orientation]
    times = sample_times(m.clip.duration, STRIP_FRAMES)
    top = np.concatenate(list(grab_many(reel, [t % m.reel.duration for t in times], size)), axis=1)
    bottom = np.concatenate(list(grab_many(clip, times, size)), axis=1)
    image = np.concatenate([top, bottom], axis=0)
    height, width = image.shape[:2]
    out.parent.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
         "-i", "-", "-frames:v", "1", "-pix_fmt", "yuvj420p", "-q:v", "4", str(out)],
        input=image.tobytes(), capture_output=True, timeout=60,
    )  # fmt: skip
    if done.returncode != 0:
        raise AuditError(f"strip: {done.stderr.decode(errors='replace').strip()[-200:]}")


# ---------------------------------------------------------------- finding the runs


@dataclass(frozen=True)
class RunRef:
    home: str
    run_id: str
    run_dir: Path
    status: str
    skill: str | None
    source: str
    clip: Path | None
    clip_inferred: bool = False  # the ledger named no clip; the newest one in the folder was taken
    clip_named: str = ""  # what the ledger calls the clip, even when the file is not there


@dataclass
class Discovery:
    runs: list[RunRef] = field(default_factory=list)  # finished, with a clip file
    no_clip: list[RunRef] = field(default_factory=list)  # finished, nothing to compare
    in_progress: int = 0  # ledgers without run_end


@dataclass(frozen=True)
class Row:
    ref: RunRef
    measure: Measure | None
    error: str = ""
    strip: Path | None = None


def read_events(path: Path) -> list[dict]:
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue  # a run in progress can end on a half-written line
        if isinstance(event, dict):
            events.append(event)
    return events


def _newest(folder: Path, pattern: str) -> Path | None:
    def number(p: Path) -> int:
        found = re.findall(r"\d+", p.parent.name)
        return int(found[-1]) if found else 0

    found = sorted(folder.glob(pattern), key=lambda p: (number(p), p.parent.name))
    return found[-1] if found else None


def find_clip(run_dir: Path, end: dict) -> tuple[Path | None, bool]:
    """The clip file of a finished run, and whether it was inferred because the ledger names none."""
    named = end.get("clip")
    if isinstance(named, str) and named:
        path = (run_dir / named).resolve()
        inside = path.is_relative_to(run_dir.resolve())  # a ledger is data: it does not point outside
        return (path, False) if inside and path.is_file() else (None, False)
    for pattern in CLIP_FALLBACKS:
        path = _newest(run_dir, pattern)
        if path is not None:
            return path, True
    return None, False


def _source_name(events: list[dict]) -> str:
    for event in events:
        if event.get("type") == "task":
            source = event.get("source")
            if isinstance(source, str) and source:
                return Path(source).name
            if isinstance(source, list):
                return ", ".join(Path(str(s)).name for s in source)
    return ""


def homes_in(args: Sequence[Path], fallback_root: Path) -> list[Path]:
    """Creature homes (folders with runs/) among the arguments or, failing those, directly in them."""
    found: list[Path] = []
    for arg in args or [fallback_root]:
        arg = arg.expanduser()
        if (arg / "runs").is_dir():
            found.append(arg)
        elif arg.is_dir():
            found.extend(p for p in sorted(arg.iterdir()) if (p / "runs").is_dir())
    return found


def discover(homes: Iterable[Path]) -> Discovery:
    found = Discovery()
    for home in homes:
        for ledger in sorted((home / "runs").glob("*.jsonl")):
            events = read_events(ledger)
            end = next((e for e in reversed(events) if e.get("type") == "run_end"), None)
            if end is None:
                found.in_progress += 1
                continue
            run_dir = ledger.with_suffix("")
            clip, inferred = find_clip(run_dir, end) if run_dir.is_dir() else (None, False)
            skill = end.get("skill")
            ref = RunRef(
                home=home.name,
                run_id=ledger.stem,
                run_dir=run_dir,
                status=str(end.get("status") or "?"),
                skill=skill if isinstance(skill, str) else None,
                source=_source_name(events),
                clip=clip,
                clip_inferred=inferred,
                clip_named=named if isinstance(named := end.get("clip"), str) else "",
            )
            (found.runs if clip else found.no_clip).append(ref)
    return found


# ---------------------------------------------------------------- the page


def _slug(ref: RunRef) -> str:
    digest = hashlib.sha1(str(ref.run_dir.resolve()).encode()).hexdigest()[:6]
    return re.sub(r"[^A-Za-z0-9._-]", "_", f"{ref.home}__{ref.run_id}") + f"-{digest}"


def audit_run(ref: RunRef, strip_dir: Path) -> Row:
    reel = ref.run_dir / "reel.mp4"
    if ref.clip is None or not reel.is_file():
        return Row(ref, None, "no reel.mp4 in the run folder" if not reel.is_file() else "no clip")
    try:
        m = measure(reel, ref.clip)
        strip = strip_dir / f"{_slug(ref)}.jpg"
        write_strip(reel, ref.clip, m, strip)
    except AuditError as exc:
        return Row(ref, None, str(exc))
    return Row(ref, m, strip=strip)


def audit(homes: Sequence[Path], out: Path) -> tuple[list[Row], Discovery]:
    """Measure every run with a clip and write the page; rows come back worst first."""
    found = discover(homes)
    strip_dir = out.parent / "audit"
    strip_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        rows = list(pool.map(lambda ref: audit_run(ref, strip_dir), found.runs))
    rows.sort(key=lambda r: (r.measure.score if r.measure else math.inf, r.ref.home, r.ref.run_id))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(rows, found, out), encoding="utf-8")
    return rows, found


@dataclass(frozen=True)
class StatusLine:
    status: str
    runs: int
    median: float
    below: int


def status_lines(rows: Iterable[Row]) -> list[StatusLine]:
    scores: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.measure:
            scores[row.ref.status].append(row.measure.score)
    return [
        StatusLine(s, len(v), statistics.median(v), sum(x < LOW_SCORE for x in v))
        for s, v in sorted(scores.items())
    ]


def low_successes(rows: Iterable[Row]) -> list[Row]:
    return [r for r in rows if r.measure and r.ref.status in SUCCESS and r.measure.score < LOW_SCORE]


CSS = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1b1d21;--mute:#667085;--line:#e3e5e8;--card:#f6f7f9;
--bad:#b42318;--warn:#a15c07;--ok:#157347;--bar:#d0d5dd}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e8eaed;--mute:#98a2b3;--line:#2b2f36;
--card:#1c1f25;--bad:#f97066;--warn:#fec84b;--ok:#47cd89;--bar:#3a3f48}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:24px 0 8px}
.mute{color:var(--mute)}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:12px 16px;margin:12px 0}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;vertical-align:top;padding:8px 10px;border-bottom:1px solid var(--line)}
th{font-size:12px;color:var(--mute);font-weight:600;white-space:nowrap}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.summary{width:auto}.summary td,.summary th{padding:4px 18px 4px 0}
.score{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums}
.bad{color:var(--bad)}.warn{color:var(--warn)}.ok{color:var(--ok)}
.bar{height:4px;width:80px;background:var(--bar);border-radius:2px;margin-top:4px}
.bar i{display:block;height:100%;background:currentColor;border-radius:2px}
tr.low{background:color-mix(in srgb,var(--bad) 8%,transparent)}
.tag{font-size:11px;font-weight:700;padding:1px 6px;border:1px solid var(--line);border-radius:4px}
.sw{display:flex;gap:2px;margin-top:3px}.sw i{width:12px;height:12px;border:1px solid var(--line)}
img{display:block;max-width:none;border:1px solid var(--line)}
a{color:inherit}code{font-size:12px}
.id{font-family:ui-monospace,monospace;font-size:12px}
"""


def _tone(score: float) -> str:
    return "bad" if score < LOW_SCORE else "warn" if score < 60 else "ok"


def _swatches(colours: Iterable[str], label: str) -> str:
    chips = "".join(f'<i style="background:{c}" title="{c}"></i>' for c in colours)
    return f'<div class="sw" title="{label}">{chips}</div>'


def _href(path: Path, out: Path) -> str:
    return quote(os.path.relpath(path, out.parent))


def _row_html(row: Row, out: Path) -> str:
    ref, m = row.ref, row.measure
    assert m is not None
    anchor = html.escape(_slug(ref))
    low = ref.status in SUCCESS and m.score < LOW_SCORE
    tone = _tone(m.score)
    orient = (
        "match"
        if m.orientation_match
        else f'<b class="bad">MISMATCH</b><br><span class="mute">reel {m.reel.orientation},'
        f" clip {m.clip.orientation}</span>"
    )
    clip_note = " (clip picked from the folder: the ledger names none)" if ref.clip_inferred else ""
    links = f'<a href="{_href(ref.run_dir / "reel.mp4", out)}">reel</a> · '
    links += f'<a href="{_href(ref.clip, out)}">clip</a>' if ref.clip else ""
    strip = (
        f'<img src="{_href(row.strip, out)}" loading="lazy" alt="top row reel, bottom row clip">'
        if row.strip
        else ""
    )
    return (
        f'<tr id="{anchor}"{" class=low" if low else ""}>'
        f"<td>{html.escape(ref.home)}</td>"
        f'<td class="id">{html.escape(ref.run_id)}'
        f'<br><span class="mute">{html.escape(ref.skill or "")}</span></td>'
        f"<td>{html.escape(ref.source)}</td>"
        f'<td><span class="tag">{html.escape(ref.status)}</span></td>'
        f'<td class="n"><div class="score {tone}">{m.score:.1f}</div>'
        f'<div class="bar {tone}"><i style="width:{m.score:.0f}%"></i></div></td>'
        f'<td class="n" title="mean absolute RGB difference {m.frame_mad:.1f} of 255">{m.frame:.0f}</td>'
        f'<td class="n">{m.colour * 100:.0f}%'
        f"{_swatches(m.reel_palette, 'reel palette')}{_swatches(m.clip_palette, 'clip palette')}</td>"
        f'<td class="n" title="clip {m.clip.duration:.1f}s of reel {m.reel.duration:.1f}s">'
        f"{m.coverage * 100:.0f}%</td>"
        f"<td>{orient}</td>"
        f"<td>{strip}<div class=mute>top reel, bottom clip{html.escape(clip_note)}</div>{links}</td>"
        "</tr>"
    )


def render(rows: Sequence[Row], found: Discovery, out: Path) -> str:
    measured = [r for r in rows if r.measure]
    failed = [r for r in rows if not r.measure]
    lines = status_lines(measured)
    low = low_successes(measured)
    successes = sum(1 for r in measured if r.ref.status in SUCCESS)
    summary = "".join(
        f'<tr><td><span class="tag">{html.escape(s.status)}</span></td><td class="n">{s.runs}</td>'
        f'<td class="n">{s.median:.1f}</td><td class="n">{s.below}</td></tr>'
        for s in lines
    )
    low_links = ", ".join(
        f'<a href="#{html.escape(_slug(r.ref))}">{html.escape(r.ref.home)}/{html.escape(r.ref.run_id)}</a>'
        f" ({r.measure.score:.0f})"
        for r in low
        if r.measure
    )
    no_clip = defaultdict(int)
    for ref in found.no_clip:
        no_clip[ref.status] += 1
    no_clip_text = ", ".join(f"{n} {html.escape(s)}" for s, n in sorted(no_clip.items())) or "none"
    body = "".join(_row_html(r, out) for r in measured)
    unmeasured = "".join(
        f"<tr><td>{html.escape(r.ref.home)}</td><td class=id>{html.escape(r.ref.run_id)}</td>"
        f"<td>{html.escape(r.ref.status)}</td><td>{html.escape(r.error)}</td></tr>"
        for r in failed
    ) + "".join(
        f"<tr><td>{html.escape(r.home)}</td><td class=id>{html.escape(r.run_id)}</td>"
        f"<td>{html.escape(r.status)}</td>"
        f"<td>the ledger names {html.escape(r.clip_named)}, not there</td></tr>"
        for r in found.no_clip
        if r.clip_named
    )
    unmeasured_table = (
        "<h2>Not measured</h2><table><tr><th>home</th><th>run</th><th>status</th><th>why</th></tr>"
        f"{unmeasured}</table>"
        if unmeasured
        else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Creature audit: resemblance to the reel</title><style>{CSS}</style></head><body>
<h1>Does the output look like its reel?</h1>
<p class="mute">Model-free: {len(measured)} runs with a clip, worst first. Score 0-100 =
{W_FRAME:g} frame resemblance + {W_COLOUR:g} colour share + {W_COVERAGE:g} coverage; a wrong orientation
caps it at {ORIENTATION_CAP:g}.</p>
<div class="card"><table class="summary"><tr><th>status</th><th class="n">runs</th>
<th class="n">median score</th><th class="n">below {LOW_SCORE:g}</th></tr>{summary}</table>
<p><b>{len(low)} of {successes}</b> BUILT/HAVE runs score below {LOW_SCORE:g}: successes that do not
resemble the reel.{" " + low_links if low_links else ""}</p>
<p class="mute">Not in the table: finished without a clip: {no_clip_text}; still running (no run_end):
{found.in_progress}; clip found but not measured: {len(failed)}.</p></div>
<table><thead><tr><th>home</th><th>run</th><th>source</th><th>status</th><th class="n">score</th>
<th class="n" title="0-100">frame</th><th class="n">colour on palette</th><th class="n">coverage</th>
<th>orientation</th><th>top: 6 reel frames, bottom: 6 clip frames</th></tr></thead>
<tbody>{body}</tbody></table>
{unmeasured_table}
<h2>How it is measured</h2>
<p class="mute">frame: {K_FRAMES} times over the clip, the reel frame at the same time (modulo its length),
both letterboxed to a {"x".join(map(str, CANVAS["portrait"]))} canvas in the reel's orientation; mean absolute
RGB difference d of 255 gives 100 x (1 - d / {FRAME_ZERO_MAD:g}), floored at 0. colour: the
{PALETTE_SIZE} most common colours of the reel ({LEVELS} levels per channel), share of clip pixels within
{PALETTE_REACH:g} of one. coverage: min(1, clip length / reel length).</p>
</body></html>
"""


# ---------------------------------------------------------------- command line


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m creature.audit", description=__doc__.split("\n")[0])
    parser.add_argument("out", type=Path, help="the HTML page to write; audit/ goes next to it")
    parser.add_argument("homes", nargs="*", type=Path, help="creature homes, or folders of them")
    args = parser.parse_args(argv)
    out = args.out.expanduser().resolve()
    homes = homes_in([h.resolve() for h in args.homes], out.parent)
    if not homes:
        print("no creature home (a folder with runs/) found", file=sys.stderr)
        return 2
    rows, found = audit(homes, out)
    measured = [r for r in rows if r.measure]
    for line in status_lines(measured):
        head = f"{line.status:7} {line.runs:3} runs  median {line.median:5.1f}"
        print(f"{head}  below {LOW_SCORE:g}: {line.below}")
    print(
        f"{len(measured)} measured, {len(rows) - len(measured)} not measured,"
        f" {len(found.no_clip)} without a clip, {found.in_progress} still running -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
