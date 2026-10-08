import pytest

from creature import criteria
from creature.ledger import Ledger
from creature.llm import Model, ModelError
from creature.perceive import Reel

CRITERIA = [
    "The text area starts empty.",
    "Characters appear one at a time, left to right.",
    "A caret sits after the last character.",
    "The caret blinks while typing pauses.",
    "The full line stays on screen at the end.",
    "Nothing else moves.",
]
REPLY = {
    "effect": "Typewriter reveal",
    "slug": "typewriter-reveal",
    "verdict": "try",
    "reason": "2D text",
    "transcript_is_speech": True,
    "task": "Type the text with a caret.",
    "duration_s": 3.33,
    "params": {"blinks": 8},
    "criteria": CRITERIA,
}


@pytest.fixture
def reel(tmp_path) -> Reel:
    strip = tmp_path / "strip.png"
    strip.write_bytes(b"\x89PNG\r\n\x1a\n")
    times = tuple(round(i + 0.5, 1) for i in range(12))
    return Reel(
        "reel.mp4",
        "Typewriter tutorial",
        "Increase the blinks",
        "",
        tmp_path / "r.mp4",
        strip,
        times,
        12.0,
        720,
        1280,
        30.0,
    )


@pytest.fixture
def model(fake_model, home) -> Model:
    return Model(fake_model, Ledger.start(home, run_id="crit"), budget_usd=1.0, reserve_usd=0.03)


def test_spec_from_the_reel(fake_model, model, reel):
    fake_model.queue("criteria", REPLY)
    spec = criteria.write(model, reel, "Stay curious.", cap_usd=0.05, seed="run-1")
    assert spec.verdict == "try" and spec.slug == "typewriter-reveal" and spec.text == "Stay curious."
    assert spec.output == {
        "file": "clip.mp4",
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "duration_s": 100 / 30,
    }
    assert len(spec.held_out) == 2 and set(spec.criteria) | set(spec.held_out) == set(CRITERIA)
    assert not set(spec.criteria) & set(spec.held_out)
    call = fake_model.calls[0]
    assert call.images[0][1] == reel.strip
    for words in ("Typewriter tutorial", "Increase the blinks", "Stay curious."):
        assert words in call.prompt


def test_held_out_choice_is_repeatable_and_seeded():
    first = criteria.hold_out(CRITERIA, "run-1")
    assert first == criteria.hold_out(CRITERIA, "run-1")
    assert any(criteria.hold_out(CRITERIA, f"run-{i}") != first for i in range(2, 12))


@pytest.mark.parametrize(("count", "hidden"), [(3, 1), (4, 1), (5, 1), (6, 2)])
def test_about_a_third_is_held_out(count, hidden):
    visible, held = criteria.hold_out(CRITERIA[:count], "s")
    assert len(held) == hidden and len(visible) == count - hidden


@pytest.mark.parametrize(
    ("asked", "seconds"),
    [(3.33, 100 / 30), (0.2, 1.0), (60, 8.0), ("x", 3.0), (float("nan"), 3.0), (None, 3.0), (2.5, 2.5)],
)
def test_clip_format_clamps_and_snaps_to_frames(asked, seconds):
    out = criteria.clip_format(asked)
    assert out["duration_s"] == pytest.approx(seconds) and (out["width"], out["height"], out["fps"]) == (
        1080,
        1920,
        30,
    )
    assert round(out["duration_s"] * 30, 6) == round(out["duration_s"] * 30)


def test_checks_follow_the_format():
    out = criteria.clip_format(2)
    kinds = {check["kind"]: check for check in criteria.checks_for(out)}
    assert set(kinds) == {"exists", "video_stream", "resolution", "fps", "duration", "not_black", "max_bytes"}
    assert kinds["duration"]["expect"] == 2.0 and kinds["resolution"]["width"] == 1080
    assert all(check["file"] == "clip.mp4" for check in kinds.values())


def test_empty_text_is_refused_before_the_model(fake_model, model, reel):
    with pytest.raises(ValueError, match="empty"):
        criteria.write(model, reel, "  ", cap_usd=0.05, seed="s")
    assert fake_model.calls == []


def test_too_few_criteria_is_an_error(fake_model, model, reel):
    fake_model.queue("criteria", {**REPLY, "criteria": ["one", " ", "two"]})
    with pytest.raises(ValueError, match="fewer than 3"):
        criteria.write(model, reel, "Stay curious.", cap_usd=0.05, seed="s")


def test_missing_fields_fail_like_any_bad_reply(fake_model, model, reel):
    fake_model.queue("criteria", ModelError("CLI result has no structured_output", cost_usd=0.001))
    with pytest.raises(ModelError):
        criteria.write(model, reel, "Stay curious.", cap_usd=0.05, seed="s")


@pytest.mark.parametrize("slug", ["../evil", "Typewriter Reveal", "a", "x" * 50, "typewriter/reveal"])
def test_slug_is_validated_in_code_not_only_in_the_schema(fake_model, model, reel, slug):
    # the slug becomes a registry directory name: a path from model output is refused here,
    # not left to a schema engine with different regex semantics
    fake_model.queue("criteria", {**REPLY, "slug": slug})
    with pytest.raises(ValueError, match="slug"):
        criteria.write(model, reel, "Stay curious.", cap_usd=0.05, seed="s")


def test_criteria_must_be_a_list_of_strings(fake_model, model, reel):
    # a string iterates as characters and would pass the "at least 3" rule with single letters
    fake_model.queue("criteria", {**REPLY, "criteria": "one long string of criteria text"})
    with pytest.raises(ValueError, match="criteria"):
        criteria.write(model, reel, "Stay curious.", cap_usd=0.05, seed="s")


def test_duplicate_criteria_are_merged(fake_model, model, reel):
    fake_model.queue("criteria", {**REPLY, "criteria": ["Same.", "Same.", "Other.", "Third."]})
    spec = criteria.write(model, reel, "Stay curious.", cap_usd=0.05, seed="s")
    assert sorted(spec.criteria + spec.held_out) == ["Other.", "Same.", "Third."]


def test_transcript_is_speech_reaches_the_spec(fake_model, model, reel):
    # a music-only reel: the forge must know to drop the lyrics, so the flag travels with the spec
    fake_model.queue("criteria", {**REPLY, "transcript_is_speech": False})
    spec = criteria.write(model, reel, "Stay curious.", cap_usd=0.05, seed="s")
    assert spec.transcript_is_speech is False


@pytest.mark.parametrize("verdict", ["ask", "refuse", "skip"])
def test_ask_refuse_skip_survive_untouched(fake_model, model, reel, verdict):
    # the demo shows one ASK and one REFUSE; the spec must carry them exactly as the model said
    fake_model.queue("criteria", {**REPLY, "verdict": verdict})
    assert criteria.write(model, reel, "Stay curious.", cap_usd=0.05, seed="s").verdict == verdict
