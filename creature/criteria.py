"""Criteria: before any attempt, a reel becomes the effect, the task on my input, the clip format and
what will decide success.

The model sees the caption, the transcript and the frame strip. Host code then picks which criteria
are held out from the forge (only the judge sees them) and derives the file checks from the format,
so the forge never writes the tests it is judged by.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

from creature.llm import Model
from creature.perceive import Reel

# House format: every effect returns a clip in exactly this shape, so clips can be chained.
WIDTH, HEIGHT, FPS = 1080, 1920, 30
MIN_S, MAX_S = 1.0, 8.0
CLIP = "clip.mp4"
DURATION_TOLERANCE_S = 0.1
MAX_CLIP_BYTES = 50_000_000
VERDICTS = ("try", "ask", "refuse", "skip")

SCHEMA = {
    "type": "object",
    "required": [
        "effect",
        "slug",
        "verdict",
        "reason",
        "transcript_is_speech",
        "task",
        "duration_s",
        "params",
        "criteria",
    ],
    "properties": {
        "effect": {"type": "string"},
        "slug": {"type": "string", "pattern": "^[a-z][a-z0-9-]{2,40}$"},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "reason": {"type": "string"},
        "transcript_is_speech": {"type": "boolean"},
        "task": {"type": "string"},
        "duration_s": {"type": "number"},
        "params": {"type": "object"},
        "criteria": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 6},
    },
}

SYSTEM = """You study a short tutorial reel about a motion design effect and write down, before anyone
tries it, what success looks like.

The domain is text and graphic animation that a Python script can render with Pillow, numpy and ffmpeg.
- verdict "try": a 2D text or graphic animation such a script can reproduce.
- verdict "ask": it needs more than that: 3D, particles, camera footage, generated video, a package or
  model downloaded at run time, or a paid plugin's look that cannot be rebuilt from scratch.
- verdict "refuse": it would need sending messages, payments, credentials or publishing for someone.
- verdict "skip": the reel shows no effect that can be reproduced.

The frames are a strip of 12 in time order, 6 per row. The transcript may be song lyrics: then set
transcript_is_speech to false and ignore it.

Criteria describe the TECHNIQUE as seen in frames: what moves, in which order, how things appear or
disappear, what stays. Never exact words, fonts or colours. Each criterion must be checkable from 6
frames of the output. Write 4 to 6.
task: one sentence applying the effect to the user's text. duration_s: a good length for the effect on
that text, 1 to 8 seconds. params: values the tutorial names (timing, blinks, sizes), units in the key
names. slug: a short lowercase name for the effect, words joined by hyphens."""


@dataclass(frozen=True)
class Spec:
    effect: str
    slug: str
    verdict: str  # try · ask · refuse · skip
    reason: str
    task: str
    text: str  # the user's input
    params: dict[str, Any]
    output: dict[str, Any]  # {file, width, height, fps, duration_s}
    criteria: tuple[str, ...]  # the forge sees these
    held_out: tuple[str, ...]  # only the judge sees these
    checks: tuple[dict[str, Any], ...] = field(default=())


def write(model: Model, reel: Reel, text: str, *, cap_usd: float, seed: str) -> Spec:
    """The spec for trying `reel` on `text`. `seed` (the run id) makes the held-out choice repeatable."""
    if not text.strip():
        raise ValueError("the user's input text is empty")
    prompt = (
        f"Caption: {reel.caption or '(none)'}\n"
        f"Transcript: {reel.transcript or '(none)'}\n"
        f"Reel: {reel.duration_s:.1f} s, frames taken at {', '.join(f'{t:.1f}' for t in reel.times)} s\n"
        f"User's text: {text}"
    )
    data = model.ask(
        "criteria", SYSTEM, prompt, SCHEMA, cap_usd=cap_usd, images=(("reel frames", reel.strip),)
    )
    criteria = [c.strip() for c in data["criteria"] if isinstance(c, str) and c.strip()]
    if len(criteria) < 3:
        raise ValueError("the criteria step gave fewer than 3 usable criteria")
    visible, held_out = hold_out(criteria, seed)
    output = clip_format(data["duration_s"])
    return Spec(
        effect=str(data["effect"]).strip(),
        slug=str(data["slug"]),
        verdict=data["verdict"] if data["verdict"] in VERDICTS else "skip",
        reason=str(data["reason"]),
        task=str(data["task"]).strip(),
        text=text,
        params=data["params"] if isinstance(data["params"], dict) else {},
        output=output,
        criteria=tuple(visible),
        held_out=tuple(held_out),
        checks=tuple(checks_for(output)),
    )


def clip_format(duration_s: Any) -> dict[str, Any]:
    """The house format with a duration clamped to the allowed range and snapped to whole frames."""
    try:
        seconds = float(duration_s)
    except (TypeError, ValueError):
        seconds = 3.0
    if not math.isfinite(seconds):
        seconds = 3.0
    frames = round(min(MAX_S, max(MIN_S, seconds)) * FPS)
    return {"file": CLIP, "width": WIDTH, "height": HEIGHT, "fps": FPS, "duration_s": frames / FPS}


def hold_out(criteria: list[str], seed: str) -> tuple[list[str], list[str]]:
    """Split criteria: about a third go to the judge only. Chosen by hash, so the model cannot steer it."""
    count = max(1, len(criteria) // 3)
    ranked = sorted(criteria, key=lambda c: hashlib.sha256(f"{seed}\0{c}".encode()).hexdigest())
    hidden = set(ranked[:count])
    return [c for c in criteria if c not in hidden], [c for c in criteria if c in hidden]


def checks_for(output: dict[str, Any]) -> list[dict[str, Any]]:
    """File checks as data, derived from the declared format; evaluated later by fixed code."""
    name = output["file"]
    return [
        {"kind": "exists", "file": name},
        {"kind": "video_stream", "file": name},
        {"kind": "resolution", "file": name, "width": output["width"], "height": output["height"]},
        {"kind": "fps", "file": name, "expect": output["fps"], "tolerance": 0.01},
        {"kind": "duration", "file": name, "expect": output["duration_s"], "tolerance": DURATION_TOLERANCE_S},
        {"kind": "not_black", "file": name},
        {"kind": "max_bytes", "file": name, "expect": MAX_CLIP_BYTES},
    ]
