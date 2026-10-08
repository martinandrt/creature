"""The round forge's installed skill, rendered for real: frames are a pure function of their number, so
the parallel range renders join into exactly the whole clip, a frame renders the same twice, and the
layout retimes to another length."""

import dataclasses
import json

import pytest

from creature import authority, criteria, deep, forge, verdict, workshop
from creature.criteria import Spec
from tests.conftest import REPO

pytestmark = pytest.mark.docker

# a box moving 20 px a frame, plus seeded speckle: the same n must give the same pixels
FRAME_PY = """
import random
from PIL import Image, ImageDraw

def frame(n, ctx):
    img = Image.new("RGB", (ctx["width"], ctx["height"]), (20, 24, 40))
    draw = ImageDraw.Draw(img)
    x = int(100 + 20 * n)
    draw.rectangle((x, 800, x + 300, 1100), fill=(240, 240, 240))
    for _ in range(200):
        draw.point((random.randrange(ctx["width"]), random.randrange(ctx["height"])), fill=(255, 0, 0))
    return img
"""
FILES = {"frame.py": FRAME_PY, "layout.json": json.dumps({"frames": 30})}

# fixed code (ours): where the white box starts, on frames of the clip and on given PNGs
EDGES_CODE = r"""
import subprocess
from PIL import Image

def edge(path):
    with Image.open(path) as im:
        row = list(im.convert("L").crop((0, 950, im.width, 951)).getdata())
    return next((x for x, v in enumerate(row) if v > 128), -1)

def run(input, work):
    found = {"clip": {}, "png": {}}
    for k in input.get("frames", []):
        path = f"{work}/d_{k}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-i", f"{work}/in/clip.mp4", "-vf", f"select=eq(n\\,{k})",
                        "-vsync", "vfr", "-frames:v", "1", path], check=True)
        found["clip"][str(k)] = edge(path)
    for name in input.get("pngs", []):
        found["png"][name] = edge(f"{work}/in/{name}")
    return found
"""


@pytest.fixture(scope="module")
def env():
    limits = dataclasses.replace(authority.load(REPO).workshop, timeout_s=120)
    return limits, workshop.ensure_image(REPO / "workshop")


def _spec(seconds):
    out = criteria.clip_format(seconds)
    return Spec(
        "e", "box-slide", "try", "", "t", "Hi", {}, out, ("a",), ("b",), tuple(criteria.checks_for(out))
    )


def _render(env, seconds, **extra):
    limits, image = env
    job = {**forge.skill_input(_spec(seconds)), **extra}
    ran = workshop.run(deep.skill_code(FILES), job, limits=limits, image=image)
    assert ran.ok, f"{ran.error}\n{ran.log[-800:]}"
    return ran


def _edges(env, tmp_path, clip=None, pngs=None):
    limits, image = env
    files = {}
    if clip is not None:
        (tmp_path / "clip.mp4").write_bytes(clip)
        files["clip.mp4"] = tmp_path / "clip.mp4"
    for name, data in (pngs or {}).items():
        (tmp_path / name).write_bytes(data)
        files[name] = tmp_path / name
    frames = [0, 14, 15, 16, 29] if clip is not None else []
    ran = workshop.run(
        EDGES_CODE, {"frames": frames, "pngs": sorted(pngs or {})}, files, limits=limits, image=image
    )
    assert ran.ok, ran.error
    return ran.value


def test_a_frame_renders_the_same_twice_seeded_randomness_included(env):
    once = _render(env, 1.0, frames_only=[5]).outputs
    again = _render(env, 1.0, frames_only=[5]).outputs
    assert set(once) == {"f_0005.png"} and once == again


def test_parallel_range_renders_join_into_exactly_the_whole_clip(env, tmp_path):
    limits, image = env
    spec = _spec(1.0)  # 30 frames; two workers in this authority (cpus 2) meet at frame 15
    full = _render(env, 1.0)
    assert full.value == {"frames": 30, "design_frames": 30}
    checked = verdict.check(full.outputs, spec.checks, limits=limits, image=image, folder=tmp_path)
    assert checked.problems == () and checked.probe["frames"] == 30
    previews = _render(env, 1.0, frames_only=[0, 14, 15, 16, 29]).outputs
    found = _edges(env, tmp_path, clip=full.outputs["clip.mp4"], pngs=previews)
    for k in (0, 14, 15, 16, 29):
        want = 100 + 20 * k
        assert abs(found["clip"][str(k)] - want) <= 2, (
            k,
            found["clip"],
        )  # the joined clip: frame k is frame k
        assert found["png"][f"f_{k:04d}.png"] == want  # the preview of frame k is the same frame


def test_the_layout_retimes_to_another_length(env, tmp_path):
    limits, image = env
    spec = _spec(2.0)  # 60 frames out of a 30-frame design: every output frame shows design time n/2
    full = _render(env, 2.0)
    assert full.value == {"frames": 60, "design_frames": 30}
    checked = verdict.check(full.outputs, spec.checks, limits=limits, image=image, folder=tmp_path)
    assert checked.problems == () and checked.probe["frames"] == 60
    previews = _render(env, 2.0, frames_only=[10, 59]).outputs
    found = _edges(env, tmp_path, pngs=previews)
    assert found["png"]["f_0010.png"] == 100 + 20 * 5  # design frame 5.0
    assert found["png"]["f_0059.png"] == int(100 + 20 * 29.5)  # design frame 29.5, float time
