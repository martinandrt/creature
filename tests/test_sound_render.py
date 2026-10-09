"""Sound put under a real clip in the real workshop container: the library is mounted read-only at /assets,
the mix and the mux run in the container, and the output is measured in a second container run. The clip is
three seconds of testsrc with a hard cut at 1.5 s; the library is a temporary folder with two short hits
and a loop, so no test depends on the real asset library."""

import dataclasses

import pytest

from creature import authority, sound, workshop
from tests.conftest import REPO
from tests.soundlib import make_library
from tests.test_sound import decode_mono, loudest_rise, probe

pytestmark = pytest.mark.docker

CLIP_CODE = """
import subprocess
def run(input, work):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=360x640:r=30:d=1.5",
         "-f", "lavfi", "-i", "testsrc2=s=360x640:r=30:d=1.5",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p", "-c:v", "libx264",
         f"{work}/out/clip.mp4"], check=True, capture_output=True)
    return "ok"
"""


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    folder = tmp_path_factory.mktemp("sound")
    library = make_library(folder / "library", hits=2, loops=1)
    limits = dataclasses.replace(authority.load(REPO).workshop, timeout_s=120, assets=str(library))
    image = workshop.ensure_image(REPO / "workshop")
    made = workshop.run(CLIP_CODE, {}, limits=limits, image=image)
    assert made.ok, made.error
    clip = folder / "clip.mp4"
    clip.write_bytes(made.outputs["clip.mp4"])
    return limits, image, clip


def test_a_clip_with_a_cut_at_1_5_s_gets_a_sound_with_an_onset_on_the_cut(env):
    limits, image, clip = env
    silent = clip.read_bytes()
    result = sound.score(clip, seed="run-1", cues=[45], limits=limits, image=image)
    assert result.ok, result.problems
    assert result.out == clip.with_name("clip-sound.mp4") and result.out.is_file()
    assert clip.read_bytes() == silent and "audio" not in probe(clip)  # the silent original is untouched
    streams = probe(result.out)
    assert streams["audio"]["codec_name"] == "aac" and streams["video"]["codec_name"] == "h264"
    assert float(streams["audio"]["duration"]) == pytest.approx(
        float(streams["video"]["duration"]), abs=1 / 30
    )
    # one of the two hits on the cut, and the loop under everything: read from the read-only mount
    assert result.cues == [45] and result.sounds[-1] == "zvuky/smycka-a.wav" and len(result.sounds) == 2
    assert result.sounds[0] in ("zvuky/uder-a.wav", "zvuky/uder-b.wav")
    # an onset at 1.5 s ± 2 frames, found by a check of the test's own (10 ms windows), not by the detector
    assert abs(loudest_rise(decode_mono(result.out), 0.6, 2.4) - 1.5) <= 2 / 30
    # the measurement taken on the written file in the container
    assert result.aligned_share == 1.0 and result.rms_db > -40 and result.peak_db <= 0
    assert not result.detected and result.problems == []


def test_the_cut_is_found_in_the_clip_when_nobody_says_where_it_is(env, tmp_path):
    limits, image, clip = env
    copy = tmp_path / "found.mp4"
    copy.write_bytes(clip.read_bytes())
    result = sound.score(copy, seed="run-2", limits=limits, image=image)
    assert result.ok, result.problems
    assert result.detected and result.cues == [45] and result.aligned_share == 1.0
    assert abs(loudest_rise(decode_mono(result.out), 0.6, 2.4) - 1.5) <= 2 / 30
    assert result.out.name == "found-sound.mp4"


def test_the_same_run_id_gives_the_same_sound_and_another_may_not(env, tmp_path):
    limits, image, clip = env
    first, second = tmp_path / "a.mp4", tmp_path / "b.mp4"
    first.write_bytes(clip.read_bytes())
    second.write_bytes(clip.read_bytes())
    one = sound.score(first, seed="same", cues=[45], limits=limits, image=image)
    two = sound.score(second, seed="same", cues=[45], limits=limits, image=image)
    assert one.ok and two.ok and one.sounds == two.sounds
    assert one.out.read_bytes() == two.out.read_bytes()


def test_a_home_with_no_library_runs_nothing(env, tmp_path):
    limits, image, clip = env
    bare = dataclasses.replace(limits, assets=None)
    result = sound.score(clip, seed="x", cues=[45], limits=bare, image=image)
    assert not result.ok and "no asset library" in result.problems[0]
