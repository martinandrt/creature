"""The verdict on one attempt: file checks first, then the judge.

Checks are data from the spec, evaluated here by fixed code. The clip was made by generated code, so
ffprobe and frame extraction run in the workshop, not on the host. The judge is a separate model call
that sees the reel's frames, the clip's frames and the criteria, never code. Pass or fail is decided
here: every criterion sent must come back passed; the model's own verdict is logged, never trusted.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from creature import workshop
from creature.criteria import Spec
from creature.llm import Model

CLIP_PER_S, CLIP_MAX = 6, 36  # frames shown per second of clip, at most: a blink still shows up
BLACK_LUMA = 8  # brightest pixel (0-255) at or below this on every sampled frame means a black clip

# Fixed code (ours) that runs in the workshop on the attempt's clip.
PROBE_CODE = (
    workshop.SHEET_CODE
    + r"""
import json

def run(input, work):
    clip = f"{work}/in/{input['file']}"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_entries",
         "stream=codec_type,codec_name,pix_fmt,width,height,avg_frame_rate,nb_read_frames:format=duration",
         "-of", "json", clip], capture_output=True, text=True)
    if probe.returncode != 0:
        return {"readable": False, "error": probe.stderr[-300:]}
    info = json.loads(probe.stdout)
    video = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    if not video:
        return {"readable": True, "video": False}
    v = video[0]
    num, den = (v.get("avg_frame_rate") or "0/1").split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    frames = int(v.get("nb_read_frames") or 0)
    # frames picked by index, first to last inclusive, so the judge sees how the clip starts and ends
    count = min(input["max"], max(2, round(frames / fps * input["per_s"]))) if fps else 2
    picks = sorted({round(i * (frames - 1) / (count - 1)) for i in range(count)}) if frames > 1 else [0]
    times = [round(n / fps, 3) if fps else 0.0 for n in picks]
    select = "+".join(f"eq(n\\,{n})" for n in picks)
    subprocess.run(["ffmpeg", "-v", "error", "-i", clip, "-vf", f"select='{select}',{FIT}", "-vsync", "vfr",
                    f"{work}/frame_%03d.png"], check=True)
    import os
    tiles = [f"{work}/frame_{i:03d}.png" for i in range(1, len(picks) + 1)]
    tiles = [t for t in tiles if os.path.exists(t)]  # a stream that stops decoding writes fewer
    times = times[:len(tiles)]
    luma = []
    for path in tiles:
        with Image.open(path) as tile:
            # brightest pixel: a small glyph on black is not black
            luma.append(max(tile.convert("L").getdata()))
    if tiles:
        sheets(tiles, times, f"{work}/out/strip")
    diffs = []
    if input.get("diffs"):  # how much each frame differs from the next, on a small grey copy
        small = subprocess.run(["ffmpeg", "-v", "error", "-i", clip, "-vf", "scale=135:240", "-f", "rawvideo",
                                "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
        import numpy as np
        grey = np.frombuffer(small, dtype=np.uint8).reshape(-1, 240, 135).astype(np.int16)
        diffs = [round(float(d), 3) for d in np.abs(np.diff(grey, axis=0)).mean(axis=(1, 2))]
    look = {}
    if input.get("look") and tiles:  # measured exactly as on the reel, so the two can be compared
        look = {"palette": palette(tiles, 6, v.get("width") or TILE_W, v.get("height") or TILE_H),
                "cuts": cuts_of(clip, 0.3)}
    return {**look, "diffs": diffs, "readable": True, "video": True, "codec": v.get("codec_name"),
            "pix_fmt": v.get("pix_fmt"), "width": v.get("width"), "height": v.get("height"), "fps": fps,
            "frames": frames, "duration_s": frames / fps if fps else 0.0, "luma": luma, "times": times,
            "sampled": len(picks), "decoded": len(tiles)}
"""
)

JUDGE_SCHEMA = {
    "type": "object",
    "required": ["criteria", "verdict", "reason"],
    "properties": {
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["criterion", "pass", "evidence"],
                "properties": {
                    "criterion": {"type": "string"},
                    "pass": {"type": "boolean"},
                    "evidence": {"type": "string"},
                },
            },
        },
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "reason": {"type": "string"},
    },
}

JUDGE_SYSTEM = """You judge whether a rendered clip reproduces what a reference reel shows, criterion by
criterion. Judge exactly what each criterion names: its technique, or its look when it speaks of
layout, density or size. Ignore the words themselves and the fonts; colours are measured elsewhere. The
reference reel is often a tutorial, so some of its frames may show an editing app; use the frames that
show the effect. Each image is a grid of frames in time order, 6 per row, each with its time in seconds
under it: up to 24 from the reel (half of them from the middle of its shots), then the clip's frames at
about 6 per second from its first frame to its last, over one or two images. Every frame is its own
cell, separated by grey borders: read positions inside each cell, never across cells, and never take a
cell's place in the grid for movement.
Judge each criterion from the clip's frames alone. Answer every criterion, in the order given, copying
its text exactly. pass is true only if the frames clearly show it; when unsure, false. Evidence
describes the frames only: never mention or quote another criterion."""


@dataclass(frozen=True)
class Checked:
    problems: tuple[str, ...]  # empty when every check passed
    probe: dict[str, Any]
    strip: bytes | None  # the clip's frames, for the judge and the side-by-side page
    more: tuple[bytes, ...] = ()  # further sheets of a long clip's frames


@dataclass(frozen=True)
class Judged:
    ok: bool
    results: tuple[dict[str, Any], ...]  # {criterion, pass, evidence, hidden}
    model_verdict: str
    reason: str


def check(
    outputs: dict[str, bytes],
    checks: tuple[dict[str, Any], ...],
    *,
    limits: workshop.Limits,
    image: str,
    folder: Path,
) -> Checked:
    """Evaluate file checks (data) against the attempt's outputs. Unknown kinds fail closed."""
    names = {c.get("file") for c in checks}
    if len(names) != 1:
        return Checked(("checks must name exactly one file",), {}, None)
    name = names.pop()
    if name not in outputs:
        return Checked((f"exists: {name} was not written",), {}, None)
    clip = folder / name
    clip.write_bytes(outputs[name])
    result = workshop.run(
        PROBE_CODE,
        {
            "file": name,
            "per_s": CLIP_PER_S,
            "max": CLIP_MAX,
            "diffs": any(c.get("kind") == "smooth" for c in checks),
            "look": any(c.get("kind") in ("palette", "rhythm") for c in checks),
        },
        {name: clip},
        limits=limits,
        image=image,
    )
    probe = result.value if result.ok and isinstance(result.value, dict) else {}
    problems = list(dict.fromkeys(p for c in checks if (p := _evaluate(c, outputs[name], probe))))
    if probe.get("decoded", 0) < probe.get("sampled", 0):
        problems.append(
            f"decodes: only {probe['decoded']} of {probe['sampled']} sampled frames decode (the stream stops)"
        )
    if not result.ok:
        problems = [f"the clip cannot be read: {result.error}"]
    more = tuple(result.outputs[n] for n in sorted(result.outputs) if n.startswith("strip-"))
    return Checked(tuple(problems), probe, result.outputs.get("strip.png"), more)


KINDS = (
    "exists", "max_bytes", "video_stream", "resolution", "fps", "duration", "frames", "not_black", "smooth",
    "safe_zone", "palette", "rhythm",
)  # fmt: skip
MAIN_SHARE = 0.10  # a clip colour covering at least this share must come from the reel's palette
STILL = 1.0  # mean grey change per frame below which nothing is moving (encoder noise)
JUMP = 25.0  # above this a frame changes at once (a flip, a cut inside a source): not motion


def _evaluate(check: dict[str, Any], data: bytes, probe: dict[str, Any]) -> str | None:
    kind = check.get("kind")
    if kind not in KINDS:
        return f"unknown check kind {kind!r}"
    if kind == "exists":
        return None if data else "exists: the clip is empty"
    if kind == "max_bytes":
        return None if len(data) <= check["expect"] else f"max_bytes: {len(data)} bytes > {check['expect']}"
    if kind == "safe_zone":
        return _safe(check)
    if not probe.get("readable"):
        return "the clip cannot be read"
    if kind == "video_stream":
        ok = probe.get("video") and probe.get("codec") == "h264" and probe.get("pix_fmt") == "yuv420p"
        return (
            None
            if ok
            else f"video_stream: need h264 yuv420p, got {probe.get('codec')} {probe.get('pix_fmt')}"
        )
    if not probe.get("video"):
        return f"{kind}: no video stream"
    if kind == "resolution":
        got = (probe.get("width"), probe.get("height"))
        return (
            None
            if got == (check["width"], check["height"])
            else f"resolution: {got[0]}x{got[1]}, need {check['width']}x{check['height']}"
        )
    if kind == "fps":
        return _near("fps", probe.get("fps"), check)
    if kind == "duration":
        return _near("duration", probe.get("duration_s"), check)
    if kind == "frames":
        got = probe.get("frames")
        return None if got == check["expect"] else f"frames: got {got}, need exactly {check['expect']}"
    if kind == "smooth":
        return _smooth(probe.get("diffs") or [], set(check["cuts"]), check["max_ratio"])
    if kind == "palette":
        return _palette(probe.get("palette") or [], check["colors"], check["distance"])
    if kind == "rhythm":
        return _rhythm(probe, check)
    luma = probe.get("luma") or []  # the one kind left: not_black
    return None if luma and max(luma) > BLACK_LUMA else "not_black: every sampled frame is black"


def _smooth(diffs: list[float], cuts: set[int], max_ratio: float) -> str | None:
    """No stutter while something moves. diffs[i] is the change from frame i to i + 1. An ease is
    monotone (each step lies between its neighbours), a stutter is a local extreme: a step more than
    `max_ratio` times both neighbours (a dropped frame) or below both by that factor (a frame held).
    Judged only where both neighbours move, so a typewriter's pauses or a blink stay out; steps across
    a cut and jumps (a flip of the whole frame) are not motion."""
    if not diffs:
        return "smooth: the clip's motion could not be measured"
    for i in range(1, len(diffs) - 1):
        if cuts & {i, i + 1, i + 2}:  # one of the three steps crosses a cut
            continue
        before, step, after = diffs[i - 1], diffs[i], diffs[i + 1]
        if min(before, after) <= STILL or max(before, step, after) >= JUMP:
            continue
        if step > max_ratio * max(before, after):
            jump = step / max(before, after)
            return f"smooth: the change at frame {i + 1} is {jump:.1f}x its neighbours (a dropped frame)"
        if step * max_ratio < min(before, after):
            return f"smooth: frame {i + 1} holds in the middle of a movement (a repeated frame)"
    return None


def _palette(measured: list[dict[str, Any]], colors: list[str], distance: float) -> str | None:
    """Every main colour of the clip must be close to one of the reel's (or the user's style's)."""
    if not measured:
        return "palette: the clip's colours could not be measured"
    allowed = [_rgb(c) for c in colors]
    for color in measured:
        if color.get("share", 0) < MAIN_SHARE:
            continue
        rgb = _rgb(color["hex"])
        nearest = min(sum(abs(a - b) for a, b in zip(rgb, other, strict=True)) for other in allowed)
        if nearest > distance:
            return (
                f"palette: {color['hex']} covers {color['share']:.0%} of the clip but is not one of the "
                f"reel's colours ({', '.join(colors)})"
            )
    return None


def _rhythm(probe: dict[str, Any], check: dict[str, Any]) -> str | None:
    """Shots last about as long as the reel's: the median shot within the tolerance of the reel's."""
    cuts = sorted(probe.get("cuts") or [])
    fps, frames = probe.get("fps") or 0, probe.get("frames") or 0
    if not fps or not frames:
        return "rhythm: the clip's cuts could not be measured"
    bounds = [0.0, *[c for c in cuts if 0 < c * fps < frames], frames / fps]
    shots = sorted((b - a) * fps for a, b in itertools.pairwise(bounds) if b > a)
    median = shots[len(shots) // 2] if shots else frames
    want, tolerance = check["frames_per_shot"], check["tolerance"]
    if abs(median - want) <= want * tolerance:
        return None
    return f"rhythm: a shot lasts about {median:.0f} frames, in the reel about {want:.0f} (± {tolerance:.0%})"


def _rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _safe(check: dict[str, Any]) -> str | None:
    safe = check["safe"]
    for x0, y0, x1, y1 in check["boxes"]:
        if x0 < safe["left"] or y0 < safe["top"] or x1 > 1 - safe["right"] or y1 > 1 - safe["bottom"]:
            return f"safe_zone: a layer at {x0:.2f},{y0:.2f} to {x1:.2f},{y1:.2f} reaches the margins {safe}"
    return None


def _near(kind: str, value: Any, check: dict[str, Any]) -> str | None:
    if (
        isinstance(value, int | float)
        and math.isfinite(value)
        and abs(value - check["expect"]) <= check["tolerance"]
    ):
        return None
    return f"{kind}: got {value}, need {check['expect']} ± {check['tolerance']}"


def judge(
    model: Model,
    spec: Spec,
    reel_strip: Path,
    clip_strip: Path,
    *,
    cap_usd: float,
    more: tuple[Path, ...] = (),
) -> Judged:
    """Ask the judge about every criterion, visible and held out; decide pass here."""
    sent = list(spec.criteria) + list(spec.held_out)
    prompt = "Criteria:\n" + "\n".join(f"{i + 1}. {c}" for i, c in enumerate(sent))
    images = (
        ("reference reel", reel_strip),
        ("clip to judge", clip_strip),
        *(("clip to judge, continued", path) for path in more),
    )
    data = model.ask("judge", JUDGE_SYSTEM, prompt, JUDGE_SCHEMA, cap_usd=cap_usd, images=images)
    answers = [a for a in data.get("criteria", []) if isinstance(a, dict)]
    by_text = {str(a.get("criterion", "")).strip(): a for a in answers}
    results = []
    for index, criterion in enumerate(sent):
        answer = by_text.get(criterion)
        if answer is None and len(answers) == len(sent):
            answer = answers[index]  # same order, reworded text
        passed = bool(answer and answer.get("pass") is True)
        evidence = str(answer.get("evidence", "")) if answer else "no answer for this criterion"
        results.append(
            {
                "criterion": criterion,
                "pass": passed,
                "evidence": evidence[:400],
                "hidden": index >= len(spec.criteria),
            }
        )
    return Judged(
        all(r["pass"] for r in results),
        tuple(results),
        str(data.get("verdict")),
        str(data.get("reason"))[:500],
    )


def feedback(checked: Checked, judged: Judged | None) -> str:
    """What the forge may read about a failed attempt: never the text of a held-out criterion."""
    lines = [f"Check failed: {p}" for p in checked.problems]
    if judged:
        secret = [r["criterion"].lower() for r in judged.results if r["hidden"]]
        for r in judged.results:
            if not r["pass"] and not r["hidden"]:
                # the judge saw every criterion and may quote a hidden one: then its evidence stays out
                quoted = any(text in r["evidence"].lower() for text in secret)
                seen = "(evidence withheld)" if quoted else r["evidence"]
                lines.append(f"Judge, not met: {r['criterion']} (seen: {seen})")
        hidden = sum(1 for r in judged.results if not r["pass"] and r["hidden"])
        if hidden:
            lines.append(f"{hidden} more criteria you were not shown also failed.")
    return "\n".join(lines)
