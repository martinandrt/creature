import pytest

from creature import criteria, forge, ledger
from creature.criteria import Spec
from creature.forge import Outcome
from creature.ledger import Ledger
from creature.llm import Model

CODE = "def run(input, work):\n    return {}"


@pytest.fixture
def spec() -> Spec:
    output = criteria.clip_format(2)
    return Spec(
        effect="Typewriter reveal",
        slug="typewriter-reveal",
        verdict="try",
        reason="2D text",
        task="Type the text with a caret.",
        text="Stay curious.",
        params={"blinks": 8},
        output=output,
        criteria=("Characters appear one at a time.", "A caret follows the last character."),
        held_out=("SECRET-CRITERION the line stays at the end.",),
        checks=tuple(criteria.checks_for(output)),
    )


@pytest.fixture
def run(home) -> Ledger:
    return Ledger.start(home, run_id="forge")


@pytest.fixture
def model(fake_model, run) -> Model:
    return Model(fake_model, run, budget_usd=1.0, reserve_usd=0.03)


def _script(fake_model, count):
    for n in range(count):
        fake_model.queue("forge", {"code": f"# attempt {n + 1}\n{CODE}", "approach": f"try {n + 1}"})


def test_first_attempt_passes(fake_model, model, run, spec):
    _script(fake_model, 1)
    seen = []
    result = forge.build(
        model, spec, run, attempts=3, cap_usd=0.05, try_code=lambda c, n: seen.append(n) or Outcome(True)
    )
    assert result.ok and result.attempts == 1 and seen == [1] and result.code.startswith("# attempt 1")
    kinds = [e["type"] for e in ledger.read(run.path)]
    assert kinds == ["model_call", "forge_attempt", "attempt_result"]


def test_failure_goes_back_to_the_forge(fake_model, model, run, spec):
    _script(fake_model, 3)
    outcomes = iter(
        [Outcome(False, "ValueError (skill.py line 7)"), Outcome(False, "fps is 25"), Outcome(True)]
    )
    result = forge.build(model, spec, run, attempts=3, cap_usd=0.05, try_code=lambda c, n: next(outcomes))
    assert result.ok and result.attempts == 3
    second, third = fake_model.calls[1].prompt, fake_model.calls[2].prompt
    assert "skill.py line 7" in second and "# attempt 1" in second
    assert "fps is 25" in third and "# attempt 2" in third


def test_the_cap_stops_the_loop_and_names_the_gap(fake_model, model, run, spec):
    _script(fake_model, 5)
    result = forge.build(
        model, spec, run, attempts=2, cap_usd=0.05, try_code=lambda c, n: Outcome(False, f"miss {n}")
    )
    assert not result.ok and result.attempts == 2 and result.gap == "miss 2"
    assert len(fake_model.calls) == 2


@pytest.mark.parametrize("attempts", [0, -1, True, 2.5, None])
def test_bad_attempt_cap_is_refused_before_the_model(fake_model, model, run, spec, attempts):
    with pytest.raises(ValueError):
        forge.build(model, spec, run, attempts=attempts, cap_usd=0.05, try_code=lambda c, n: Outcome(True))
    assert fake_model.calls == []


def test_held_out_criteria_never_reach_the_forge(fake_model, model, run, spec):
    _script(fake_model, 2)
    outcomes = iter([Outcome(False, "a hidden criterion failed"), Outcome(True)])
    forge.build(model, spec, run, attempts=2, cap_usd=0.05, try_code=lambda c, n: next(outcomes))
    for call in fake_model.calls:
        assert "SECRET-CRITERION" not in call.prompt and "SECRET-CRITERION" not in call.system


def test_budget_refusal_ends_the_build_and_keeps_its_history(fake_model, run, spec):
    poor = Model(fake_model, run, budget_usd=0.01, reserve_usd=0.03)
    result = forge.build(
        poor, spec, run, attempts=3, cap_usd=0.05, try_code=lambda c, n: pytest.fail("ran without code")
    )
    assert not result.ok and result.attempts == 0 and result.gap.startswith("budget")


def test_budget_refusal_after_a_failed_attempt_keeps_that_attempt(fake_model, run, spec):
    _script(fake_model, 1)
    model = Model(fake_model, run, budget_usd=0.05, reserve_usd=0.03)
    fake_model.calls.clear()

    def try_code(code, n):
        run.record("note", cost_usd=0.03)  # something else spent the rest
        return Outcome(False, "caret missing")

    result = forge.build(model, spec, run, attempts=3, cap_usd=0.05, try_code=try_code)
    assert (
        not result.ok
        and result.attempts == 1
        and len(result.outcomes) == 1
        and result.gap.startswith("budget")
    )


@pytest.mark.parametrize(
    ("raw", "code"),
    [("```python\nx = 1\n```", "x = 1"), ("```\nx = 1\n```", "x = 1"), ("  x = 1  ", "x = 1")],
)
def test_fences_are_stripped(raw, code):
    assert forge.clean(raw) == code


def test_skill_input_carries_text_params_and_format(spec):
    assert forge.skill_input(spec) == {
        "text": "Stay curious.",
        "params": {"blinks": 8},
        "output": spec.output,
    }


def test_outcome_detail_is_nested_not_spread_into_the_event(fake_model, model, run, spec):
    # detail comes from verdict.py: a cost in it is already in the ledger via Model.ask (never count
    # it twice), and a key that clashes with the event's own fields must not crash a paid attempt
    _script(fake_model, 1)
    detail = {"cost_usd": 0.5, "type": "judge", "attempt": 99}
    result = forge.build(
        model, spec, run, attempts=1, cap_usd=0.05, try_code=lambda c, n: Outcome(True, detail=detail)
    )
    assert result.ok
    [event] = [e for e in ledger.read(run.path) if e["type"] == "attempt_result"]
    assert event["attempt"] == 1 and event["detail"] == detail
    assert run.spent_usd == pytest.approx(0.0018)  # the forge call only


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("```py\nx = 1\n```", "x = 1"),
        ("Here is the skill:\n```python\nx = 1\n```\nHope it helps.", "x = 1"),
    ],
)
def test_py_tag_and_prose_around_the_fence_are_stripped(raw, code):
    # every fence left in the code is a wasted paid attempt and a render
    assert forge.clean(raw) == code
