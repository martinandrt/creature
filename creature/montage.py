"""Montage: the spine's glue for a design over several learned surfaces.

Two fixed operations, no model: sequence (a cut every N frames, cycling through the sources, each
source keeping its own time) and layer (one element fixed on top of everything, inked dark or light by
the brightness behind it, scene by scene). What to put together is the creature's choice; this module
only expands that choice into a timeline, the single source of time, and renders it in the workshop.
"""

from __future__ import annotations

import collections
import itertools
import math
from typing import Any

from creature import criteria, workshop

MONTAGE = "montage.mp4"
MIN_EVERY, MAX_SOURCES, MAX_LAYERS = 2, 6, 2
MAX_S = 30.0  # a montage covers its reel, up to this long; one effect clip stays within criteria.MAX_S
SAFE = {"left": 0.06, "right": 0.06, "top": 0.10, "bottom": 0.20}  # share of the frame kept clear
MAX_STEP_RATIO = 1.5  # inside a scene, one frame's change at most 1.5x the one before or after
MARK_HEIGHT = (0.03, 0.25)  # a layer's height as a share of the frame's
FONT_FILES = {  # user style names → files in the workshop image
    "Barlow": "/usr/share/fonts/truetype/barlow/Barlow-Bold.ttf",
    "Barlow Black": "/usr/share/fonts/truetype/barlow/Barlow-Black.ttf",
    "JetBrains Mono": "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-Regular.ttf",
    "Inter": "/usr/share/fonts/opentype/inter/Inter-Bold.otf",
    "DejaVu Sans": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
}


def timeline(
    name: str,
    sources: dict[str, dict[str, Any]],
    order: list[str],
    *,
    every: int,
    frames: int,
    layers: list[dict[str, Any]],
    landscape: bool = False,
) -> dict[str, Any]:
    """Expand "a cut every `every` frames through `order`" into scenes and cues. Each scene shows its
    source at the same moment of the source's own time, so every surface keeps moving under the cuts."""
    frames = max(criteria.FPS, min(round(MAX_S * criteria.FPS), int(frames)))
    every = max(MIN_EVERY, int(every))
    scenes, cues = [], []
    for number, start in enumerate(range(0, frames, every) if order else ()):  # no source: no scenes
        scene = {
            "id": f"s{number + 1:02d}",
            "source": order[number % len(order)],
            "start": start,
            "dur": min(every, frames - start),
        }
        scenes.append(scene)
        cues.append({"frame": start, "kind": "cut", "scene": scene["id"]})
    return {
        "name": name,
        "fps": criteria.FPS,
        "width": criteria.HEIGHT if landscape else criteria.WIDTH,  # the sources' orientation
        "height": criteria.WIDTH if landscape else criteria.HEIGHT,
        "frames": frames,
        "sources": sources,
        "scenes": scenes,
        "layers": layers,
        "cues": cues,
    }


def follow(
    name: str,
    served: list[tuple[tuple[float, ...], str, int]],
    duration_s: float,
    cuts: tuple[float, ...],
    *,
    layers: list[dict[str, Any]],
    landscape: bool = False,
) -> dict[str, Any] | None:
    """The reel's own shots as the montage: each shot shows the learned screen the reel shows at that
    time. `served` is (reel times of a screen, skill, version) per learned screen. A shot showing no
    learned screen gets the one nearest in time that differs from the shot before, so the cut stays
    a cut. Without cuts, a shot ends halfway between two times that show different screens."""
    sources: dict[str, dict[str, Any]] = {}
    ids: dict[str, str] = {}
    times: list[tuple[float, str]] = []
    for part_times, slug, version in served:
        if slug not in ids:
            ids[slug] = chr(97 + len(sources))
            sources[ids[slug]] = {"skill": slug, "version": version, "params": {}}
        times += [(float(t), ids[slug]) for t in part_times]
    if not times or len(sources) > MAX_SOURCES:
        return None
    frames = frames_for(duration_s)
    end = frames / criteria.FPS
    if cuts:
        bounds = [0.0, *sorted(c for c in cuts if 0 < c < end), end]
    else:
        ordered = sorted(times)
        changes = [(a + b) / 2 for (a, sa), (b, sb) in itertools.pairwise(ordered) if sa != sb]
        bounds = [0.0, *changes, end]
    scenes: list[dict[str, Any]] = []
    cues: list[dict[str, Any]] = []
    previous, at = None, 0
    for a, b in itertools.pairwise(bounds):
        stop = frames if b >= end else round(b * criteria.FPS)
        if stop <= at:
            continue
        inside = collections.Counter(s for t, s in times if a <= t < b)
        if inside:
            source = inside.most_common(1)[0][0]
        else:
            ranked = sorted(times, key=lambda ts: abs(ts[0] - (a + b) / 2))
            source = next((s for _, s in ranked if s != previous), ranked[0][1])
        scene = {"id": f"s{len(scenes) + 1:02d}", "source": source, "start": at, "dur": stop - at}
        scenes.append(scene)
        cues.append({"frame": at, "kind": "cut", "scene": scene["id"]})
        previous, at = source, stop
    return {
        "name": name,
        "fps": criteria.FPS,
        "width": criteria.HEIGHT if landscape else criteria.WIDTH,
        "height": criteria.WIDTH if landscape else criteria.HEIGHT,
        "frames": frames,
        "sources": sources,
        "scenes": scenes,
        "layers": layers,
        "cues": cues,
    }


def problems(t: dict[str, Any]) -> list[str]:
    """What is wrong with a timeline, before anything renders. Empty when it can be rendered."""
    found = []
    sources, scenes = t.get("sources") or {}, t.get("scenes") or []
    frames = t.get("frames")
    if not isinstance(frames, int) or not criteria.FPS <= frames <= round(MAX_S * criteria.FPS):
        found.append(f"frames must be a whole number from {criteria.FPS} to {round(MAX_S * criteria.FPS)}")
    if not scenes:
        found.append("a montage needs at least one scene")
    if not 1 <= len(sources) <= MAX_SOURCES:
        found.append(f"a montage needs 1 to {MAX_SOURCES} sources")
    at = 0
    for scene in scenes:
        if scene.get("source") not in sources:
            found.append(f"scene {scene.get('id')} uses an unknown source")
        if scene.get("start") != at or not isinstance(scene.get("dur"), int) or scene["dur"] < 1:
            found.append(f"scene {scene.get('id')} does not follow the one before it")
            break
        at += scene["dur"]
    if at != t.get("frames"):
        found.append(f"the scenes cover {at} frames, the timeline says {t.get('frames')}")
    if len(t.get("layers") or []) > MAX_LAYERS:
        found.append(f"at most {MAX_LAYERS} layers")
    for layer in t.get("layers") or []:
        if not MARK_HEIGHT[0] <= _number(layer.get("height"), 0.0) <= MARK_HEIGHT[1]:
            found.append(f"layer {layer.get('id')}: height must be {MARK_HEIGHT[0]} to {MARK_HEIGHT[1]}")
    return found


def style_params(style: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """A user's style file as params every source gets: palette and font files. Unknown fonts are
    named in the notes and left out. Data, not instructions: only colours and known font names."""
    if not style:
        return {}, []
    params: dict[str, Any] = {}
    notes = []
    colors = style.get("colors") or {}
    palette = [c for c in (colors.values() if isinstance(colors, dict) else colors) if _is_hex(c)]
    if palette:
        params["palette"] = palette
    fonts = {}
    for role, family in (style.get("fonts") or {}).items():
        if family in FONT_FILES:
            fonts[str(role)] = FONT_FILES[family]
        else:
            notes.append(f"font {family!r} is not in the workshop; skills keep their own")
    if fonts:
        params["fonts"] = fonts
    return params, notes


def inks(style: dict[str, Any] | None) -> dict[str, str]:
    """Dark and light ink for a layer: the darkest and lightest style colours, else near black/white."""
    palette = list(style_params(style)[0].get("palette", []))
    if len(palette) < 2:
        return {"dark": "#111111", "light": "#f4f4f4"}
    ranked = sorted(palette, key=_luma)
    return {"dark": ranked[0], "light": ranked[-1]}


def checks(t: dict[str, Any], boxes: list[list[int]]) -> list[dict[str, Any]]:
    """Fixed checks for the rendered montage: the house file checks for its total length, smooth
    motion inside scenes (a cut is a jump on purpose) and every layer inside the safe zone. `boxes` are
    the layers' [x, y, w, h] in pixels, as the render placed them."""
    output = {
        "file": MONTAGE,
        "width": t["width"],
        "height": t["height"],
        "fps": t["fps"],
        "duration_s": t["frames"] / t["fps"],
    }
    found = criteria.checks_for(output)
    cuts = [s["start"] for s in t["scenes"]]
    found.append({"kind": "smooth", "file": MONTAGE, "cuts": cuts, "max_ratio": MAX_STEP_RATIO})
    if boxes:
        shares = [
            [x / t["width"], y / t["height"], (x + w) / t["width"], (y + h) / t["height"]]
            for x, y, w, h in boxes
        ]
        found.append({"kind": "safe_zone", "file": MONTAGE, "boxes": shares, "safe": SAFE})
    return found


def render(
    t: dict[str, Any],
    clips: dict[str, Any],
    *,
    text: str,
    mark: Any | None,
    style: dict[str, Any] | None,
    limits: workshop.Limits,
    image: str,
) -> workshop.WorkshopResult:
    """Render the timeline in the workshop from one clip per source (each at least `frames` long)."""
    files = {f"{sid}.mp4": path for sid, path in clips.items()}
    if mark is not None:
        files["mark.png"] = mark
    job = {
        "frames": t["frames"],
        "width": t["width"],
        "height": t["height"],
        "fps": t["fps"],
        "scenes": t["scenes"],
        "sources": sorted(clips),
        "layers": t["layers"],
        "mark_text": (text.split() or ["?"])[0].strip(".,;:!?").upper()[:12],
        "mark_font": FONT_FILES["Barlow Black"],
        "inks": inks(style),
        "safe": SAFE,
        "out": MONTAGE,
    }
    return workshop.run(MONTAGE_CODE, job, files, limits=limits, image=image)


def _number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _is_hex(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 7 and value[0] == "#" and _hexdigits(value[1:])


def _hexdigits(text: str) -> bool:
    return all(c in "0123456789abcdefABCDEF" for c in text)


def _luma(color: str) -> float:
    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.299 * r + 0.587 * g + 0.114 * b


def frames_for(seconds: float) -> int:
    """Whole frames for a montage length, inside the house range."""
    if not math.isfinite(seconds):
        seconds = criteria.MIN_S
    return round(min(MAX_S, max(criteria.MIN_S, seconds)) * criteria.FPS)


# Fixed code (ours) that runs in the workshop: reads every source in step, frame by frame, takes the
# frame of the scene's source, puts the layer on top with the ink that contrasts with what is behind it.
MONTAGE_CODE = r"""
import subprocess
import numpy as np
from PIL import Image, ImageDraw, ImageFont

def hex_rgb(color):
    return np.array([int(color[i:i + 2], 16) for i in (1, 3, 5)], dtype=np.float32)

def mark_alpha(job, work, height_px):
    try:
        img = Image.open(f"{work}/in/mark.png")
        img.load()
    except OSError:
        img = None
    if img is not None:
        if "A" in img.getbands() and img.getchannel("A").getextrema()[0] < 250:
            alpha = img.getchannel("A")
        else:  # no transparency: dark drawing on a light ground
            alpha = Image.eval(img.convert("L"), lambda v: 255 - v)
        box = alpha.getbbox() or (0, 0, alpha.width, alpha.height)
        alpha = alpha.crop(box)
    else:
        font = ImageFont.truetype(job["mark_font"], 400)
        left, top, right, bottom = font.getbbox(job["mark_text"])
        alpha = Image.new("L", (right - left + 20, bottom - top + 20), 0)
        ImageDraw.Draw(alpha).text((10 - left, 10 - top), job["mark_text"], font=font, fill=255)
    width = max(1, round(alpha.width * height_px / alpha.height))
    return np.asarray(alpha.resize((width, height_px), Image.LANCZOS), dtype=np.float32) / 255.0

def run(input, work):
    W, H, total = input["width"], input["height"], input["frames"]
    size = W * H * 3
    readers = {}
    for sid in input["sources"]:
        # few threads each: one ffmpeg per source plus the encoder would otherwise take a thread per host
        # core apiece and run into the workshop's process limit
        source = f"{work}/in/{sid}.mp4"
        readers[sid] = subprocess.Popen(["ffmpeg", "-v", "error", "-threads", "2", "-i", source,
                                         "-filter_threads", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                        stdout=subprocess.PIPE)
    layers = []
    for layer in input["layers"]:
        safe = input.get("safe") or {"left": 0, "right": 0, "top": 0, "bottom": 0}
        left, right = round(safe["left"] * W), W - round(safe["right"] * W)
        top, bottom = round(safe["top"] * H), H - round(safe["bottom"] * H)
        alpha = mark_alpha(input, work, max(8, round(layer["height"] * H)))
        if alpha.shape[1] > right - left:  # a long word: smaller, never wider than the safe zone
            ratio = (right - left) / alpha.shape[1]
            alpha = mark_alpha(input, work, max(8, int(alpha.shape[0] * ratio)))
            alpha = alpha[:, : right - left]
        h, w = alpha.shape
        x0 = int(round(layer.get("x", 0.5) * W - w / 2))
        y0 = int(round(layer.get("y", 0.5) * H - h / 2))
        x0, y0 = max(left, min(right - w, x0)), max(top, min(max(top, bottom - h), y0))
        layers.append((alpha[..., None], x0, y0, w, h))
    dark, light = hex_rgb(input["inks"]["dark"]), hex_rgb(input["inks"]["light"])
    path = f"{work}/out/{input['out']}"
    writer = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s",
                               f"{W}x{H}", "-r", str(input["fps"]), "-i", "-", "-frames:v", str(total),
                               "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", path],
                              stdin=subprocess.PIPE)
    scene_of = []
    for scene in input["scenes"]:
        scene_of += [scene] * scene["dur"]
    chosen, ink = [], {}
    for t in range(total):
        frames = {}
        for sid, reader in readers.items():
            data = reader.stdout.read(size)
            if len(data) < size:
                raise RuntimeError(f"source {sid} ended at frame {t}, the timeline needs {total}")
            frames[sid] = data
        scene = scene_of[t]
        frame = np.frombuffer(frames[scene["source"]], dtype=np.uint8).reshape(H, W, 3).astype(np.float32)
        for index, (alpha, x0, y0, w, h) in enumerate(layers):
            region = frame[y0:y0 + h, x0:x0 + w]
            if t == scene["start"]:  # the ink is chosen once per scene, by the brightness behind the layer
                luma = float((region @ np.array([0.299, 0.587, 0.114], dtype=np.float32)).mean())
                ink[index] = "dark" if luma > 140 else "light"
                if index == 0:
                    chosen.append(ink[index])
            color = dark if ink[index] == "dark" else light
            frame[y0:y0 + h, x0:x0 + w] = region * (1 - alpha) + color * alpha
        writer.stdin.write(frame.clip(0, 255).astype(np.uint8).tobytes())
    writer.stdin.close()
    if writer.wait() != 0:
        raise RuntimeError("encoding the montage failed")
    for reader in readers.values():
        reader.stdout.close()
        reader.wait()
    return {"frames": total, "scenes": len(input["scenes"]), "inks": chosen,
            "layers": [[x0, y0, w, h] for _, x0, y0, w, h in layers]}
"""
