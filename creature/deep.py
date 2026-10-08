"""The round forge: build a skill in many small rounds, each one looking at its own output.

Every round is one capped model call without history. It sees the task, the visible criteria, the
skill's current files, a one-line note per earlier round, the last failures and measurements, and
images: frame pairs (reel | its output, same moments) and full-resolution crops it asked for; the first
round also gets a contact sheet of the library items picked for it. It answers with only what changes:
new files or find/replace edits. The spine applies them, renders preview frames in the workshop (a frame
is a pure function of its number, so only those frames render), and when the forge says done, renders
the whole clip, runs the fixed checks and asks the separate judge. The forge never sees held-out criteria
or what the judge said about them. Measuring scripts the forge writes run on the reel in the workshop;
their answers come back as data.

A skill is files: frame.py with frame(n, ctx) and layout.json (timing, positions, colours, texts as
data), plus helpers. It installs as one skill.py: fixed wrapper code (ours) with the files inside, so
everything that runs skills runs it unchanged, and the layout can be retimed without a model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from itertools import zip_longest
from pathlib import Path
from typing import Any

from creature import forge, workshop
from creature.criteria import Spec
from creature.forge import Outcome
from creature.ledger import Ledger
from creature.llm import MAX_IMAGE_BYTES, BudgetRefused, Model, ModelError

PAIRS = 12  # frame pairs per preview sheet
WORSE = 1.10  # a round scoring more than 10 % worse than the best goes back to the best
PLATEAU = 6  # rounds without a 3 % better score or a whole-clip check: the best version is checked, then stop
BETTER = 0.97
MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES = 8, 16_000, 48_000  # a round resends every file
FILE = re.compile(r"[a-z][a-z0-9_]{0,30}\.(py|json)")
MEASURE_CHARS = 3000

SCHEMA = {
    "type": "object",
    "required": ["note", "files", "edits", "done"],
    "properties": {
        "note": {"type": "string"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "content"],
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            },
        },
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "find", "replace"],
                "properties": {
                    "path": {"type": "string"},
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                },
            },
        },
        "measure": {"type": "string"},
        "look": {
            "type": "array",
            "maxItems": 2,
            "items": {
                "type": "object",
                "required": ["time", "box"],
                "properties": {
                    "time": {"type": "number"},
                    "box": {"type": "array", "items": {"type": "number"}},
                },
            },
        },
        "done": {"type": "boolean"},
        "assets": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = f"""You build a skill that renders a motion design clip as close as you can to a reference reel,
in rounds. Each round you see the reel's frames next to your output's frames at the same moments (pairs:
reel left, yours right, the time under each), full-resolution crops you asked for, measurements you
asked for, and what failed. You answer with only what changes.

The skill is a few files:
- frame.py defines frame(n, ctx) returning a PIL RGB image of ctx["width"] x ctx["height"]. It is a pure
  function of n: no state between calls, no randomness that is not seeded from n or from layout data.
  ctx: width, height, fps, frames (n runs over 0..frames-1; n may be fractional when the clip is retimed,
  so use float maths), text (the user's input), params, layout (the parsed layout.json), dir (your files'
  folder; import your other modules normally).
- layout.json holds the data: timing (in frames of the design length "frames"), positions, colours,
  sizes, texts. Drawing goes in the .py files. The spine may retime the layout or change the text.
- At most {MAX_FILES} files, each under 150 lines. Plain names like elements.py.
Answer with: note (one line: what you changed and why), files (new files or full replacements, only
when needed), edits (find/replace on a file: find must appear exactly once), done (true only when you
believe your frames match the reel's). Optional: measure (a script defining run(input, work) where
input["clip"] is the reel's path; return a small JSON object: positions, colours, timings you measured)
and look (up to 2 crops for the next round: time in seconds and box [x0, y0, x1, y1] as shares of the
frame).
When an asset library is mounted at /assets (described in the task), textures, photos, icons and fonts
come from it, and geometry is drawn by the code itself.
Available in the sandbox, nothing else: Python 3.12, Pillow 11.3, numpy 2.5, scipy 1.18, ffmpeg.
Fonts: {forge.FONTS}."""

# Fixed code (ours): the installed skill. FILES is filled in at install; run() writes them out, imports
# frame.py and renders, either preview frames as PNG or the whole clip, frames in parallel.
WRAPPER = r"""
import json, os, sys, random, subprocess
FILES = __FILES__

def _load(work):
    folder = f"{work}/skill"
    os.makedirs(folder, exist_ok=True)
    for name, text in FILES.items():
        with open(f"{folder}/{name}", "w") as file:
            file.write(text)
    sys.path.insert(0, folder)
    import frame as module
    layout = json.loads(FILES["layout.json"]) if "layout.json" in FILES else {}
    return module, layout, folder

def _ctx(input, layout, folder):
    out = input["output"]
    design = int(layout.get("frames") or round(out["duration_s"] * out["fps"]))
    return {"width": out["width"], "height": out["height"], "fps": out["fps"], "frames": design,
            "text": input.get("text", ""), "params": input.get("params", {}), "layout": layout, "dir": folder}

_STATE = {}

def _one(n_out):
    module, ctx, scale = _STATE["module"], _STATE["ctx"], _STATE["scale"]
    n = n_out * scale  # the design timeline, retimed to the asked length
    random.seed(n_out)
    try:
        import numpy
        numpy.random.seed(n_out % (2 ** 32))
    except ImportError:
        pass
    image = module.frame(n, ctx).convert("RGB")
    if image.size != (ctx["width"], ctx["height"]):
        raise ValueError(f"frame {n_out} is {image.size}, need {(ctx['width'], ctx['height'])}")
    return image.tobytes()

def _cpus():
    try:
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()
        return max(1, int(int(quota) / int(period))) if quota != "max" else (os.cpu_count() or 2)
    except (OSError, ValueError):
        return 2

def _segment(a, b, path, ctx, out):
    writer = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s",
                               f"{ctx['width']}x{ctx['height']}", "-r", str(out["fps"]), "-i", "-",
                               "-frames:v", str(b - a), "-c:v", "libx264", "-threads", "2",
                               "-pix_fmt", "yuv420p", path],
                              stdin=subprocess.PIPE)
    for n in range(a, b):
        writer.stdin.write(_one(n))
    writer.stdin.close()
    if writer.wait() != 0:
        raise RuntimeError(f"encoding frames {a}-{b} failed")

def run(input, work):
    module, layout, folder = _load(work)
    ctx = _ctx(input, layout, folder)
    out = input["output"]
    total = round(out["duration_s"] * out["fps"])
    _STATE.update(module=module, ctx=ctx, scale=ctx["frames"] / total)
    if input.get("frames_only") is not None:
        from PIL import Image
        for n in input["frames_only"]:
            Image.frombytes("RGB", (ctx["width"], ctx["height"]), _one(n)).save(f"{work}/out/f_{n:04d}.png")
        return {"frames": len(input["frames_only"])}
    # frames are independent: each worker renders and encodes its own range, the ranges join exactly
    workers = max(1, min(_cpus(), 8, total))
    bounds = [round(total * i / workers) for i in range(workers + 1)]
    children = []
    for i in range(workers):
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                _segment(bounds[i], bounds[i + 1], f"{work}/seg_{i}.mp4", ctx, out)
            except BaseException:
                import traceback
                traceback.print_exc()
                code = 1
            os._exit(code)
        children.append(pid)
    failed = [pid for pid in children if os.waitpid(pid, 0)[1] != 0]
    if failed:
        raise RuntimeError(f"{len(failed)} of {workers} render workers failed (see the log)")
    with open(f"{work}/segments.txt", "w") as file:
        file.writelines(f"file '{work}/seg_{i}.mp4'\n" for i in range(workers))
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", f"{work}/segments.txt",
                    "-c", "copy", f"{work}/out/{out['file']}"], check=True)
    return {"frames": total, "design_frames": ctx["frames"]}
"""

# Fixed code (ours): reel frames at given times next to the output's preview frames, as pair sheets,
# and full-resolution crops of both. Paths come from the spine, never from the model.
PAIRS_CODE = (
    workshop.SHEET_CODE
    + r"""
import json, os

def run(input, work):
    tiles, times = [], []
    for i, (t, n) in enumerate(input["pairs"]):
        reel_tile, out_tile = f"{work}/r_{i:02d}.png", f"{work}/o_{i:02d}.png"
        t = grab(f"{work}/in/reel.mp4", t, reel_tile, fit(input["landscape"]))
        shape = fit(input["landscape"])
        subprocess.run(["ffmpeg", "-v", "error", "-i", f"{work}/in/f_{n:04d}.png", "-vf", shape, out_tile],
                       check=True)
        tiles += [reel_tile, out_tile]
        times += [t, t]
    sheets(tiles, times, f"{work}/out/pairs", input["landscape"])
    # a number, no model: how far the output's frames are from the reel's (small colour copies), plus a
    # penalty for each main colour of the output that is not one of the reel's
    diffs = []
    for i in range(len(input["pairs"])):
        with Image.open(f"{work}/r_{i:02d}.png") as a, Image.open(f"{work}/o_{i:02d}.png") as b:
            size = (96, 54) if input["landscape"] else (54, 96)
            small = [np.asarray(x.convert("RGB").resize(size), dtype=np.float32) for x in (a, b)]
        diffs.append(float(np.abs(small[0] - small[1]).mean()))
    outs = [f"{work}/o_{i:02d}.png" for i in range(len(input["pairs"]))]
    allowed = [np.array([int(c[k:k + 2], 16) for k in (1, 3, 5)]) for c in input["palette"]]
    misses = 0
    if allowed:
        for color in palette(outs, 6, *((480, 270) if input["landscape"] else (270, 480))):
            rgb = np.array([int(color["hex"][k:k + 2], 16) for k in (1, 3, 5)])
            if color["share"] >= 0.10 and min(np.abs(rgb - a).sum() for a in allowed) > 90:
                misses += 1
    score = round(sum(diffs) / max(1, len(diffs)) + 20 * misses, 2)
    for k, crop in enumerate(input["crops"]):
        x0, y0, x1, y1 = crop["box"]
        full = f"{work}/reel_full_{k}.png"
        grab(f"{work}/in/reel.mp4", crop["time"], full)
        parts = []
        for path in (full, f"{work}/in/f_{crop['n']:04d}.png"):
            with Image.open(path) as im:
                w, h = im.size
                left, top = int(x0 * w), int(y0 * h)
                box = (left, top, max(left + 1, int(x1 * w)), max(top + 1, int(y1 * h)))
                parts.append(im.convert("RGB").crop(box))
        height = max(p.height for p in parts)
        side = Image.new("RGB", (sum(p.width for p in parts) + 16, height), (127, 127, 127))
        side.paste(parts[0], (0, 0))
        side.paste(parts[1], (parts[0].width + 16, 0))
        if side.width > 1500:
            side = side.resize((1500, max(1, round(side.height * 1500 / side.width))))
        side.save(f"{work}/out/crop_{k}.png")
    return {"pairs": len(input["pairs"]), "crops": len(input["crops"]), "score": score,
            "frame_diff": round(sum(diffs) / max(1, len(diffs)), 2), "palette_misses": misses}
"""
)


LIBRARY_SHOWN = 30  # items from the asset manifest shown to the forge, picked by material
FONTS_SHOWN = 8  # fonts shown on top of those when the clip draws type
USED = re.compile(r"/assets/[A-Za-z0-9_./-]+")

# What the reel is made of, in the words a criterion or a gap uses  ->  the library tags those words stand
# for (a category name is a tag too). Material only: a texture, a photo, an icon, a figure, type. Shapes,
# arrows, frames, numbers and sounds are not material (the code draws geometry itself), so nothing here
# picks them.
MATERIAL_TAGS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = tuple(
    (re.compile(words), tags)
    for words, tags in (
        (r"textur", ("textury",)),
        (r"\bgrain|\bnois[ey]|speckle", ("zrno", "grain", "sum", "noise")),
        (r"paper|parchment|cardboard", ("papir", "paper")),
        (r"halftone|dither|stipple", ("poloton", "halftone", "dither")),
        (r"scanline|\bcrt\b", ("scanlines", "crt")),
        (r"\bdust|scratch", ("prach", "dust", "scratches")),
        (r"fabric|cloth|denim|leather|linen", ("fabric", "cloth", "denim", "leather")),
        (r"concrete|plaster|asphalt|plywood|\bwood|\bsand\b|\brust|metal",
         ("concrete", "plaster", "asphalt", "wood", "sand", "rusty", "metal")),
        (r"glow|flare|\bhalo|bloom|light leak", ("zare", "glow", "flare", "halo")),
        (r"photo|\bimagery", ("fotky",)),
        (r"\bicons?\b|pictogram|glyph", ("ikony",)),
        (r"silhouette|\bfigures?\b|\bpeople\b|\bpersons?\b|\bhumans?\b|mascot|avatar", ("postavy",)),
        (r"\bfonts?\b|typeface|lettering|typograph", ("pisma",)),
        (r"(?<!sans[- ])serif", ("serif", "patkové")),
        (r"\bsans\b|grotesk|grotesque", ("sans", "grotesk")),
        (r"\bmono(?:space|spaced)?\b|terminal", ("mono", "terminál")),
        (r"condensed|compressed", ("úzký",)),
        (r"extra[- ]?bold|ultra[- ]?bold", ("těžký",)),
    )
)  # fmt: skip
MATERIAL_KINDS = (
    "textury",
    "zare",
    "fotky",
    "ikony",
    "postavy",
    "pisma",
)  # library categories that are material
WORD = re.compile(r"[a-zá-ž0-9]+")


def reel_words(spec: Spec) -> str:
    """What the spec says the reel shows: its task and visible criteria, the gaps (what a script cannot
    draw) and each surface's own task and criteria. Never the effect's name, the user's text, the params
    or the held-out criteria."""
    parts = [spec.task, *spec.criteria]
    for gap in spec.gaps:
        parts += [str(gap.get("what", "")), str(gap.get("needs", ""))]
    for surface in spec.parts:
        parts += [surface.task, *surface.criteria]
    return " ".join(parts).lower()


def pick(items: list[dict[str, Any]], spec: Spec) -> list[dict[str, Any]]:
    """The library items for the material the spec names, fonts last. An item is picked when its tags (its
    category counts) share one with a tag the words stand for; items matching more of them come first, then
    those whose tags share words with the spec; ties keep the manifest's order. Kinds take turns, so a
    hundred icons cannot crowd out a photo. A clip with user text draws type, so it always gets fonts."""
    said = reel_words(spec)
    wanted = [tags for words, tags in MATERIAL_TAGS if words.search(said)]
    if spec.text.strip():
        wanted.append(("pisma",))
    if not wanted:
        return []
    asked = {word for word in WORD.findall(said) if len(word) >= 4}

    scored = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("kategorie") not in MATERIAL_KINDS:
            continue
        tags = {str(tag).lower() for tag in item.get("stitky", [])}
        have = tags | {str(item.get("kategorie", "")).lower()}
        named = set(WORD.findall(f"{item.get('jmeno', '')} {' '.join(sorted(tags))}".lower()))
        if hits := sum(1 for want in wanted if have & set(want)):
            scored.append((-hits, -len(asked & named), index, item))
    ranked = [entry[3] for entry in sorted(scored, key=lambda entry: entry[:3])]
    fonts = [item for item in ranked if item.get("kategorie") == "pisma"][:FONTS_SHOWN]
    kinds: dict[str, list[dict[str, Any]]] = {}
    for item in ranked:
        if item.get("kategorie") != "pisma":
            kinds.setdefault(str(item.get("kategorie")), []).append(item)
    turns = [item for row in zip_longest(*kinds.values()) for item in row if item is not None]
    return turns[:LIBRARY_SHOWN] + fonts


def stock(folder: str | None, spec: Spec) -> tuple[str, list[dict[str, Any]]]:
    """What the forge is told about the asset library (its README, where things are, and the items picked
    for the material the spec names; never the whole manifest) and those items."""
    if not folder:
        return "", []
    root = Path(folder)
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        readme = (root / "README.md").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as error:
        note = "An asset library is mounted read-only at /assets, but its manifest could not be read"
        return f"{note} ({error}).", []
    items = manifest.get("prvky", []) if isinstance(manifest, dict) else []
    chosen = pick(items, spec)
    lines = [
        "Asset library, read-only at /assets (use only what helps; say which files you used):",
        readme[:2500],
        "Items picked for this task (path, category, tags, size):",
    ]
    if not chosen:
        lines.append("(none: the criteria name no texture, photo, icon, figure or font)")
    for item in chosen:
        size = item.get("viewBox") or item.get("rozmer") or item.get("delka_s") or ""
        tags = ", ".join(str(t) for t in item.get("stitky", [])[:8])
        lines.append(f"- /assets/{item.get('soubor')} [{item.get('kategorie')}] {tags} {size}".rstrip())
    return "\n".join(lines), chosen


def library(folder: str | None, spec: Spec) -> str:
    """The text the forge reads about the library (see stock)."""
    return stock(folder, spec)[0]


SHEET_TILES = 24  # library items on the preview sheet
SHEET_SUFFIXES = {".png", ".jpg", ".jpeg", ".svg"}
SHEET_TIMEOUT_S = 60
SHEET_LABEL = "library items you may use (file names under the tiles; full paths are in the task)"

# Fixed code (ours), run in the workshop: the library is mounted there and Pillow is not on the host. Each
# item becomes a tile (an SVG is rasterised by ffmpeg, which has no size option: the root tag gets a bigger
# width and height first), the tiles a labelled grid, saved as PNG, or as JPEG when that is over the cap.
SHEET_CODE = r"""
import io, re, subprocess
from PIL import Image, ImageDraw, ImageFont

TILE, LABEL, GAP, COLUMNS = 200, 18, 8, 6
GREY = (160, 160, 160, 255)  # black strokes and white glows both show on it
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
QUOTED = r'''\s*=\s*(?:"([^"]*)"|'([^']*)')'''

def svg_raster(path, work, n):
    text = open(path, encoding="utf-8", errors="replace").read()
    root = re.search(r"<svg\b[^>]*>", text)
    if root:
        tag = root.group(0)
        box = re.search("viewBox" + QUOTED, tag)
        numbers = re.findall(r"[-+]?[0-9.]+(?:[eE][-+]?[0-9]+)?", box.group(1) or box.group(2) if box else "")
        if len(numbers) == 4 and float(numbers[2]) > 0 and float(numbers[3]) > 0:
            w, h = float(numbers[2]), float(numbers[3])
            k = 2 * TILE / max(w, h)
            tag = re.sub(r"\s(?:width|height)" + QUOTED, "", tag)
            tag = tag.replace("<svg", f'<svg width="{w * k:.0f}" height="{h * k:.0f}"', 1)
        if "xmlns=" not in tag:
            tag = tag.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
        text = text.replace(root.group(0), tag, 1)
    source, target = f"{work}/s{n}.svg", f"{work}/s{n}.png"
    with open(source, "w", encoding="utf-8") as file:
        file.write(text)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", source, "-frames:v", "1", target],
                   check=True, capture_output=True, timeout=30)
    return Image.open(target)

def tile_of(picture):
    picture = picture.convert("RGBA")
    picture.thumbnail((TILE, TILE))
    base = Image.new("RGBA", (TILE, TILE), GREY)
    base.alpha_composite(picture, ((TILE - picture.width) // 2, (TILE - picture.height) // 2))
    return base.convert("RGB")

def encoded(picture, cap):
    for kind, options in (("PNG", {}), ("JPEG", {"quality": 85}), ("JPEG", {"quality": 60})):
        buffer = io.BytesIO()
        picture.save(buffer, kind, **options)
        if buffer.tell() <= cap:
            return kind, buffer.getvalue()
    return None

def run(input, work):
    tiles, labels, skipped = [], [], []
    for n, item in enumerate(input["items"]):
        try:
            path = item["path"]
            picture = svg_raster(path, work, n) if path.lower().endswith(".svg") else Image.open(path)
            tiles.append(tile_of(picture))
            labels.append(item["label"])
        except Exception as error:
            skipped.append([item.get("path"), f"{type(error).__name__}: {error}"[:120]])
    if not tiles:
        return {"tiles": 0, "skipped": skipped}
    columns = min(COLUMNS, len(tiles))
    rows = -(-len(tiles) // columns)
    size = (GAP + columns * (TILE + GAP), GAP + rows * (TILE + LABEL + GAP))
    sheet = Image.new("RGB", size, (127, 127, 127))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype(FONT, 12)
    for j, (tile, label) in enumerate(zip(tiles, labels)):
        x, y = GAP + (j % columns) * (TILE + GAP), GAP + (j // columns) * (TILE + LABEL + GAP)
        sheet.paste(tile, (x, y))
        draw.rectangle((x, y + TILE, x + TILE - 1, y + TILE + LABEL - 1), fill=(0, 0, 0))
        draw.text((x + 4, y + TILE + 2), label[:28], fill=(255, 255, 255), font=font)
    found = encoded(sheet, input["max_bytes"])
    while not found and sheet.width > 200:  # smaller until it fits
        sheet = sheet.resize((sheet.width * 7 // 10, sheet.height * 7 // 10))
        found = encoded(sheet, input["max_bytes"])
    if not found:
        return {"tiles": len(tiles), "skipped": skipped, "error": "does not fit the image cap"}
    kind, data = found
    name = "library.png" if kind == "PNG" else "library.jpg"
    with open(f"{work}/out/{name}", "wb") as file:
        file.write(data)
    return {"tiles": len(tiles), "skipped": skipped, "file": name, "bytes": len(data)}
"""


def library_sheet(
    chosen: list[dict[str, Any]],
    assets: str | None,
    limits: workshop.Limits | None,
    image: str,
    folder: Path,
    ledger: Ledger,
) -> tuple[tuple[str, Path], ...]:
    """Round 1's extra image: the picked items that can be shown (PNG, JPEG, SVG; fonts and sounds cannot)
    as one labelled contact sheet, made in the workshop. Nothing to show or any failure: no sheet."""
    shown = [
        item
        for item in chosen
        if Path(str(item.get("soubor", ""))).suffix.lower() in SHEET_SUFFIXES
        and not {"", ".", ".."} & set(str(item["soubor"]).split("/"))  # a plain relative path only
    ][:SHEET_TILES]
    if not shown or not assets or limits is None:
        return ()
    items = [{"path": f"/assets/{i['soubor']}", "label": Path(i["soubor"]).stem} for i in shown]
    # the sheet reads the folder the items were picked from, and is not worth more than a minute
    shop = replace(limits, assets=assets, timeout_s=min(limits.timeout_s, SHEET_TIMEOUT_S))
    ran = workshop.run(SHEET_CODE, {"items": items, "max_bytes": MAX_IMAGE_BYTES}, limits=shop, image=image)
    value = ran.value if ran.ok and isinstance(ran.value, dict) else {}
    data = ran.outputs.get(str(value.get("file")))
    if not data or len(data) > MAX_IMAGE_BYTES:
        why = ran.error or str(value.get("error") or "no tile could be made")
        ledger.record("library_sheet", ok=False, items=len(items), error=why[:300])
        return ()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / str(value["file"])
    path.write_bytes(data)
    ledger.record("library_sheet", ok=True, items=len(items), tiles=value.get("tiles"), bytes=len(data))
    return ((SHEET_LABEL, path),)


def used_assets(files: dict[str, str], said: Any) -> list[str]:
    """The library files a skill uses: those its code names, plus those the forge said it used."""
    found = {m.rstrip(".,") for text in files.values() for m in USED.findall(text)}
    if isinstance(said, list):
        found |= {str(x) for x in said if isinstance(x, str) and x.startswith("/assets/")}
    # a path that walks with "." or ".." could point anywhere: the record keeps plain paths only
    return sorted(f for f in found if not {".", ".."} & set(f.split("/")[2:]))


@dataclass
class Workspace:
    """The skill's files as they last rendered, and what each round did."""

    files: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def skill_code(files: dict[str, str]) -> str:
    """The installable skill.py: the fixed wrapper with the files inside."""
    return WRAPPER.replace("__FILES__", repr(dict(sorted(files.items()))), 1)


def apply(space: Workspace, reply: dict[str, Any]) -> list[str]:
    """Apply a round's files and edits; what could not be applied comes back as text for the forge."""
    problems = []
    files = dict(space.files)
    for item in reply.get("files", []) if isinstance(reply.get("files"), list) else []:
        path, content = str(item.get("path", "")), item.get("content")
        if not FILE.fullmatch(path) or not isinstance(content, str):
            problems.append(f"file {path!r} refused: plain lowercase names ending in .py or .json only")
            continue
        files[path] = content
    for item in reply.get("edits", []) if isinstance(reply.get("edits"), list) else []:
        path, find, replace = str(item.get("path", "")), item.get("find"), item.get("replace")
        if path not in files or not isinstance(find, str) or not isinstance(replace, str) or not find:
            problems.append(f"edit on {path!r} refused: no such file or an empty find")
            continue
        if files[path].count(find) != 1:
            problems.append(
                f"edit on {path!r} not applied: find appears {files[path].count(find)} times, not once"
            )
            continue
        files[path] = files[path].replace(find, replace, 1)
    if len(files) > MAX_FILES:
        problems.append(f"at most {MAX_FILES} files; the change was not applied")
        return problems
    big = [p for p, c in files.items() if len(c.encode()) > MAX_FILE_BYTES]
    if big or sum(len(c.encode()) for c in files.values()) > MAX_TOTAL_BYTES:
        problems.append(
            f"files too long (each at most {MAX_FILE_BYTES} bytes, all together {MAX_TOTAL_BYTES}): "
            f"{', '.join(big) or 'all together'}; the change was not applied"
        )
        return problems
    if "layout.json" in files:
        try:
            layout = json.loads(files["layout.json"])
        except json.JSONDecodeError as error:
            problems.append(f"layout.json is not valid JSON: {error}; the change was not applied")
            return problems
        design = layout.get("frames") if isinstance(layout, dict) else None
        if design is not None and (
            isinstance(design, bool) or not isinstance(design, int) or not 1 <= design <= 240
        ):
            problems.append(
                'layout.json "frames" must be a whole number from 1 to 240; the change was not applied'
            )
            return problems
    space.files = files
    return problems


def prompt(
    spec: Spec, space: Workspace, feedback: str, measured: str, round_no: int, rounds: int, shelf: str = ""
) -> str:
    lines = [
        f"Round {round_no} of at most {rounds}.",
        f"Effect: {spec.effect}",
        f"Task: {spec.task}",
        f"Input text: {spec.text!r}",
        f"Params: {json.dumps(spec.params)[:600]}",
        f"Output: {spec.output['width']}x{spec.output['height']}, {spec.output['fps']} fps, "
        f"{spec.output['duration_s']} s",
        "Success criteria (judged from frames of your whole clip):",
        *[f"- {c}" for c in spec.criteria],
    ]
    if shelf:
        lines += ["", shelf]
    if space.notes:
        lines += ["", "Your earlier rounds:", *[f"{i + 1}. {n}" for i, n in enumerate(space.notes[-12:])]]
    if space.files:
        lines += ["", "Your files now:"]
        for name, text in sorted(space.files.items()):
            lines += [f"--- {name}", text]
    else:
        lines += ["", "No files yet: write frame.py and layout.json."]
    if measured:
        lines += ["", "Your measurement of the reel returned:", measured]
    if feedback:
        lines += ["", "Last round:", feedback]
    return "\n".join(lines)


@dataclass(frozen=True)
class Result:
    ok: bool
    files: dict[str, str]
    rounds: int
    gap: str
    assets: tuple[str, ...] = ()


def build(
    model: Model,
    spec: Spec,
    ledger: Ledger,
    *,
    reel: Any,
    rounds: int,
    cap_usd: float,
    model_name: str | None,
    limits: workshop.Limits,
    image: str,
    folder: Path,
    finish: Callable[[str, int], Outcome],
    assets: str | None = None,
) -> Result:
    """Up to `rounds` rounds. `finish(code, round)` renders the whole clip, checks and judges it."""
    space = Workspace()
    feedback, measured = "", ""
    shelf, chosen = stock(assets, spec)
    said: list[str] = []
    failed: set[str] = set()  # file sets the whole-clip check already failed
    last_verdict = ""
    # the version the next round starts from (round, files, images), and the lowest score seen
    base: tuple[int, dict[str, str], tuple[tuple[str, Path], ...]] = (0, {}, ())
    lowest = float("inf")
    moved = 0  # the last round that scored 3 % better than before, or ended in a whole-clip check
    looks: list[dict[str, Any]] = []
    # the first round also sees what the library holds for this reel's material
    images: tuple[tuple[str, Path], ...] = (
        ("reel frames", reel.strip),
        *library_sheet(chosen, assets, limits, image, folder, ledger),
    )
    for number in range(1, rounds + 1):
        here = folder / f"round-{number:02d}"
        here.mkdir(parents=True, exist_ok=True)
        try:
            reply = model.ask(
                "forge", SYSTEM, prompt(spec, space, feedback, measured, number, rounds, shelf), SCHEMA,
                cap_usd=cap_usd, images=images, model=model_name,
            )  # fmt: skip
        except BudgetRefused as error:
            return Result(False, space.files, number - 1, f"budget: {error}")
        except ModelError as error:
            ledger.record("round", round=number, ok=False, error=str(error)[:300])
            too_big = "budget" in str(error)
            feedback = (
                "Your last answer went over this round's cap: keep files short, change only what is needed."
                if too_big
                else "Your last answer could not be used (cut off or malformed): answer with less."
            )
            continue
        problems = apply(space, reply)
        if isinstance(reply.get("assets"), list):
            said = reply["assets"]
        note = str(reply.get("note", ""))[:200]
        space.notes.append(note + (" (some changes refused)" if problems else ""))
        (here / "files.json").write_text(json.dumps(space.files, indent=1, ensure_ascii=False))
        feedback = "\n".join(problems)
        if isinstance(reply.get("measure"), str) and reply["measure"].strip():
            measured = _measure(reply["measure"], reel, limits, image)
        looks = (
            [lk for lk in reply.get("look", []) if _valid_look(lk)][:2]
            if isinstance(reply.get("look"), list)
            else []
        )
        if "frame.py" not in space.files:
            feedback += "\nThere is no frame.py yet."
            ledger.record("round", round=number, ok=False, note=note, problems=problems)
            continue
        preview = _preview(space, spec, reel, looks, limits, image, here)
        ledger.record("round", round=number, ok=not preview.error, note=note, problems=problems,
                      files=sorted(space.files), preview=preview.error or "ok",
                      done=bool(reply.get("done")))  # fmt: skip
        if preview.error:
            feedback += f"\nRendering your frames failed: {preview.error}"
            images = (("reel frames", reel.strip),)
            continue
        images = preview.images
        score = f"Score {preview.score:.2f} (lower is better; {preview.detail})"
        if preview.score <= lowest * WORSE:  # within the margin: a fix the judge asked for may cost a little
            mine = (
                "your best so far"
                if preview.score < lowest
                else f"kept, within 10 % of your best {lowest:.2f}"
            )
            if preview.score < lowest * BETTER:
                moved = number
            base, lowest = (number, dict(space.files), preview.images), min(lowest, preview.score)
            feedback += f"\n{score}: {mine}."
        else:  # the next round starts again from the kept version, and sees its frames
            feedback += (
                f"\n{score}: worse than round {base[0]} by over 10 %; back to round {base[0]}'s files."
            )
            space.files, images = dict(base[1]), base[2]
        ledger.record("round_score", round=number, score=preview.score, best=lowest, best_round=base[0])
        stalled = number - moved >= PLATEAU  # more rounds buy nothing: check the best version, then stop
        if stalled:
            space.files = dict(base[1])
            ledger.record("plateau", round=number, best=lowest, best_round=base[0])
        if reply.get("done") is True or number == rounds or stalled:
            moved = number
            judged = json.dumps(space.files, sort_keys=True)
            if judged in failed:  # the same files again: forge and judge disagree, more rounds buy nothing
                ledger.record("stalemate", round=number, why=feedback.strip()[-400:])
                return Result(False, space.files, number, "stalemate: the forge calls these files done, the "
                              "judge already failed them:" + last_verdict)  # fmt: skip
            outcome = finish(skill_code(space.files), number)
            if outcome.ok:
                return Result(True, space.files, number, "", tuple(used_assets(space.files, said)))
            failed.add(judged)
            last_verdict = "\n" + outcome.feedback
            if stalled:
                return Result(False, space.files, number, f"plateau: no better score for {PLATEAU} rounds, "
                              "the best version did not pass:" + last_verdict)  # fmt: skip
            feedback += "\nYour whole clip was checked and did not pass:\n" + outcome.feedback
    return Result(False, space.files, rounds, feedback.strip() or "no round passed")


@dataclass(frozen=True)
class Preview:
    images: tuple[tuple[str, Path], ...]
    error: str | None
    score: float = float("inf")  # lower is better: frame difference to the reel + colour penalties
    detail: str = ""


def _preview(
    space: Workspace, spec: Spec, reel: Any, looks: list[dict[str, Any]], limits: workshop.Limits, image: str,
    here: Path,
) -> Preview:  # fmt: skip
    total = round(spec.output["duration_s"] * spec.output["fps"])
    # each reel moment once: a surface has only its own few frames, and a repeated pair is paid for twice
    times = sorted(set(reel.times)) or [0.0]
    if len(times) > PAIRS:
        times = sorted({times[round(i * (len(times) - 1) / (PAIRS - 1))] for i in range(PAIRS)})
    picks = times
    pairs = [(t, min(total - 1, round(t / max(reel.duration_s, 0.001) * total))) for t in picks]
    crops = [
        {**lk, "n": min(total - 1, round(lk["time"] / max(reel.duration_s, 0.001) * total))} for lk in looks
    ]
    wanted = sorted({n for _, n in pairs} | {c["n"] for c in crops})
    job = {"text": spec.text, "params": spec.params, "output": spec.output, "frames_only": wanted}
    ran = workshop.run(skill_code(space.files), job, limits=limits, image=image)
    if not ran.ok:
        return Preview((), f"{ran.error} {ran.log[-400:]}".strip())
    files = {name: here / name for name in ran.outputs}
    for name, data in ran.outputs.items():
        (here / name).write_bytes(data)
    files["reel.mp4"] = reel.video
    colors = [
        c["hex"] for c in getattr(reel, "palette", ()) if isinstance(c, dict) and c.get("share", 0) >= 0.03
    ]
    job = {
        "pairs": pairs,
        "crops": crops,
        "palette": colors,
        "landscape": spec.output["width"] > spec.output["height"],
    }
    made = workshop.run(PAIRS_CODE, job, files, limits=limits, image=image)
    if not made.ok:
        return Preview((), f"the preview sheet failed: {made.error}")
    shown: list[tuple[str, Path]] = []
    for name in sorted(made.outputs):
        (here / name).write_bytes(made.outputs[name])
        label = (
            "pairs: reel left, yours right"
            if name.startswith("pairs")
            else "crop at full resolution: reel left, yours right"
        )
        shown.append((label, here / name))
    value = made.value or {}
    detail = (
        f"frame difference {value.get('frame_diff')}, colours not in the reel {value.get('palette_misses')}"
    )
    return Preview(tuple(shown), None, float(value.get("score", float("inf"))), detail)


def _valid_look(look: Any) -> bool:
    box = look.get("box") if isinstance(look, dict) else None
    return (
        isinstance(look.get("time"), int | float)
        and isinstance(box, list)
        and len(box) == 4
        and all(isinstance(v, int | float) and 0 <= v <= 1 for v in box)
        and box[0] < box[2]
        and box[1] < box[3]
    )


def _measure(code: str, reel: Any, limits: workshop.Limits, image: str) -> str:
    ran = workshop.run(
        code, {"clip": "/work/in/reel.mp4"}, {"reel.mp4": reel.video}, limits=limits, image=image
    )
    if not ran.ok:
        return f"(the measurement failed: {ran.error})"
    return json.dumps(ran.value, ensure_ascii=False, default=str)[:MEASURE_CHARS]
