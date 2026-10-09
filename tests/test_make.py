# ruff: noqa: F811  (world is a fixture from test_loop, used as an argument)
"""creature make: a script, one line per shot, composed by one cheap call over skills with style cards,
judged once with fixed criteria, recomposed at most twice. Offline: model, workshop and checks faked."""

import json

import pytest

from creature import card, ledger, loop, make, registry, workshop
from tests.fakes import FakeModel
from tests.test_loop import PNG, RENDER, _build, _creature, _kinds, world  # noqa: F401 (world is a fixture)

CARD_VALUE = {
    "background": "#1b2135",
    "luma": 0.12,
    "colors": [{"hex": "#1b2135", "share": 0.7}, {"hex": "#d4a574", "share": 0.1}],
    "text_share": 0.18,
    "moving_share": 0.6,
}
LINES = ["Frankenstein.", "A creature that learns what it lacks."]


@pytest.fixture
def made(world, fake_model, monkeypatch):
    """A home with one learned skill, and a workshop that renders, measures cards and joins."""
    _build(world, fake_model)
    calls = {"renders": [], "cards": 0, "joins": 0}

    def run(code, input, files=None, **kwargs):
        if code == card.CARD_CODE:
            calls["cards"] += 1
            return workshop.WorkshopResult(True, CARD_VALUE, {}, None, None, 0.1, "")
        if code == loop.JOIN_CODE:
            calls["joins"] += 1
            return workshop.WorkshopResult(True, {"parts": 2}, {loop.JOINED: b"joined"}, None, None, 0.1, "")
        calls["renders"].append(input)
        return RENDER

    monkeypatch.setattr(loop.workshop, "run", run)
    monkeypatch.setattr(card.workshop, "run", run)
    return calls


def _choice(*skills):
    return {
        "shots": [
            {"line": n, "skill": s, "why": "dark, like its neighbours"} for n, s in enumerate(skills, 1)
        ],
        "reason": "one dark family",
    }


def _judged(ok=True, covered=False, one_piece=True):
    return {
        "lines": [
            {"line": n, "readable": ok, "nothing_over_it": not covered, "why": "seen"}
            for n in range(1, len(LINES) + 1)
        ],
        "one_piece": one_piece,
        "one_piece_why": "same navy and amber",
        "feedback": "" if ok and not covered and one_piece else "line 2 is cut off",
    }


def test_make_composes_each_line_at_its_full_length_and_saves_a_design(world, made, monkeypatch):
    fake = FakeModel()
    fake.queue("compose", _choice("typewriter-reveal", "typewriter-reveal"))
    fake.queue("judge", _judged())
    creature = _creature(world, fake)
    report = creature.make(LINES, style={"colors": {"night": "#1B2135", "amber": "#D4A574"}})
    assert report.status == "DONE", report.gap
    assert [c.step for c in fake.calls] == [
        "compose",
        "judge",
    ]  # no forge, no criteria: nothing new is learned
    fake.assert_drained()
    renders = [r for r in made["renders"] if r["text"] in LINES]
    assert [r["text"] for r in renders] == LINES  # each line is its own shot's text
    assert all(r["output"]["duration_s"] == 3.0 for r in renders)  # the skill's whole learned length
    assert all(r["params"]["palette"] == ["#1B2135", "#D4A574"] for r in renders)  # the user's style wins
    assert made["joins"] == 1 and report.cues == [{"frame": 0, "kind": "cut"}, {"frame": 90, "kind": "cut"}]
    events = ledger.read(creature.ledger.path)
    [choice] = [e for e in events if e["type"] == "make_choice"]
    assert [s["skill"] for s in choice["shots"]] == ["typewriter-reveal"] * 2 and choice["reason"]
    [judged] = [e for e in events if e["type"] == "make_judged"]
    assert judged["ok"] is True and [r["line"] for r in judged["lines"]] == [1, 2]
    design = registry.design(world / "registry", "make")
    assert design["origin"]["composed_by"] == "make" and design["origin"]["lines"] == LINES
    assert [s["skill"] for s in design["steps"]] == ["typewriter-reveal"] * 2
    assert report.fingerprint_same


def test_the_style_card_is_measured_once_and_written_through_the_registry(world, made):
    fake = FakeModel().queue("compose", _choice("typewriter-reveal", "typewriter-reveal"))
    fake.queue("judge", _judged())
    creature = _creature(world, fake)
    creature.make(LINES)
    skill = registry.get(world / "registry", "typewriter-reveal")  # the seal still holds
    have = registry.card(skill)
    assert have["tone"] == "dark" and have["seconds"] == 3.0 and have["orientation"] == "portrait"
    assert have["text_share"] == 0.18 and [c["hex"] for c in have["colors"]] == ["#1b2135", "#d4a574"]
    assert made["cards"] == 1
    [event] = [e for e in ledger.read(creature.ledger.path) if e["type"] == "card"]
    assert event["skill"] == "typewriter-reveal" and event["background"] == "#1b2135"
    prompt = fake.calls[0].prompt
    assert "background #1b2135 (dark" in prompt and "text takes 18% of the frame" in prompt
    second = FakeModel().queue("compose", _choice("typewriter-reveal", "typewriter-reveal"))
    second.queue("judge", _judged())
    _creature(world, second).make(LINES)
    assert made["cards"] == 1  # a measured card is read, not measured again


def test_an_unknown_name_is_dropped_and_the_line_is_composed_again(world, made):
    fake = FakeModel()
    fake.queue("compose", _choice("typewriter-reveal", "no-such-skill"))
    fake.queue("compose", _choice("typewriter-reveal", "typewriter-reveal"))
    fake.queue("judge", _judged())
    creature = _creature(world, fake)
    report = creature.make(LINES)
    assert report.status == "DONE"
    first, _ = [e for e in ledger.read(creature.ledger.path) if e["type"] == "make_choice"]
    assert first["unknown"] == ["no-such-skill"] and first["shots"][1]["skill"] is None
    assert "lines [2] got no skill" in fake.calls[1].prompt


def test_a_failing_judge_recomposes_at_most_twice_then_fails(world, made):
    fake = FakeModel()
    for _ in range(3):
        fake.queue("compose", _choice("typewriter-reveal", "typewriter-reveal"))
        fake.queue("judge", _judged(covered=True))
    creature = _creature(world, fake)
    report = creature.make(LINES)
    assert report.status == "FAILED" and "after 3 compositions" in report.gap
    assert [c.step for c in fake.calls] == ["compose", "judge"] * 3
    assert "line 2 is cut off" in fake.calls[2].prompt  # the judge's feedback reaches the next composition
    assert (
        len([r for r in made["renders"] if r["text"] in LINES]) == 2
    )  # a shot already made is not rendered again
    assert registry.designs(world / "registry") == ["typewriter-reveal"]  # nothing saved
    assert "make_judged" in _kinds(creature)


def test_an_empty_script_calls_nothing(world, made):
    fake = FakeModel()
    report = _creature(world, fake).make(["", "  "])
    assert report.status == "FAILED" and report.gap == "the script has no lines" and fake.calls == []


def test_the_judge_verdict_comes_from_the_fixed_criteria(tmp_path):
    strip = tmp_path / "s.png"
    strip.write_bytes(PNG)

    class Asks:
        def __init__(self, data):
            self.data = data

        def ask(self, *args, **kwargs):
            self.kwargs = kwargs
            return self.data

    spans = make.spans([0, 90], 180)
    assert spans == [(0.0, 3.0), (3.0, 6.0)]
    model = Asks(_judged())
    assert make.judge(model, LINES, spans, [strip, strip], cap_usd=0.03)["ok"] is True
    assert [label for label, _ in model.kwargs["images"]] == [
        f"line {n}: {t!r}" for n, t in enumerate(LINES, 1)
    ]
    assert make.judge(Asks(_judged(covered=True)), LINES, spans, [strip, strip], cap_usd=0.03)["ok"] is False
    assert (
        make.judge(Asks(_judged(one_piece=False)), LINES, spans, [strip, strip], cap_usd=0.03)["ok"] is False
    )
    missing = {**_judged(), "lines": [{"line": 1, "readable": True, "nothing_over_it": True, "why": "ok"}]}
    result = make.judge(Asks(missing), LINES, spans, [strip, strip], cap_usd=0.03)
    assert result["ok"] is False and result["lines"][1]["why"] == "no answer for this line"
    assert (
        make.judge(Asks(_judged()), LINES, spans, [strip, None], cap_usd=0.03)["ok"] is False
    )  # a shot unseen


def test_shots_keep_known_names_in_range_and_the_first_answer_per_line(world, made):
    skill = registry.get(world / "registry", "typewriter-reveal")
    catalog = [(skill, {})]
    choice = {
        "shots": [
            {"line": 1, "skill": "typewriter-reveal", "why": "a"},
            {"line": 1, "skill": "other", "why": "b"},
            {"line": 9, "skill": "typewriter-reveal", "why": "c"},
            {"line": "x", "skill": "typewriter-reveal", "why": "d"},
        ]
    }
    picked = make.shots(choice, LINES, catalog)
    assert picked[0] == (skill, "a") and picked[1] == (None, "")


def test_card_line_and_tone():
    assert card.tone(0.1) == "dark" and card.tone(0.5) == "mid" and card.tone(0.9) == "light"
    assert not card.without_error({"error": "x"}) and not card.without_error(None)


def test_save_card_refuses_an_unknown_skill(world, made):
    with pytest.raises(KeyError):
        registry.save_card(world / "registry", "nope", 1, {"card": 1})
    path = registry.get(world / "registry", "typewriter-reveal").path / registry.CARD
    registry.save_card(world / "registry", "typewriter-reveal", 1, {"card": 1, "tone": "dark"})
    assert json.loads(path.read_text())["tone"] == "dark"
