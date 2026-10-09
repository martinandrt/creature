"""A style card per skill: what its screen looks like, measured by fixed code from its own renders.

No model. The skill renders twice in the workshop, on two different texts; fixed code reads both clips:
the background (the median of the frame's border) and how light it is, the three colours that cover most
of the frame, the orientation, the learned length, how much of the frame the user's text takes (the
pixels that differ between the two renders) and how much of the clip moves. A composer that sees only a
name and a sentence glues light and dark screens together; with the card it sees what each screen is.
The card is data in the skill's folder, written through the registry, never by hand.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from creature import forge, registry, workshop

VERSION = 2  # 2: probe texts are sentences (a one-word text killed skills that split their text)
# two renders differ only where the text is; both are sentences, like the texts skills were learned on
TEXTS = ("Stay curious. Keep going.", "A much longer second line of text. It shows where the words land.")
SIZE = (54, 96)  # frames are read this small: enough for colours and areas, cheap to decode
SAMPLES = 12

# Fixed code (ours) that runs in the workshop: reads the two renders and measures the card.
CARD_CODE = r"""
import subprocess
import numpy as np

def frames(path, w, h):
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", f"scale={w}:{h}",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3).astype(np.float32)

def pick(arr, n):
    return arr[np.unique(np.linspace(0, len(arr) - 1, min(n, len(arr))).round().astype(int))]

def hexa(rgb):
    return "#" + "".join(f"{int(round(v)):02x}" for v in rgb)

def run(input, work):
    w, h, n = input["w"], input["h"], input["samples"]
    a_all = frames(f"{work}/in/a.mp4", w, h)
    b_all = frames(f"{work}/in/b.mp4", w, h)
    a, b = pick(a_all, n), pick(b_all, n)
    border = np.concatenate([a[:, 0, :], a[:, -1, :], a[:, :, 0], a[:, :, -1]], axis=1).reshape(-1, 3)
    background = np.median(border, axis=0)
    luma = float(background @ np.array([0.2126, 0.7152, 0.0722]) / 255)
    pixels = a.reshape(-1, 3)
    keys = (pixels[:, 0] // 32) * 64 + (pixels[:, 1] // 32) * 8 + pixels[:, 2] // 32
    counts = np.bincount(keys.astype(int), minlength=512)
    colors = []
    for key in np.argsort(counts)[::-1][:3]:
        if counts[key] == 0:
            break
        share = round(float(counts[key] / len(pixels)), 3)
        colors.append({"hex": hexa(pixels[keys == key].mean(axis=0)), "share": share})
    k = min(len(a), len(b))
    text = (np.abs(a[:k] - b[:k]).mean(axis=3) > 24).any(axis=0)
    steps = np.abs(np.diff(a_all, axis=0)).mean(axis=(1, 2, 3)) if len(a_all) > 1 else np.zeros(1)
    return {
        "background": hexa(background), "luma": round(luma, 3), "colors": colors,
        "text_share": round(float(text.mean()), 3), "moving_share": round(float((steps > 1.0).mean()), 3),
    }
"""


def tone(luma: float) -> str:
    return "dark" if luma < 0.35 else "light" if luma > 0.65 else "mid"


def measure(skill: registry.Skill, folder: Path, *, limits: workshop.Limits, image: str) -> dict[str, Any]:
    """The card of one skill, or {"error": ...} when a render or the measurement failed."""
    clips = {}
    for name, text in zip(("a.mp4", "b.mp4"), TEXTS, strict=True):
        spec = registry.spec_of(skill, text)
        ran = workshop.run(skill.code, forge.skill_input(spec), limits=limits, image=image)
        data = ran.outputs.get(spec.output["file"]) if ran.ok else None
        if not data:
            return {"error": f"render on {text!r} failed: {ran.error or 'no clip'}"[:300]}
        folder.mkdir(parents=True, exist_ok=True)
        clips[name] = folder / name
        clips[name].write_bytes(data)
    output = skill.capability["output"]
    landscape = output["width"] > output["height"]
    w, h = (SIZE[1], SIZE[0]) if landscape else SIZE
    ran = workshop.run(CARD_CODE, {"w": w, "h": h, "samples": SAMPLES}, clips, limits=limits, image=image)
    if not ran.ok or not isinstance(ran.value, dict):
        return {"error": f"measuring failed: {ran.error}"[:300]}
    value = ran.value
    return {
        "card": VERSION,
        "background": value["background"],
        "tone": tone(value["luma"]),
        "luma": value["luma"],
        "colors": value["colors"],
        "orientation": "landscape" if landscape else "portrait",
        "seconds": float(output["duration_s"]),
        "text_share": value["text_share"],
        "moving_share": value["moving_share"],
    }


def line(skill: registry.Skill, card: dict[str, Any]) -> str:
    """One catalog line for the composer: the skill's name, its sentence, and its card as plain data."""
    colors = " ".join(c["hex"] for c in card.get("colors", []))
    return (
        f"- {skill.slug}: {skill.capability.get('effect', '')} | background {card['background']} "
        f"({card['tone']}, luma {card['luma']}) | main colours {colors} | {card['orientation']} | "
        f"{card['seconds']:.1f} s | text takes {card['text_share']:.0%} of the frame | "
        f"moves in {card['moving_share']:.0%} of frames"
    )


def without_error(card: dict[str, Any] | None) -> bool:
    return bool(card) and "error" not in card and card.get("card") == VERSION
