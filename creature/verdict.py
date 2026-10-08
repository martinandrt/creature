"""The verdict on one attempt: file checks first, then the judge.

Checks are data from the spec, evaluated here by fixed code. The clip was made by generated code, so
ffprobe and frame extraction run in the workshop, not on the host. The judge is a separate model call
that sees the reel's frames, the clip's frames and the criteria, never code. Pass or fail is decided
here: every criterion sent must come back passed; the model's own verdict is logged, never trusted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from creature import workshop
from creature.criteria import Spec
from creature.llm import Model

BLACK_LUMA = 8.0  # mean luma (0-255) at or below this on every sampled frame means a black clip

# Fixed code (ours) that runs in the workshop on the attempt's clip.
PROBE_CODE = r"""
import json, subprocess

FIT = "scale=180:320:force_original_aspect_ratio=decrease,pad=180:320:(ow-iw)/2:(oh-ih)/2"

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
    count = input["count"]
    picks = sorted({round(i * (frames - 1) / (count - 1)) for i in range(count)}) if frames > 1 else [0]
    times = [round(n / fps, 3) if fps else 0.0 for n in picks]
    select = "+".join(f"eq(n\\,{n})" for n in picks)
    subprocess.run(["ffmpeg", "-v", "error", "-i", clip, "-vf", f"select='{select}',{FIT}", "-vsync", "vfr",
                    f"{work}/frame_%02d.png"], check=True)
    luma = []
    for i in range(1, len(picks) + 1):
        gray = subprocess.run(["ffmpeg", "-v", "error", "-i", f"{work}/frame_{i:02d}.png", "-f", "rawvideo",
                               "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
        luma.append(sum(gray) / max(1, len(gray)))
    subprocess.run(["ffmpeg", "-v", "error", "-start_number", "1", "-i", f"{work}/frame_%02d.png",
                    "-vf", "tile=6x2", "-frames:v", "1", f"{work}/out/strip.png"], check=True)
    return {"readable": True, "video": True, "codec": v.get("codec_name"), "pix_fmt": v.get("pix_fmt"),
            "width": v.get("width"), "height": v.get("height"), "fps": fps, "frames": frames,
            "duration_s": frames / fps if fps else 0.0, "luma": luma, "times": times}
"""

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

JUDGE_SYSTEM = """You judge whether a rendered clip reproduces the TECHNIQUE of a motion design effect
shown in a reference reel. Compare technique only: ignore the words, fonts, colours and layout. The
reference reel is a tutorial, so many of its frames show an editing app; use the frames that show the
effect. Each image is a strip of 12 frames in time order, 6 per row; the clip's strip runs from its
first frame to its last.
Judge each criterion from the clip's frames alone. Answer every criterion, in the order given, copying
its text exactly. pass is true only if the frames clearly show it; when unsure, false."""


@dataclass(frozen=True)
class Checked:
    problems: tuple[str, ...]  # empty when every check passed
    probe: dict[str, Any]
    strip: bytes | None  # the clip's frames, for the judge and the side-by-side page


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
    result = workshop.run(PROBE_CODE, {"file": name, "count": 12}, {name: clip}, limits=limits, image=image)
    probe = result.value if result.ok and isinstance(result.value, dict) else {}
    problems = [p for c in checks if (p := _evaluate(c, outputs[name], probe))]
    if not result.ok:
        problems.append(f"probe failed: {result.error}")
    return Checked(tuple(problems), probe, result.outputs.get("strip.png"))


KINDS = ("exists", "max_bytes", "video_stream", "resolution", "fps", "duration", "not_black")


def _evaluate(check: dict[str, Any], data: bytes, probe: dict[str, Any]) -> str | None:
    kind = check.get("kind")
    if kind not in KINDS:
        return f"unknown check kind {kind!r}"
    if kind == "exists":
        return None if data else "exists: the clip is empty"
    if kind == "max_bytes":
        return None if len(data) <= check["expect"] else f"max_bytes: {len(data)} bytes > {check['expect']}"
    if not probe.get("readable"):
        return f"{kind}: the clip cannot be read"
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
    luma = probe.get("luma") or []  # the one kind left: not_black
    return None if luma and max(luma) > BLACK_LUMA else "not_black: every sampled frame is black"


def _near(kind: str, value: Any, check: dict[str, Any]) -> str | None:
    if (
        isinstance(value, int | float)
        and math.isfinite(value)
        and abs(value - check["expect"]) <= check["tolerance"]
    ):
        return None
    return f"{kind}: got {value}, need {check['expect']} ± {check['tolerance']}"


def judge(model: Model, spec: Spec, reel_strip: Path, clip_strip: Path, *, cap_usd: float) -> Judged:
    """Ask the judge about every criterion, visible and held out; decide pass here."""
    sent = list(spec.criteria) + list(spec.held_out)
    prompt = "Criteria:\n" + "\n".join(f"{i + 1}. {c}" for i, c in enumerate(sent))
    images = (("reference reel", reel_strip), ("clip to judge", clip_strip))
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
        for r in judged.results:
            if not r["pass"] and not r["hidden"]:
                lines.append(f"Judge, not met: {r['criterion']} (seen: {r['evidence']})")
        hidden = sum(1 for r in judged.results if not r["pass"] and r["hidden"])
        if hidden:
            lines.append(f"{hidden} more criteria you were not shown also failed.")
    return "\n".join(lines)
