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


def _timeline(clips, *, every, landscape=False, layers=()):
    sources = {sid: {"skill": f"s-{sid}", "version": 1, "params": {}} for sid in clips}
    return montage.timeline(
        "m", sources, list(clips), every=every, frames=60, layers=list(layers), landscape=landscape
    )


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
    assert w <= zone and h < round(0.12 * 1920), (w, h)  # smaller than asked, to fit
    assert checked.problems == (), checked.problems


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
