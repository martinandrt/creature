"""The round forge: build a skill in many small rounds, each one looking at its own output.

Every round is one capped model call without history. It sees the task, the visible criteria, the
skill's current files, a one-line note per earlier round, the last failures and measurements, and
images: frame pairs (reel | its output, same moments) and full-resolution crops it asked for. It answers
with only what changes: new files or find/replace edits. The spine applies them, renders preview frames
in the workshop (a frame is a pure function of its number, so only those frames render), and when the
forge says done, renders the whole clip, runs the fixed checks and asks the separate judge. The forge
never sees held-out criteria or what the judge said about them. Measuring scripts the forge writes run
on the reel in the workshop; their answers come back as data.

A skill is files: frame.py with frame(n, ctx) and layout.json (timing, positions, colours, texts as
data), plus helpers. It installs as one skill.py: fixed wrapper code (ours) with the files inside, so
everything that runs skills runs it unchanged, and the layout can be retimed without a model.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from creature import forge, workshop
from creature.criteria import Spec
from creature.forge import Outcome
from creature.ledger import Ledger
from creature.llm import BudgetRefused, Model, ModelError

PAIRS = 12  # frame pairs per preview sheet
WORSE = 1.10  # a round scoring more than 10 % worse than the best goes back to the best
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


LIBRARY_SHOWN = 30  # items from the asset manifest shown to the forge, picked by tags
USED = re.compile(r"/assets/[A-Za-z0-9_./-]+")


def library(folder: str | None, spec: Spec) -> str:
    """What the forge is told about the asset library: its README (where things are) and the items
    whose tags share most words with the task; fonts always. Never the whole manifest."""
    if not folder:
        return ""
    root = Path(folder)
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        readme = (root / "README.md").read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError) as error:
        return (
            f"An asset library is mounted read-only at /assets, but its manifest could not be read ({error})."
        )
    items = manifest.get("prvky", []) if isinstance(manifest, dict) else []
    words = set(re.findall(r"[a-zá-ž0-9]+", " ".join([spec.effect, spec.task, *spec.criteria]).lower()))

    def score(item: dict[str, Any]) -> int:
        tags = " ".join(str(t) for t in item.get("stitky", []))
        have = set(
            re.findall(r"[a-zá-ž0-9]+", f"{item.get('jmeno', '')} {item.get('kategorie', '')} {tags}".lower())
        )
        return len(words & have)

    fonts = [i for i in items if i.get("kategorie") == "pisma"][:8]
    ranked = sorted((i for i in items if i.get("kategorie") != "pisma"), key=lambda i: -score(i))
    chosen = [i for i in ranked if score(i) > 0][:LIBRARY_SHOWN] + fonts
    lines = [
        "Asset library, read-only at /assets (use only what helps; say which files you used):",
        readme[:2500],
        "Items picked for this task (path, category, tags, size):",
    ]
    for item in chosen:
        size = item.get("viewBox") or item.get("rozmer") or item.get("delka_s") or ""
        tags = ", ".join(str(t) for t in item.get("stitky", [])[:8])
        lines.append(f"- /assets/{item.get('soubor')} [{item.get('kategorie')}] {tags} {size}".rstrip())
    return "\n".join(lines)


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
    shelf = library(assets, spec)
    said: list[str] = []
    # the version the next round starts from (round, files, images), and the lowest score seen
    base: tuple[int, dict[str, str], tuple[tuple[str, Path], ...]] = (0, {}, ())
    lowest = float("inf")
    looks: list[dict[str, Any]] = []
    images: tuple[tuple[str, Path], ...] = (("reel frames", reel.strip),)
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
            base, lowest = (number, dict(space.files), preview.images), min(lowest, preview.score)
            feedback += f"\n{score}: {mine}."
        else:  # the next round starts again from the kept version, and sees its frames
            feedback += (
                f"\n{score}: worse than round {base[0]} by over 10 %; back to round {base[0]}'s files."
            )
            space.files, images = dict(base[1]), base[2]
        ledger.record("round_score", round=number, score=preview.score, best=lowest, best_round=base[0])
        if reply.get("done") is True or number == rounds:
            outcome = finish(skill_code(space.files), number)
            if outcome.ok:
                return Result(True, space.files, number, "", tuple(used_assets(space.files, said)))
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
    picks = (
        [reel.times[round(i * (len(reel.times) - 1) / (PAIRS - 1))] for i in range(PAIRS)]
        if reel.times
        else [0.0]
    )
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
