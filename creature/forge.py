"""The forge: writes a skill (a script that renders an effect) and retries with what went wrong.

The forge sees the task, the visible criteria, the clip format and, after a failure, its own last code
and what failed. It never sees held-out criteria, the judge's notes on them, or how checks are made.
The attempt cap comes from authority.json and is enforced here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from creature.criteria import Spec
from creature.ledger import Ledger
from creature.llm import BudgetRefused, Model, ModelError

SCHEMA = {
    "type": "object",
    "required": ["code", "approach"],
    "properties": {"code": {"type": "string"}, "approach": {"type": "string"}},
}

FONTS = (
    "/usr/share/fonts/opentype/inter/Inter-<weight>.otf with <weight> one of Thin, ExtraLight, Light, "
    "Regular, Medium, SemiBold, Bold, ExtraBold, Black (also <weight>Italic and InterDisplay-<weight>); "
    "/usr/share/fonts/truetype/barlow/Barlow-<weight>.ttf with <weight> one of Regular, Medium, SemiBold, "
    "Bold, ExtraBold, Black; /usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-<weight>.ttf with "
    "<weight> one of Thin, Light, Regular, Medium, SemiBold, Bold, ExtraBold; "
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf, DejaVuSans-Bold.ttf, DejaVuSerif.ttf, "
    "DejaVuSerif-Bold.ttf, DejaVuSansMono.ttf, DejaVuSansMono-Bold.ttf"
)

SYSTEM = f"""You write one Python script, a skill, that renders a motion design effect to a video clip.

Contract:
- Define run(input, work). input = {{"text": str, "params": dict, "output": {{"file", "width", "height",
  "fps", "duration_s"}}}}.
- Write exactly one clip to work + "/out/" + output["file"]: H.264 MP4, pixel format yuv420p, exactly
  width x height, exactly fps, exactly round(duration_s * fps) frames.
- Return a small JSON-serialisable dict about what you rendered.
- Every param is optional: use the given value if present, else a sensible default, so the skill works
  on any text. When params has "palette" (hex colours, most used first) or "fonts" ({{"title": path,
  "label": path}}), draw with those instead of your own choice: a design may pass the user's own.
- The skill runs later on texts you have not seen: take every word from input["text"] and derive
  anything that depends on the words from them; never hardcode words of one text.

Available, and nothing else: no network, no installs, no files outside work and /tmp.
- Python 3.12 standard library, Pillow 11.3 (PIL), numpy 2.5, scipy 1.18.
- ffmpeg and ffprobe 7.1 on PATH. A reliable pattern: draw each frame with Pillow and write raw RGB bytes
  to ffmpeg started with ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
  "-s", f"{{w}}x{{h}}", "-r", str(fps), "-i", "-", "-frames:v", str(n), "-c:v", "libx264",
  "-pix_fmt", "yuv420p", path], then close stdin and wait.
- Fonts: {FONTS}

Write clean, short code (under 150 lines). Only the code field is run."""

FENCE = re.compile(r"```[a-z]*[ \t]*\n(.*?)\n?```", re.DOTALL)


@dataclass(frozen=True)
class Outcome:
    """What one attempt achieved. `feedback` is what the forge may read next time."""

    ok: bool
    feedback: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Build:
    ok: bool
    code: str | None
    attempts: int
    outcomes: tuple[Outcome, ...]
    gap: str  # when it failed: the last feedback, i.e. what is still missing
    assets: tuple[str, ...] = ()  # library files the skill uses (the round forge only)


def skill_input(spec: Spec) -> dict[str, Any]:
    return {"text": spec.text, "params": spec.params, "output": spec.output}


def prompt(spec: Spec, last_code: str | None, feedback: str | None) -> str:
    lines = [
        f"Effect: {spec.effect}",
        f"Task: {spec.task}",
        f"Input text this time: {spec.text!r}",
        f"Params from the tutorial: {spec.params}",
        f"Output: {spec.output}",
        "Success criteria (judged from frames of your clip):",
        *[f"- {criterion}" for criterion in spec.criteria],
    ]
    if last_code is not None:
        lines += [
            "",
            "Your previous attempt failed.",
            feedback or "(no detail)",
            "",
            "Previous code:",
            last_code,
        ]
    return "\n".join(lines)


def build(
    model: Model,
    spec: Spec,
    ledger: Ledger,
    *,
    attempts: int,
    cap_usd: float,
    try_code: Callable[[str, int], Outcome],
    system: str = SYSTEM,
    write: Callable[[Spec, str | None, str | None], str] | None = None,
) -> Build:
    """Up to `attempts` rounds of write, run and judge; `try_code(code, n)` runs workshop and verdict.
    A tool (a script that returns data) brings its own `system` and prompt `write`r."""
    write = write or prompt
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise ValueError(f"attempts must be an integer >= 1, got {attempts!r}")
    code: str | None = None
    feedback: str | None = None
    outcomes: list[Outcome] = []
    for number in range(1, attempts + 1):
        try:
            reply = model.ask("forge", system, write(spec, code, feedback), SCHEMA, cap_usd=cap_usd)
        except BudgetRefused as error:
            # keep what the earlier attempts showed: the report says why the build stopped
            return Build(False, code, number - 1, tuple(outcomes), f"budget: {error}")
        except ModelError as error:  # paid, but no usable answer: a failed attempt, not a crash
            outcome = Outcome(False, feedback or "", {"stage": "forge", "error": str(error)[:300]})
            outcomes.append(outcome)
            ledger.record("attempt_result", attempt=number, ok=False, detail=outcome.detail)
            continue
        code = clean(reply["code"])
        ledger.record(
            "forge_attempt",
            attempt=number,
            of=attempts,
            chars=len(code),
            approach=str(reply["approach"])[:300],
        )
        if not code:
            outcome = Outcome(False, "The reply had no code: return the whole script in the code field.")
        else:
            outcome = try_code(code, number)
        outcomes.append(outcome)
        # detail is nested: costs are written only by whoever spends, and its keys cannot clash
        ledger.record("attempt_result", attempt=number, ok=outcome.ok, detail=outcome.detail)
        if outcome.ok:
            return Build(True, code, number, tuple(outcomes), "")
        feedback = outcome.feedback
    return Build(False, code, attempts, tuple(outcomes), feedback or "")


def clean(code: Any) -> str:
    """The script itself: the first fenced block if there is one (prose around it dropped), else the text."""
    if not isinstance(code, str):
        return ""
    match = FENCE.search(code)
    return (match.group(1) if match else code).strip()
