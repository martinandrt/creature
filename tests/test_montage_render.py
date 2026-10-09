"""The montage rendered for real in the workshop: several source decoders and the encoder must live
within the workshop's process limit (both night montages died there), and a landscape montage keeps
its sources' orientation."""

import dataclasses

import pytest

from creature import authority, montage, verdict, workshop
from tests.conftest import REPO

pytestmark = pytest.mark.docker

COLOURS = ["red", "blue", "green", "yellow"]


def _clip_code(source: str) -> str:
    return (
        "import subprocess\n"
        "def run(input, work):\n"
        f"    source = '{source}'\n"
        "    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', source, '-pix_fmt', 'yuv420p',\n"
        "                    '-c:v', 'libx264', f'{work}/out/clip.mp4'], check=True)\n"
        "    return 'ok'"
    )


@pytest.fixture(scope="module")
def env():
    limits = dataclasses.replace(authority.load(REPO).workshop, timeout_s=120)
    return limits, workshop.ensure_image(REPO / "workshop")


def _sources(env, tmp_path, count, size="1080x1920"):
    limits, image = env
    clips = {}
    for i in range(count):
        sid = chr(97 + i)
        made = workshop.run(
            _clip_code(f"color=c={COLOURS[i]}:s={size}:r=30:d=2"), {}, limits=limits, image=image
        )
        assert made.ok, made.error
        clips[sid] = tmp_path / f"{sid}.mp4"
        clips[sid].write_bytes(made.outputs["clip.mp4"])
    return clips


def _timeline(clips, *, every, landscape=False, layers=(), frames=60):
    sources = {sid: {"skill": f"s-{sid}", "version": 1, "params": {}} for sid in clips}
    return montage.timeline(
        "m", sources, list(clips), every=every, frames=frames, layers=list(layers), landscape=landscape
    )


THUMBS_CODE = r"""
import subprocess
from PIL import Image

def run(input, work):
    found = {}
    for k in input["frames"]:
        path = f"{work}/t_{k}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-i", f"{work}/in/montage.mp4", "-vf", f"select=eq(n\\,{k})",
                        "-vsync", "vfr", "-frames:v", "1", path], check=True)
        with Image.open(path) as im:
            found[str(k)] = list(im.convert("L").resize((24, 40)).getdata())
    return found
"""


BOX_CODE = r"""
import subprocess
from PIL import Image, ImageDraw

def run(input, work):
    writer = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s",
                               "1080x1920", "-r", "30", "-i", "-", "-frames:v", "90", "-c:v", "libx264",
                               "-pix_fmt", "yuv420p", f"{work}/out/clip.mp4"], stdin=subprocess.PIPE)
    for n in range(90):
        img = Image.new("RGB", (1080, 1920), "black")
        x = 100 + round(600 * n / 89)
        ImageDraw.Draw(img).rectangle((x, 760, x + 400, 1160), fill="white")
        writer.stdin.write(img.tobytes())
    writer.stdin.close()
    assert writer.wait() == 0
    return "ok"
"""


def _thumbs(env, tmp_path, montage_bytes, frames):
    """Small grey thumbnails of the given montage frames, measured in the workshop."""
    limits, image = env
    path = tmp_path / "thumbs-montage.mp4"
    path.write_bytes(montage_bytes)
    ran = workshop.run(THUMBS_CODE, {"frames": frames}, {"montage.mp4": path}, limits=limits, image=image)
    assert ran.ok, ran.error
    return {int(k): v for k, v in ran.value.items()}


def _apart(a, b):
    return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / len(a)


def _render(env, t, clips, tmp_path, text="Stay curious."):
    limits, image = env
    ran = montage.render(t, clips, text=text, mark=None, style=None, limits=limits, image=image)
    assert ran.ok, f"{ran.error}\n{ran.log[-600:]}"
    checks = tuple(montage.checks(t, ran.value.get("layers", [])))
    return ran, verdict.check(ran.outputs, checks, limits=limits, image=image, folder=tmp_path)


def test_four_sources_and_the_encoder_render_within_the_process_limit(env, tmp_path):
    # four decoders plus the encoder once took a thread per host core each and hit pids 256
    clips = _sources(env, tmp_path, 4)
    mark = [{"id": "mark", "x": 0.5, "y": 0.5, "height": 0.06}]
    ran, checked = _render(env, _timeline(clips, every=5, layers=mark), clips, tmp_path)
    assert ran.value["frames"] == 60 and ran.value["scenes"] == 12 and len(ran.value["layers"]) == 1
    assert len(ran.value["inks"]) == 12  # an ink chosen at every cut
    assert checked.problems == (), checked.problems
    assert checked.probe["frames"] == 60 and (checked.probe["width"], checked.probe["height"]) == (1080, 1920)


def test_a_landscape_montage_keeps_its_sources_orientation(env, tmp_path):
    clips = _sources(env, tmp_path, 2, size="1920x1080")
    t = _timeline(clips, every=10, landscape=True)
    assert (t["width"], t["height"]) == (1920, 1080)
    ran, checked = _render(env, t, clips, tmp_path)
    assert ran.value["frames"] == 60 and checked.problems == (), checked.problems
    assert (checked.probe["width"], checked.probe["height"]) == (1920, 1080)


def test_a_long_mark_is_drawn_smaller_inside_the_safe_zone(env, tmp_path):
    # DcEZ: a 12-letter mark at height 0.12 came out 1103 px wide in a 1080 frame and crashed the render;
    # a word too wide for the safe zone is drawn smaller, never wider than the zone, and passes its check
    clips = _sources(env, tmp_path, 2)
    mark = [{"id": "mark", "x": 0.5, "y": 0.5, "height": 0.12}]
    t = _timeline(clips, every=10, layers=mark)
    ran, checked = _render(env, t, clips, tmp_path, text="Configurations.")
    [(_x0, _y0, w, h)] = ran.value["layers"]
    zone = 1080 - 2 * round(montage.SAFE["left"] * 1080)
    # with no logo the mark is a monogram (one letter), never the word: at most a quarter of the width
    assert w <= min(zone, round(1080 * montage.MARK_MAX_WIDTH)) and h <= round(0.12 * 1920), (w, h)
    assert checked.problems == (), checked.problems


def test_a_short_source_loops_through_a_longer_montage(env, tmp_path):
    # uceni-ok 01-hvmudvlab: screens learned at 3 s were rendered at the montage's 15 s and their frame()
    # crashed past its designed frames ("N of 4 render workers failed"); now a source renders at its own
    # length and the montage plays it again from the start: a 3 s source fills a 9 s montage, repeating
    limits, image = env
    made = workshop.run(BOX_CODE, {}, limits=limits, image=image)  # a 400 px box sliding 600 px in 3 s
    assert made.ok, made.error
    clips = {"a": tmp_path / "a.mp4"}
    clips["a"].write_bytes(made.outputs["clip.mp4"])
    t = _timeline(clips, every=270, frames=270)  # one scene over the whole 9 s
    ran, checked = _render(env, t, clips, tmp_path)
    assert ran.value["frames"] == 270 and checked.probe["frames"] == 270
    thumbs = _thumbs(env, tmp_path, ran.outputs[montage.MONTAGE], [0, 45, 90, 180])
    assert _apart(thumbs[0], thumbs[90]) < 4 and _apart(thumbs[0], thumbs[180]) < 4  # started again
    assert _apart(thumbs[0], thumbs[45]) > 15  # and it does move in between
    assert checked.problems == (), checked.problems  # the restart inside the scene passes the fixed checks


def test_a_mark_pushed_to_the_edge_stays_inside_the_safe_zone_in_landscape_too(env, tmp_path):
    # the composer may put a wide mark at x 0.1: it is clamped to the zone's left edge; in a 1920 frame
    # that edge is 115.2 px, and 115 (rounded down) is 0.0599 of the width: outside the zone the check wants
    clips = _sources(env, tmp_path, 2, size="1920x1080")
    mark = [{"id": "mark", "x": 0.1, "y": 0.5, "height": 0.12}]
    t = _timeline(clips, every=10, landscape=True, layers=mark)
    ran, checked = _render(env, t, clips, tmp_path, text="Configurations.")
    [(x0, _y0, w, _h)] = ran.value["layers"]
    assert x0 / 1920 >= montage.SAFE["left"] and (x0 + w) / 1920 <= 1 - montage.SAFE["right"], (x0, w)
    assert checked.problems == (), checked.problems
