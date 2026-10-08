import dataclasses

import pytest

from creature import authority, criteria, verdict, workshop
from creature.criteria import Spec
from creature.ledger import Ledger
from creature.llm import Model
from tests.conftest import REPO

VISIBLE = ("Characters appear one at a time.", "A caret follows the last character.")
HIDDEN = ("SECRET the line stays at the end.",)


def _clip_code(source: str) -> str:
    return (
        "import subprocess\n"
        "def run(input, work):\n"
        f"    source = '{source}'\n"
        "    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', source, '-pix_fmt', 'yuv420p',\n"
        "                    '-c:v', 'libx264', f'{work}/out/clip.mp4'], check=True)\n"
        "    return 'ok'"
    )


@pytest.fixture
def spec() -> Spec:
    output = criteria.clip_format(2)
    return Spec(
        "Typewriter", "typewriter", "try", "", "Type it.", "Hi", {}, output, VISIBLE, HIDDEN,
        tuple(criteria.checks_for(output)),
    )  # fmt: skip


@pytest.fixture
def model(fake_model, home) -> Model:
    return Model(fake_model, Ledger.start(home, run_id="verdict"), budget_usd=1.0, reserve_usd=0.03)


def _strips(tmp_path):
    reel, clip = tmp_path / "reel.png", tmp_path / "clip.png"
    reel.write_bytes(b"\x89PNG\r\n\x1a\n")
    clip.write_bytes(b"\x89PNG\r\n\x1a\n")
    return reel, clip


def _answers(*passes, texts=None):
    texts = texts or list(VISIBLE + HIDDEN)
    return {
        "criteria": [
            {"criterion": t, "pass": p, "evidence": f"seen {i}"}
            for i, (t, p) in enumerate(zip(texts, passes, strict=False))
        ],
        "verdict": "pass",
        "reason": "looks right",
    }


def test_judge_passes_only_when_every_criterion_passes(fake_model, model, spec, tmp_path):
    fake_model.queue("judge", _answers(True, True, True))
    assert verdict.judge(model, spec, *_strips(tmp_path), cap_usd=0.03).ok
    fake_model.queue("judge", _answers(True, True, False))
    judged = verdict.judge(model, spec, *_strips(tmp_path), cap_usd=0.03)
    assert not judged.ok and judged.model_verdict == "pass"  # the model's own verdict is not trusted
    assert [r["hidden"] for r in judged.results] == [False, False, True]


def test_a_missing_answer_counts_as_fail(fake_model, model, spec, tmp_path):
    fake_model.queue("judge", _answers(True, True, texts=list(VISIBLE)))
    judged = verdict.judge(model, spec, *_strips(tmp_path), cap_usd=0.03)
    assert not judged.ok and judged.results[2]["evidence"] == "no answer for this criterion"


def test_reworded_answers_in_the_same_order_still_count(fake_model, model, spec, tmp_path):
    fake_model.queue("judge", _answers(True, True, True, texts=["one", "two", "three"]))
    assert verdict.judge(model, spec, *_strips(tmp_path), cap_usd=0.03).ok


def test_judge_sees_frames_and_criteria_never_code(fake_model, model, spec, tmp_path):
    fake_model.queue("judge", _answers(True, True, True))
    reel, clip = _strips(tmp_path)
    verdict.judge(model, spec, reel, clip, cap_usd=0.03)
    call = fake_model.calls[0]
    assert [path for _, path in call.images] == [reel, clip]
    assert "def run" not in call.prompt and HIDDEN[0] in call.prompt


def test_feedback_never_names_a_hidden_criterion(fake_model, model, spec, tmp_path):
    fake_model.queue("judge", _answers(False, True, False))
    judged = verdict.judge(model, spec, *_strips(tmp_path), cap_usd=0.03)
    text = verdict.feedback(verdict.Checked((), {}, None), judged)
    assert VISIBLE[0] in text and "SECRET" not in text and "1 more criteria" in text


def test_unknown_check_kind_fails_closed():
    assert "unknown check kind" in verdict._evaluate(
        {"kind": "vibes", "file": "clip.mp4"}, b"x", {"readable": True}
    )


def test_feedback_scrubs_hidden_text_quoted_in_visible_evidence(fake_model, model, spec, tmp_path):
    # the judge sees every criterion and may quote a hidden one inside another's evidence;
    # that quote must not ride into the forge prompt
    answers = _answers(False, True, True)
    answers["criteria"][0]["evidence"] = f"the caret is missing, unlike '{HIDDEN[0]}' which holds"
    fake_model.queue("judge", answers)
    judged = verdict.judge(model, spec, *_strips(tmp_path), cap_usd=0.03)
    text = verdict.feedback(verdict.Checked((), {}, None), judged)
    assert VISIBLE[0] in text and "SECRET" not in text


def test_frames_are_checked_exactly_for_composition():
    # Martin's rule: an effect returns exactly round(duration_s * fps) frames, so clips concatenate
    # without drift. duration ± 0.1 s lets a 3-frame drift through; the frame count must be exact.
    output = criteria.clip_format(2)
    [frames] = [c for c in criteria.checks_for(output) if c["kind"] == "frames"]
    assert frames["expect"] == 60
    probe = {"readable": True, "video": True, "frames": 60}
    assert verdict._evaluate(frames, b"x", probe) is None
    assert verdict._evaluate(frames, b"x", {**probe, "frames": 59})


@pytest.mark.docker
class TestChecks:
    @pytest.fixture(scope="class")
    def env(self):
        limits = dataclasses.replace(authority.load(REPO).workshop, timeout_s=60)
        return limits, workshop.ensure_image(REPO / "workshop")

    def _check(self, env, spec, tmp_path, source):
        limits, image = env
        made = workshop.run(_clip_code(source), {}, limits=limits, image=image)
        return verdict.check(made.outputs, spec.checks, limits=limits, image=image, folder=tmp_path)

    def test_a_clip_in_the_declared_format_passes(self, env, spec, tmp_path):
        checked = self._check(env, spec, tmp_path, "testsrc2=s=1080x1920:r=30:d=2")
        assert checked.problems == () and checked.strip.startswith(b"\x89PNG")
        assert checked.probe["frames"] == 60 and checked.probe["times"][0] == 0.0
        assert checked.probe["times"][-1] == pytest.approx(59 / 30, abs=1e-3)

    def test_wrong_format_is_named(self, env, spec, tmp_path):
        checked = self._check(env, spec, tmp_path, "testsrc2=s=720x1280:r=25:d=3")
        kinds = {problem.split(":")[0] for problem in checked.problems}
        assert kinds == {"resolution", "fps", "duration", "frames"}

    def test_black_clip_fails(self, env, spec, tmp_path):
        checked = self._check(env, spec, tmp_path, "color=c=black:s=1080x1920:r=30:d=2")
        assert [p.split(":")[0] for p in checked.problems] == ["not_black"]

    def test_missing_clip_fails_without_running_anything(self, env, spec, tmp_path):
        limits, image = env
        checked = verdict.check({}, spec.checks, limits=limits, image=image, folder=tmp_path)
        assert checked.problems == ("exists: clip.mp4 was not written",)

    def test_dark_clip_with_small_bright_content_is_not_black(self, env, spec, tmp_path):
        # motion design's default look: a little white on black. A 200x200 white box on 1080x1920
        # is 1.9 % of the frame, mean luma ~5 — "black" by mean, plainly visible to a judge
        source = "color=c=black:s=1080x1920:r=30:d=2,drawbox=x=440:y=860:w=200:h=200:color=white:t=fill"
        checked = self._check(env, spec, tmp_path, source)
        assert checked.problems == ()


@pytest.mark.docker
def test_a_stream_that_stops_decoding_is_a_finding_not_a_traceback(tmp_path):
    # two parts of different formats joined with -c copy: the second half does not decode. The probe
    # must say what it found (frames decoded vs. counted), not fall over a frame file ffmpeg never wrote
    from creature import loop

    limits = dataclasses.replace(authority.load(REPO).workshop, timeout_s=90)
    image = workshop.ensure_image(REPO / "workshop")
    parts = {}
    for name, source in (
        ("part00.mp4", "testsrc2=s=1080x1920:r=30:d=3"),
        ("part01.mp4", "testsrc2=s=720x1280:r=25:d=3"),
    ):
        made = workshop.run(_clip_code(source), {}, limits=limits, image=image)
        (tmp_path / name).write_bytes(made.outputs["clip.mp4"])
        parts[name] = tmp_path / name
    joined = workshop.run(loop.JOIN_CODE, {"parts": sorted(parts)}, parts, limits=limits, image=image)
    assert joined.ok and loop.JOINED in joined.outputs  # concat itself does not notice
    output = {**criteria.clip_format(5.5), "file": loop.JOINED}  # 90 + 75 frames declared
    checked = verdict.check(
        joined.outputs, tuple(criteria.checks_for(output)), limits=limits, image=image, folder=tmp_path
    )
    assert checked.problems
    assert not any("Error" in problem for problem in checked.problems), checked.problems
    assert checked.probe.get("readable") and checked.probe.get("frames")  # ffprobe did read the file


MOVE_CODE = r"""
import subprocess
from PIL import Image, ImageDraw
def run(input, work):
    W, H = 1080, 1920
    if input["stutter"]:
        xs = [100 + 20 * i for i in range(30)]  # linear, 20 px a frame
        xs[15] = xs[14]  # one frame shown twice
    else:
        xs = [int(round(100 + (1 - (1 - min(1.0, i / 12)) ** 3) * 580)) for i in range(30)]  # ease-out, hold
    for i, x in enumerate(xs):
        img = Image.new("RGB", (W, H), (0, 0, 0))
        ImageDraw.Draw(img).rectangle((x, 760, x + 400, 1160), fill=(255, 255, 255))
        img.save(f"{work}/f_{i:03d}.png")
    subprocess.run(["ffmpeg", "-v", "error", "-r", "30", "-i", f"{work}/f_%03d.png", "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", f"{work}/out/clip.mp4"], check=True)
    return xs
"""


@pytest.mark.docker
def test_smooth_passes_an_eased_move_and_fails_a_frame_shown_twice(tmp_path):
    # motion design eases: a cubic ease-out over 12 frames is smooth motion and must pass. A frame shown
    # twice (the stutter the check is for) must fail: its 0 step is the defect, not a still frame
    limits = dataclasses.replace(authority.load(REPO).workshop, timeout_s=90)
    image = workshop.ensure_image(REPO / "workshop")
    output = criteria.clip_format(1.0)
    smooth = {"kind": "smooth", "file": "clip.mp4", "cuts": [0], "max_ratio": 1.5}

    def check(stutter):
        made = workshop.run(MOVE_CODE, {"stutter": stutter}, limits=limits, image=image)
        assert made.ok, made.error
        folder = tmp_path / ("stutter" if stutter else "eased")
        folder.mkdir()
        checks = (*criteria.checks_for(output), smooth)
        return verdict.check(made.outputs, checks, limits=limits, image=image, folder=folder)

    eased = check(False)
    assert eased.problems == (), eased.problems
    held = check(True)
    assert any(p.startswith("smooth:") for p in held.problems), held.problems
