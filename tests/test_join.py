"""Joining the clips of a multi-step design, for real: ffmpeg concat without re-encoding inside the
workshop, then the same fixed file checks on the result. The promise under test is exact: the joined
clip holds the steps' frames together, no more and no fewer."""

import dataclasses

import pytest

from creature import authority, criteria, loop, verdict, workshop
from tests.conftest import REPO
from tests.fakes import FakeModel

pytestmark = pytest.mark.docker

HOUSE = "s=1080x1920:r=30"


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
    limits = dataclasses.replace(authority.load(REPO).workshop, timeout_s=90)
    return limits, workshop.ensure_image(REPO / "workshop")


def _render(env, tmp_path, name, source):
    limits, image = env
    made = workshop.run(_clip_code(source), {}, limits=limits, image=image)
    assert made.ok, made.error
    path = tmp_path / name
    path.write_bytes(made.outputs["clip.mp4"])
    return path


def _probe(env, tmp_path, outputs, frames):
    limits, image = env
    output = {
        "file": loop.JOINED, "width": criteria.WIDTH, "height": criteria.HEIGHT, "fps": criteria.FPS,
        "duration_s": frames / criteria.FPS,
    }  # fmt: skip
    return verdict.check(
        outputs, tuple(criteria.checks_for(output)), limits=limits, image=image, folder=tmp_path
    )


def test_two_clips_join_to_exactly_the_sum_of_their_frames(env, home, tmp_path):
    first = _render(env, tmp_path, "a.mp4", f"testsrc2={HOUSE}:d=3")  # 90 frames
    second = _render(env, tmp_path, "b.mp4", f"color=c=red:{HOUSE}:d=2")  # 60 frames
    (home / "authority.json").write_bytes((REPO / "authority.json").read_bytes())
    creature = loop.Creature(home, transport=FakeModel())
    joined, problems = creature.join([first, second], 150)
    assert problems == [] and joined == creature.folder / loop.JOINED and joined.stat().st_size > 0
    checked = _probe(env, tmp_path, {loop.JOINED: joined.read_bytes()}, 150)
    assert checked.problems == () and checked.probe["frames"] == 150
    assert checked.probe["duration_s"] == pytest.approx(5.0, abs=1e-3)
    assert len(checked.more) == 1  # 30 frames at 6 per second: a second sheet for the judge


def test_the_frames_check_catches_a_part_that_went_missing(env, home, tmp_path):
    # the declared total is what the design promised; a join that lost a part must not pass as it
    first = _render(env, tmp_path, "a.mp4", f"testsrc2={HOUSE}:d=3")
    second = _render(env, tmp_path, "b.mp4", f"color=c=red:{HOUSE}:d=2")
    (home / "authority.json").write_bytes((REPO / "authority.json").read_bytes())
    creature = loop.Creature(home, transport=FakeModel())
    joined, problems = creature.join([first, second], 150 + 30)
    assert joined is not None
    assert [p.split(":")[0] for p in problems] == ["duration", "frames"]
    assert "frames: got 150, need exactly 180" in problems
