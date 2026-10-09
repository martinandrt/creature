"""Sound under a finished clip (creature/sound.py), offline.

The fixed workshop code is exercised in this process (it only needs numpy and ffmpeg, which the dev
environment has): the choice of sounds is a pure function of the cues, the library and the run id, the mix
is an array we can measure, and a whole clip goes through the same `run()` the workshop runs. The loop
tests fake `sound.score` and pin what the spine does with its answer. The real container is in
test_sound_render.py."""

import dataclasses
import itertools
import json
import math
import shutil
import subprocess

import numpy as np
import pytest

from creature import authority, board, ledger, loop, montage, page, registry, sound, workshop
from creature.perceive import Reel
from tests import test_loop as tl
from tests.conftest import REPO
from tests.fakes import FakeModel
from tests.soundlib import RATE, make_library

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")


def code(source):
    namespace = {"__name__": "skill"}
    exec(compile(source, "skill.py", "exec"), namespace)
    return namespace


@pytest.fixture(scope="module")
def mixer():
    return code(sound.SOUND_CODE)


@pytest.fixture(scope="module")
def measurer():
    return code(sound.MEASURE_CODE)


@pytest.fixture
def rich(tmp_path):
    return make_library(tmp_path / "rich", hits=3, whooshes=3, keys=3, risers=2, loops=1)


def plan(mixer, root, cues, *, frames=300, seed="run-1", fps=30.0, kind="cut"):
    sounds = mixer["library_sounds"](str(root))
    return mixer["make_plan"](sounds, [{"frame": f, "kind": kind} for f in cues], frames, fps, seed)


def kinds(made):
    return [p["kind"] for p in made["placements"]]


# --- the library and the choice -----------------------------------------------------------------


def test_the_library_is_read_by_category_and_a_missing_file_or_a_graphic_is_left_out(mixer, rich):
    sounds = mixer["library_sounds"](str(rich))
    assert [e["name"] for e in sounds["hit"]] == ["uder-a", "uder-b", "uder-c"]  # uder-chybi has no file
    assert len(sounds["whoosh"]) == 3 and len(sounds["key"]) == 3 and len(sounds["riser"]) == 2
    assert [e["name"] for e in sounds["loop"]] == ["smycka-a"] == [e["name"] for e in sounds["pad"]]
    assert all(e["path"].startswith("zvuky/") for k in sounds.values() for e in k)  # the shape was ignored


def test_the_same_run_id_always_chooses_the_same_sounds(mixer, rich):
    cues = [40, 70, 100, 130, 160, 190]
    assert plan(mixer, rich, cues, seed="20261009T010000Z-aaaaaa") == plan(
        mixer, rich, cues, seed="20261009T010000Z-aaaaaa"
    )


def test_other_run_ids_choose_other_sounds(mixer, rich):
    cues = [40, 70, 100, 130, 160, 190]
    plans = {json.dumps(plan(mixer, rich, cues, seed=f"run-{n}"), sort_keys=True) for n in range(8)}
    assert len(plans) > 1


def test_hard_cuts_alternate_a_hit_and_a_whoosh_and_no_file_comes_twice_in_a_row(mixer, rich):
    cues = list(range(40, 290, 25))  # a cut every 25 frames: well under 6 to a second
    for seed in ("a", "b", "c", "d"):
        made = plan(mixer, rich, cues, seed=seed)
        cuts = [p for p in made["placements"] if p["kind"] != "riser"]
        assert [p["kind"] for p in cuts] == ["hit", "whoosh"] * 5, seed
        files = [p["file"] for p in cuts]
        assert all(a != b for a, b in itertools.pairwise(files)), files
        hits = [p["file"] for p in cuts if p["kind"] == "hit"]
        assert all(a != b for a, b in itertools.pairwise(hits)), hits
        assert [p["frame"] for p in cuts] == cues


def test_a_library_with_two_hits_and_no_whoosh_alternates_the_two_hits(mixer, tmp_path):
    root = make_library(tmp_path / "poor", hits=2, whooshes=0)
    made = plan(mixer, root, list(range(40, 280, 20)))
    files = [p["file"] for p in made["placements"] if p["kind"] != "riser"]
    assert len(files) == 12 and set(kinds(made)) == {"hit"}
    assert all(a != b for a, b in itertools.pairwise(files)), files
    assert made["bed"] == "zvuky/smycka-a.wav"


def test_one_sound_alone_may_repeat_since_there_is_nothing_else(mixer, tmp_path):
    root = make_library(tmp_path / "one", hits=1)
    made = plan(mixer, root, [40, 80, 120])
    assert [p["file"] for p in made["placements"]] == ["zvuky/uder-a.wav"] * 3


def test_a_riser_starts_on_the_first_frame_only_when_it_ends_before_the_first_cut(mixer, rich):
    early = plan(mixer, rich, [20, 60])  # 0.67 s to the first cut: the risers last 1.0 s
    assert "riser" not in kinds(early)
    late = plan(mixer, rich, [60, 100])  # 2 s
    first = late["placements"][0]
    assert first["kind"] == "riser" and first["frame"] == 0 and first["anchor"] is False
    assert first["file"] in ("zvuky/nabeh-a.wav", "zvuky/nabeh-b.wav")
    assert "riser" not in kinds(plan(mixer, rich, []))  # nothing to lead into


def test_six_cues_inside_one_second_are_key_sounds_and_five_are_not(mixer, rich):
    every5 = plan(mixer, rich, list(range(10, 160, 5)))  # 6 cues in 25 frames, all the way
    assert set(kinds(every5)) == {"key"} and len(every5["dense"]) == 30
    every6 = plan(mixer, rich, list(range(10, 160, 6)))  # 6 cues need 30 frames: not inside one second
    assert every6["dense"] == [] and set(kinds(every6)) - {"riser"} == {"hit", "whoosh"}
    burst = plan(mixer, rich, [100, 102, 104, 106, 108, 110, 200, 260])  # a burst in the middle
    assert burst["dense"] == [100, 102, 104, 106, 108, 110]
    assert [p["kind"] for p in burst["placements"] if p["frame"] in (200, 260)] == ["hit", "whoosh"]


def test_the_first_frame_and_cues_outside_the_clip_get_no_sound(mixer, rich):
    made = plan(mixer, rich, [0, 40, 299, 300, 450, -3], frames=300)
    assert made["cues"] == [40, 299]


def test_motion_onsets_are_sounded_like_cuts(mixer, rich):
    made = plan(mixer, rich, [50, 90], kind="onset")
    assert [p["kind"] for p in made["placements"] if p["kind"] != "riser"] == ["hit", "whoosh"]


def test_the_bed_is_the_loop_with_the_pad_tags(mixer, tmp_path):
    root = make_library(tmp_path / "loops", hits=1, loops=2)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["prvky"]:
        if entry["jmeno"] == "smycka-b":
            entry["stitky"] = ["smycka", "puls"]  # a rhythmic loop is not the quiet bed
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for seed in ("a", "b", "c", "d", "e"):
        assert plan(mixer, root, [40], seed=seed)["bed"] == "zvuky/smycka-a.wav"
    no_loop = make_library(tmp_path / "noloop", hits=1, loops=0)
    assert plan(mixer, no_loop, [40])["bed"] is None


# --- the mix ----------------------------------------------------------------------------------


def burst(seconds=0.2, amp=0.7079, freq=1000.0):
    t = np.arange(int(seconds * RATE)) / RATE
    mono = amp * np.exp(-t / 0.05) * np.sin(2 * np.pi * freq * t)
    return np.stack([mono, mono], axis=1).astype(np.float32)


def pad(seconds=2.0, amp=0.7079):
    t = np.arange(int(seconds * RATE)) / RATE
    mono = amp * np.sin(2 * np.pi * 220 * t)
    return np.stack([mono, mono], axis=1).astype(np.float32)


def placed(frames, *, kind="hit", file="h"):
    return [{"kind": kind, "file": file, "frame": f, "anchor": True} for f in frames]


def db(x):
    return 20 * math.log10(max(float(np.abs(x).max()), 1e-9))


@pytest.mark.parametrize("n", [3 * RATE, 3 * RATE + 33, RATE // 4])
def test_the_mix_is_exactly_the_length_of_the_clip(mixer, n):
    mix, _ = mixer["render"](n, placed([5, 30, 60, 200]), {"h": burst()}, pad(), 30.0)
    assert mix.shape == (n, 2) and mix.dtype == np.float32


def test_the_mix_is_peak_normalised_and_never_clips_however_much_piles_up(mixer):
    n = 4 * RATE
    mix, _ = mixer["render"](n, placed(range(10, 110, 4)), {"h": burst(0.5, 0.95)}, pad(), 30.0)
    assert db(mix) == pytest.approx(-1.0, abs=0.05)
    assert float(np.abs(mix).max()) < 1.0


def test_the_bed_runs_under_the_whole_clip_twenty_db_below_the_sounds(mixer):
    n = 5 * RATE
    mix, parts = mixer["render"](n, placed([45, 90]), {"h": burst()}, pad(), 30.0)
    bed = parts["bed"]
    for second in np.arange(0.4, 4.4, 0.1):  # past the fade in, before the fade out
        window = bed[int(second * RATE) : int((second + 0.1) * RATE)]
        assert float(np.abs(window).max()) > 0.01, second  # never a silent stretch, hits or not
    assert abs(bed[0]).max() < 1e-3 and abs(bed[-1]).max() < 1e-3  # short fades at both ends
    sounds_only = mix - bed
    assert db(bed) - db(sounds_only) == pytest.approx(-20.0, abs=1.0)
    quiet = slice(int(3.5 * RATE), int(4.0 * RATE))  # no sound there: the mix is the bed
    assert np.allclose(mix[quiet], bed[quiet], atol=1e-5)


def test_a_clip_without_cues_keeps_a_quiet_bed_not_a_loud_one(mixer):
    mix, parts = mixer["render"](3 * RATE, [], {}, pad(), 30.0)
    assert db(mix) < -15.0  # the boost to -1 dBFS is capped at 6 dB: the bed was 23 dB under full scale
    assert db(mix) == pytest.approx(-23.0 + 6.0, abs=0.2) and parts["items"] == []


def test_without_a_loop_there_is_still_a_mix(mixer):
    mix, _ = mixer["render"](2 * RATE, placed([30]), {"h": burst()}, None, 30.0)
    assert mix.shape == (2 * RATE, 2) and db(mix) == pytest.approx(-1.0, abs=0.05)


def test_a_sound_is_silent_when_the_next_one_starts(mixer):
    mix, parts = mixer["render"](3 * RATE, placed([30, 33]), {"h": burst(0.5)}, None, 30.0)
    first, second = parts["items"]
    assert first["end"] == second["start"] == pytest.approx(33 * 1600, abs=RATE // 100)  # the 500 ms were cut
    assert float(np.abs(mix[first["end"] - 5 : first["end"]]).max()) < 0.05  # faded out, no click


def test_a_whoosh_is_started_where_it_becomes_audible_not_where_it_creeps_in(mixer):
    t = np.arange(int(0.8 * RATE)) / RATE
    swell = (np.minimum(t / 0.5, 1.0) ** 3 * np.random.default_rng(1).standard_normal(len(t)) * 0.5).astype(
        np.float32
    )
    audio = {"w": np.stack([swell, swell], axis=1) / np.abs(swell).max() * 0.7079}
    mix, parts = mixer["render"](3 * RATE, placed([45], kind="whoosh", file="w"), audio, pad(), 30.0)
    [item] = parts["items"]
    assert item["start"] <= 45 * 1600 and item["x"].shape[0] < audio["w"].shape[0]  # the creeping lead is cut
    onsets = mixer["audio_onsets"](mix.mean(axis=1))
    assert any(abs(o * 30 - 45) <= 2 for o in onsets), onsets


# --- the onset detector ------------------------------------------------------------------------


def test_the_onset_detector_finds_a_hit_in_a_quiet_bed_and_nothing_in_a_steady_tone(mixer):
    n = 3 * RATE
    steady = np.sin(2 * np.pi * 220 * np.arange(n) / RATE) * 0.05
    assert mixer["audio_onsets"](steady) == []
    with_hit = steady.copy()
    with_hit[RATE : RATE + 4800] += burst(0.1)[:4800, 0]
    [found] = mixer["audio_onsets"](with_hit)
    assert found == pytest.approx(1.0, abs=0.02)


# --- the whole clip, through the workshop's own entry point --------------------------------------


def run_code(namespace, work, job):
    return namespace["run"](job, str(work))


def make_clip(path, kind):
    """Three seconds: a cut at 1.5 s (two different sources), motion that starts at 1.5 s, or nothing."""
    size = "s=360x640:r=30"
    if kind == "cut":
        args = ["-f", "lavfi", "-i", f"testsrc={size}:d=1.5", "-f", "lavfi", "-i", f"testsrc2={size}:d=1.5"]
        args += ["-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p"]
    elif kind == "motion":  # a box pops in at 1.5 s and slides on
        graph = (
            f"color=black:{size}:d=3[bg];color=white:s=80x80:r=30:d=3[box];"
            "[bg][box]overlay=x='if(lt(t,1.5),-80,(t-1.5)*250)':y=200:eval=frame,format=yuv420p"
        )
        args = ["-filter_complex", graph]
    else:
        args = ["-f", "lavfi", "-i", f"color=gray:{size}:d=3", "-pix_fmt", "yuv420p"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args, "-c:v", "libx264", str(path)], check=True)
    return path


def work_for(tmp_path, clip, name="work"):
    work = tmp_path / name
    (work / "in").mkdir(parents=True)
    (work / "out").mkdir()
    shutil.copy(clip, work / "in" / "clip.mp4")
    return work


def decode_mono(path):
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-f", "f32le", "-ar", str(RATE), "-ac", "1", "-"],
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(out, dtype=np.float32)


def loudest_rise(x, lo_s, hi_s):
    """When the level climbs the most inside [lo_s, hi_s]: 10 ms windows, a simple check of our own."""
    w = RATE // 100
    count = len(x) // w
    level = 10 * np.log10((x[: count * w].reshape(count, w) ** 2).mean(axis=1) + 1e-10)
    rise = level[3:] - level[:-3]
    at = (np.arange(len(rise)) + 3) * w / RATE
    inside = (at >= lo_s) & (at <= hi_s)
    return float(at[inside][np.argmax(rise[inside])])


def video_md5(path):
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v", "-c", "copy", "-f", "md5", "-"],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,duration", "-of", "json",
         str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return {s["codec_type"]: s for s in json.loads(out.stdout)["streams"]}


@needs_ffmpeg
def test_a_clip_with_a_cut_gets_a_sound_on_the_cut_and_keeps_its_picture(mixer, measurer, rich, tmp_path):
    clip = make_clip(tmp_path / "cut.mp4", "cut")
    work = work_for(tmp_path, clip)
    job = {"seed": "run-9", "cues": [{"frame": 45, "kind": "cut"}], "assets": str(rich)}
    made = run_code(mixer, work, job)
    assert made["cues"] == [45] and made["bed"] == "zvuky/smycka-a.wav" and made["detected"] is False
    assert made["sounds"][-1] == "zvuky/smycka-a.wav" and 1 < len(made["sounds"]) <= 3
    out = work / "out" / "sound.mp4"
    streams = probe(out)
    assert streams["audio"]["codec_name"] == "aac" and streams["video"]["codec_name"] == "h264"
    assert float(streams["audio"]["duration"]) == pytest.approx(
        float(streams["video"]["duration"]), abs=1 / 30
    )
    assert video_md5(out) == video_md5(clip)  # the picture was copied, not re-encoded
    assert "audio" not in probe(clip)
    # an onset of our own measuring, not the detector's: the loudest climb after the fade-in is at 1.5 s
    assert abs(loudest_rise(decode_mono(out), 0.6, 2.4) - 1.5) <= 2 / 30
    shutil.copy(out, work / "in" / "sound.mp4")
    measured = run_code(measurer, work, {"file": "sound.mp4", "cues": made["cues"], "fps": made["fps"]})
    assert measured["audio"] is True and measured["aligned_share"] == 1.0
    assert -40 < measured["rms_db"] < 0 and measured["peak_db"] <= 0


@needs_ffmpeg
def test_cues_are_found_in_the_clip_itself_when_no_timeline_gives_them(mixer, rich, tmp_path):
    cut = run_code(
        mixer,
        work_for(tmp_path, make_clip(tmp_path / "cut.mp4", "cut"), "w1"),
        {"seed": "s", "cues": None, "assets": str(rich)},
    )
    assert cut["detected"] is True and cut["cues"] == [45]  # a scene change at 1.5 s
    motion = run_code(
        mixer,
        work_for(tmp_path, make_clip(tmp_path / "motion.mp4", "motion"), "w2"),
        {"seed": "s", "cues": None, "assets": str(rich)},
    )
    assert motion["cues"] == [45]  # the box pops in and slides on: one onset, not one per frame
    still = run_code(
        mixer,
        work_for(tmp_path, make_clip(tmp_path / "still.mp4", "still"), "w3"),
        {"seed": "s", "cues": None, "assets": str(rich)},
    )
    assert still["cues"] == [] and still["sounds"] == ["zvuky/smycka-a.wav"]  # a still clip: the bed alone


@needs_ffmpeg
def test_a_clip_with_cues_every_few_frames_is_lined_up_cue_by_cue(mixer, measurer, rich, tmp_path):
    clip = make_clip(tmp_path / "cut.mp4", "cut")
    work = work_for(tmp_path, clip)
    cues = [{"frame": f, "kind": "cut"} for f in range(0, 90, 8)]  # a montage: a cut every 8 frames
    made = run_code(mixer, work, {"seed": "run-3", "cues": cues, "assets": str(rich)})
    assert made["cues"] == list(range(8, 90, 8)) and made["dense"] == []
    shutil.copy(work / "out" / "sound.mp4", work / "in" / "sound.mp4")
    measured = run_code(measurer, work, {"file": "sound.mp4", "cues": made["cues"], "fps": made["fps"]})
    assert measured["aligned_share"] >= 0.9 and measured["chance_share"] <= 0.1, measured
    assert [p["kind"] for p in made["placements"]] == ["hit", "whoosh"] * 5 + ["hit"]


@needs_ffmpeg
def test_the_same_run_id_makes_the_same_file_bytes_for_bytes(mixer, rich, tmp_path):
    clip = make_clip(tmp_path / "cut.mp4", "cut")
    job = {"seed": "run-4", "cues": [{"frame": 30, "kind": "cut"}, {"frame": 60, "kind": "cut"}]}
    job["assets"] = str(rich)
    first = work_for(tmp_path, clip, "a")
    second = work_for(tmp_path, clip, "b")
    run_code(mixer, first, job)
    run_code(mixer, second, job)
    assert (first / "out" / "sound.mp4").read_bytes() == (second / "out" / "sound.mp4").read_bytes()


# --- the verdict on a measurement --------------------------------------------------------------

MADE = {"sounds": ["zvuky/uder-a.wav", "zvuky/smycka-a.wav"], "cues": [45], "detected": False}
GOOD = {
    "audio": True, "rms_db": -22.0, "peak_db": -1.2, "audio_s": 3.0, "video_s": 3.0, "cues": 1,
    "aligned_share": 1.0, "chance_share": None,
}  # fmt: skip


def test_a_measurement_that_passes_every_check_is_ok():
    result = sound.assess(MADE, GOOD)
    assert result.ok and result.problems == [] and result.sounds == MADE["sounds"] and result.cues == [45]
    assert (result.rms_db, result.peak_db, result.aligned_share) == (-22.0, -1.2, 1.0)


@pytest.mark.parametrize(
    ("change", "word"),
    [
        ({"audio": False}, "no audio stream"),
        ({"rms_db": -41.0}, "too quiet"),
        ({"rms_db": -40.0}, "too quiet"),  # "above -40" is strict
        ({"peak_db": 0.4}, "clips"),
        ({"audio_s": 3.5}, "audio lasts"),
        ({"aligned_share": 0.5, "cues": 4}, "50% of 4 cues"),
    ],
)
def test_each_failed_check_names_itself(change, word):
    result = sound.assess(MADE, {**GOOD, **change})
    assert not result.ok and any(word in p for p in result.problems), result.problems


def test_no_cues_to_line_up_is_not_a_failure():
    assert sound.assess({**MADE, "cues": []}, {**GOOD, "aligned_share": None, "cues": 0}).ok


def test_a_measurement_that_could_not_be_taken_is_a_problem_not_a_pass():
    result = sound.assess(MADE, None, "killed: timeout")
    assert not result.ok and "could not be measured: killed: timeout" in result.problems[0]


# --- score() with a faked workshop -------------------------------------------------------------


def faked_workshop(monkeypatch, *, made_ok=True, measured=None, calls=None):
    value = {"sounds": MADE["sounds"], "cues": [45], "fps": 30.0, "out": "sound.mp4", "detected": False}

    def run(source, job, files=None, **kwargs):
        if calls is not None:
            calls.append((source, job, {k: v.read_bytes() for k, v in (files or {}).items()}))
        if source == sound.SOUND_CODE:
            if not made_ok:
                return workshop.WorkshopResult(False, None, {}, "killed: timeout", "timeout", 1.0, "")
            return workshop.WorkshopResult(True, value, {"sound.mp4": b"SOUNDED"}, None, None, 1.0, "")
        return workshop.WorkshopResult(True, measured, {}, None, None, 0.5, "")

    monkeypatch.setattr(sound.workshop, "run", run)


def limits(tmp_path, assets=True):
    base = authority.load(REPO).workshop
    return dataclasses.replace(base, assets=str(tmp_path) if assets else None)


def test_the_sounded_file_appears_next_to_the_clip_only_after_it_passed(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"SILENT")
    calls = []
    faked_workshop(monkeypatch, measured=GOOD, calls=calls)
    result = sound.score(clip, seed="r", cues=[45], limits=limits(tmp_path), image="img")
    assert result.ok and result.out == tmp_path / "clip-sound.mp4" and result.out.read_bytes() == b"SOUNDED"
    assert clip.read_bytes() == b"SILENT"  # the silent original is untouched
    mix_call, measure_call = calls
    assert mix_call[1] == {"seed": "r", "cues": [{"frame": 45, "kind": "cut"}], "assets": "/assets"}
    assert mix_call[2] == {"clip.mp4": b"SILENT"}
    assert measure_call[2] == {"sound.mp4": b"SOUNDED"}  # the measurement is on the written bytes
    assert measure_call[1]["cues"] == [45] and result.seconds == 1.5


def test_a_montage_gets_montage_sound_mp4(tmp_path, monkeypatch):
    clip = tmp_path / "montage.mp4"
    clip.write_bytes(b"SILENT")
    faked_workshop(monkeypatch, measured=GOOD)
    assert sound.score(clip, seed="r", limits=limits(tmp_path), image="img").out.name == "montage-sound.mp4"


def test_a_sound_that_fails_its_measurement_leaves_no_file(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"SILENT")
    faked_workshop(monkeypatch, measured={**GOOD, "rms_db": -55.0})
    result = sound.score(clip, seed="r", limits=limits(tmp_path), image="img")
    assert not result.ok and result.out is None and "too quiet" in result.problems[0]
    assert {p.name for p in tmp_path.iterdir()} - {"home"} == {"clip.mp4"}  # nothing left beside the clip


def test_a_workshop_that_fails_is_a_failed_score_not_an_exception(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"SILENT")
    faked_workshop(monkeypatch, made_ok=False)
    result = sound.score(clip, seed="r", limits=limits(tmp_path), image="img")
    assert (
        not result.ok
        and "killed: timeout" in result.problems[0]
        and not (tmp_path / "clip-sound.mp4").exists()
    )


def test_without_a_library_nothing_is_run(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"SILENT")
    calls = []
    faked_workshop(monkeypatch, measured=GOOD, calls=calls)
    result = sound.score(clip, seed="r", limits=limits(tmp_path, assets=False), image="img")
    assert not result.ok and "no asset library" in result.problems[0] and calls == []


def test_the_silent_clip_can_never_be_the_output(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"SILENT")
    faked_workshop(monkeypatch, measured=GOOD)
    with pytest.raises(ValueError, match="overwrite"):
        sound.score(clip, seed="r", limits=limits(tmp_path), image="img", out=clip)


def test_cues_may_be_frames_or_timeline_cues_and_a_timeline_gives_its_cuts():
    assert sound._cues([8, 16]) == [{"frame": 8, "kind": "cut"}, {"frame": 16, "kind": "cut"}]
    assert sound._cues([{"frame": 8, "kind": "onset", "scene": "s01"}]) == [{"frame": 8, "kind": "onset"}]
    assert sound._cues(None) is None
    t = montage.timeline(
        "m", {"a": {"skill": "s", "version": 1, "params": {}}}, ["a"], every=10, frames=60, layers=[]
    )
    assert [c["frame"] for c in sound.timeline_cues(t)] == [0, 10, 20, 30, 40, 50]
    scenes_only = {k: v for k, v in t.items() if k != "cues"}
    assert [c["frame"] for c in sound.timeline_cues(scenes_only)] == [0, 10, 20, 30, 40, 50]


def test_the_ledger_event_is_plain_json():
    result = sound.Scored(
        True, sounds=["zvuky/a.wav"], cues=list(range(300)), aligned_share=1.0, rms_db=-20.0
    )
    event = result.event()
    json.dumps(event, allow_nan=False)
    assert len(event["cues"]) == sound.MAX_LISTED and event["cue_count"] == 300 and event["ok"] is True


# --- the loop ---------------------------------------------------------------------------------


class Scorer:
    """A stand-in for sound.score: records what it was asked and answers as scripted."""

    def __init__(self, problems=(), raises=None):
        self.calls, self.problems, self.raises = [], problems, raises

    def __call__(self, clip, *, seed, limits, image, cues=None, out=None):
        self.calls.append({"clip": clip, "seed": seed, "cues": cues, "assets": limits.assets})
        if self.raises:
            raise self.raises
        if self.problems:
            return sound.Scored.failed(*self.problems)
        target = sound.output_path(clip)
        target.write_bytes(b"sounded")
        return sound.Scored(
            True, out=target, sounds=["zvuky/uder-a.wav", "zvuky/smycka-a.wav"], cues=[45, 75],
            aligned_share=1.0, chance_share=0.0, rms_db=-21.4, peak_db=-1.2, detected=True,
        )  # fmt: skip


def _setup(home, tmp_path, monkeypatch, *, library):
    if library:
        lib = make_library(tmp_path / "lib")
        tl._authority(home, lambda a: a["workshop"].update(assets=str(lib)))
    else:
        tl._authority(home)
    strip = tmp_path / "reel-strip.png"
    strip.write_bytes(tl.PNG)
    reel = Reel(
        source="reel.mp4", caption="Typewriter tutorial", transcript="type it slowly", author="anet",
        video=tmp_path / "reel.mp4", strip=strip, times=(0.5, 1.5), duration_s=12.0,
        width=720, height=1280, fps=30.0,
    )  # fmt: skip
    monkeypatch.setattr(loop.perceive, "perceive", lambda *a, **k: reel)
    monkeypatch.setattr(loop.workshop, "ensure_image", lambda *a, **k: tl.IMAGE)
    monkeypatch.setattr(loop.workshop, "image_id", lambda *a, **k: tl.IMAGE)
    monkeypatch.setattr(loop.workshop, "run", lambda *a, **k: tl.RENDER)
    monkeypatch.setattr(loop.verdict, "check", tl._check_passes)
    return home


@pytest.fixture
def library_home(home, tmp_path, monkeypatch):
    return _setup(home, tmp_path, monkeypatch, library=True)


@pytest.fixture
def bare_home(home, tmp_path, monkeypatch):
    return _setup(home, tmp_path, monkeypatch, library=False)


def events_of(home, report, kind):
    return [e for e in ledger.read(home / "runs" / f"{report.run_id}.jsonl") if e["type"] == kind]


def test_a_built_clip_gets_sound_and_the_run_points_at_the_sounded_file(
    library_home, fake_model, monkeypatch
):
    scorer = Scorer()
    monkeypatch.setattr(loop.sound, "score", scorer)
    report = tl._build(library_home, fake_model)
    silent = report.folder / "attempt-1" / "clip.mp4"
    assert report.status == "BUILT" and report.clip == report.folder / "attempt-1" / "clip-sound.mp4"
    assert silent.read_bytes() == b"clip" and report.clip.read_bytes() == b"sounded"
    [call] = scorer.calls
    assert call["clip"] == silent and call["seed"] == report.run_id and call["cues"] is None  # no timeline
    assert call["assets"] == str(tmp_assets(library_home))
    [scored] = events_of(library_home, report, "scored")
    assert scored["ok"] is True and scored["sounds"] == ["zvuky/uder-a.wav", "zvuky/smycka-a.wav"]
    assert scored["cues"] == [45, 75] and scored["aligned_share"] == 1.0 and scored["problems"] == []
    assert scored["rms_db"] == -21.4 and scored["peak_db"] == -1.2
    [end] = events_of(library_home, report, "run_end")
    assert end["clip"] == "attempt-1/clip-sound.mp4" and end["status"] == "BUILT"
    assert "no sound" not in " ".join(report.notes)
    assert "sound added: 2 sounds, 100% of 2 cues" in page._describe(scored | {"type": "scored"})


def tmp_assets(home):
    return json.loads((home / "authority.json").read_text(encoding="utf-8"))["workshop"]["assets"]


def test_a_sound_that_fails_leaves_the_silent_clip_a_note_and_a_passed_run(
    library_home, fake_model, monkeypatch
):
    monkeypatch.setattr(loop.sound, "score", Scorer(problems=("only 40% of 5 cues have an audio onset",)))
    report = tl._build(library_home, fake_model)
    assert report.status == "BUILT" and report.clip == report.folder / "attempt-1" / "clip.mp4"
    assert any(
        n.startswith("no sound: only 40%") and n.endswith("the silent clip stays") for n in report.notes
    )
    [scored] = events_of(library_home, report, "scored")
    assert scored["ok"] is False and scored["problems"] == ["only 40% of 5 cues have an audio onset"]
    assert "sound skipped" in page._describe(scored | {"type": "scored"})
    assert (report.folder / "page.html").exists()


def test_a_scorer_that_crashes_cannot_fail_the_run(library_home, fake_model, monkeypatch):
    monkeypatch.setattr(loop.sound, "score", Scorer(raises=RuntimeError("docker fell over")))
    report = tl._build(library_home, fake_model)
    assert report.status == "BUILT" and report.clip == report.folder / "attempt-1" / "clip.mp4"
    [scored] = events_of(library_home, report, "scored")
    assert scored["ok"] is False and "RuntimeError: docker fell over" in scored["problems"][0]


def test_a_home_without_a_library_keeps_the_silent_clip_and_says_so(bare_home, fake_model, monkeypatch):
    scorer = Scorer()
    monkeypatch.setattr(loop.sound, "score", scorer)
    report = tl._build(bare_home, fake_model)
    assert report.status == "BUILT" and report.clip == report.folder / "attempt-1" / "clip.mp4"
    assert scorer.calls == [] and events_of(bare_home, report, "scored") == []
    assert any(n.startswith("no sound: this home mounts no asset library") for n in report.notes)


def test_a_reused_skill_gets_sound_too(library_home, fake_model, monkeypatch):
    tl._build(library_home, fake_model)
    scorer = Scorer()
    monkeypatch.setattr(loop.sound, "score", scorer)
    fresh = FakeModel()
    fresh.queue("planner", {"skill": "typewriter-reveal", "reason": "same technique"})
    fresh.queue("judge", tl._judge(True, True, True))
    report = tl._creature(library_home, fresh).try_reel("another.mp4", "A different line.")
    assert report.status == "HAVE" and report.clip.name == "clip-sound.mp4"
    assert report.clip.parent.name == "have-typewriter-reveal" and len(scorer.calls) == 1


def test_a_design_replay_gets_sound_and_costs_nothing(library_home, fake_model, monkeypatch):
    tl._build(library_home, fake_model)
    scorer = Scorer()
    monkeypatch.setattr(loop.sound, "score", scorer)
    silent = FakeModel()
    report = tl._creature(library_home, silent).run_design("typewriter-reveal", "Replay this.")
    assert report.status == "DONE" and report.clip.name == "clip-sound.mp4" and silent.calls == []
    assert report.spent_usd == 0 and scorer.calls[0]["cues"] is None


@pytest.mark.parametrize("status", ["FAILED", "ASK", "REFUSE", "SKIP"])
def test_only_a_passed_run_with_a_clip_is_scored(library_home, fake_model, monkeypatch, status):
    scorer = Scorer()
    monkeypatch.setattr(loop.sound, "score", scorer)
    creature = tl._creature(library_home, fake_model)
    clip = creature.folder / "clip.mp4"
    clip.write_bytes(b"clip")
    report = loop.Report(creature.ledger.run_id, status, creature.folder, clip=clip)
    creature.add_sound(report)
    assert scorer.calls == [] and report.clip == clip and report.notes == []
    gone = loop.Report(creature.ledger.run_id, "DONE", creature.folder, clip=creature.folder / "gone.mp4")
    creature.add_sound(gone)
    nothing = loop.Report(creature.ledger.run_id, "DONE", creature.folder)  # wishes, tools: no clip
    creature.add_sound(nothing)
    assert scorer.calls == [] and gone.notes == [] and nothing.notes == []


@pytest.fixture
def montage_home(library_home, monkeypatch):
    monkeypatch.setattr(
        loop.perceive, "frames_at", lambda reel, times, target, **k: (target.write_bytes(tl.PNG), target)[1]
    )

    def run(code, input, files=None, **kwargs):
        return tl.MONTAGE_RESULT if code == loop.montage.MONTAGE_CODE else tl.RENDER

    monkeypatch.setattr(loop.workshop, "run", run)
    return library_home


def test_a_montage_is_scored_with_the_cues_of_its_timeline(montage_home, fake_model, monkeypatch):
    scorer = Scorer()
    monkeypatch.setattr(loop.sound, "score", scorer)
    creature, report = tl._learn_montage(montage_home, fake_model)
    assert report.status == "BUILT" and report.clip == creature.folder / "montage-1" / "montage-sound.mp4"
    t = registry.design(montage_home / "registry", "grid-promo")["timeline"]
    [call] = scorer.calls
    assert call["clip"].name == "montage.mp4" and call["cues"] == sound.timeline_cues(t)
    # a saved timeline with cuts every 10 frames replays with those cuts
    made = montage.timeline("cuts", t["sources"], ["a"], every=10, frames=60, layers=[])
    registry.save_timeline(montage_home / "registry", "cuts", made, origin={"by": "test"})
    again = tl._creature(montage_home, FakeModel()).run_design("cuts", "Stay curious.")
    assert again.status == "DONE" and again.clip == again.folder / "montage" / "montage-sound.mp4"
    assert [c["frame"] for c in scorer.calls[-1]["cues"]] == [0, 10, 20, 30, 40, 50]


# --- the page and the board ---------------------------------------------------------------------


def test_the_run_page_reads_a_scored_event_in_plain_english():
    ok = {"type": "scored", "ok": True, "sounds": ["a", "b", "c"], "cue_count": 12, "aligned_share": 0.917,
          "rms_db": -19.5, "peak_db": -1.1, "problems": []}  # fmt: skip
    assert page._describe(ok) == (
        "sound added: 3 sounds, 92% of 12 cues on an audio onset, rms -19.5 dBFS, peak -1.1 dBFS"
    )
    bed_only = {**ok, "aligned_share": None, "cue_count": 0, "sounds": ["a"]}
    assert "no cue to line up" in page._describe(bed_only)
    failed = {"type": "scored", "ok": False, "problems": ["the sound is too quiet: RMS -45.0 dBFS"]}
    assert (
        page._describe(failed)
        == "sound skipped, the clip stays silent: the sound is too quiet: RMS -45.0 dBFS"
    )
    assert page._describe({"type": "scored", "ok": False, "problems": []}).endswith("no reason given")


def test_the_boards_output_player_is_not_muted_but_the_source_player_is(tmp_path):
    base = tmp_path / "creature-homes"
    home = base / "one"
    (home / "runs").mkdir(parents=True)
    log = ledger.Ledger.start(home, run_id="run1")
    log.record("run_start", fingerprint="f")
    log.record("task", source="reel.mp4", text="Stay curious.")
    log.record("run_end", status="BUILT", clip="attempt-1/clip-sound.mp4")
    folder = home / "runs" / "run1"
    (folder / "attempt-1").mkdir(parents=True)
    (folder / "attempt-1" / "clip-sound.mp4").write_bytes(b"mp4")
    (folder / "reel.mp4").write_bytes(b"mp4")
    board.render([home], base / "board.html")
    data = board.data_path(base / "board.html").read_text(encoding="utf-8")
    html = json.loads(data[len("window.BOARD_DATA = ") : -2])["runs"][0]["html"]
    source = next(v for v in html.split("<video")[1:] if "reel.mp4" in v)
    output = next(v for v in html.split("<video")[1:] if "clip-sound.mp4" in v)
    assert " muted" in source.split(">")[0] and " muted" not in output.split(">")[0]
    assert "controls" in output.split(">")[0]
