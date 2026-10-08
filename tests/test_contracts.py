"""Contracts between modules: what the spine writes must match the JSON Schemas it hands around.

The same schema files go to the model (--json-schema) and to these tests, so a drift between
code and schema fails here instead of in a live run. Add a case per schema as they land
(capability.json, planner output, examiner tests).
"""

import json
from importlib import resources

import pytest
from jsonschema import Draft202012Validator

from creature import criteria, ledger
from creature.ledger import Ledger

SCHEMAS = resources.files("creature") / "schemas"
SCHEMA_NAMES = sorted(p.name for p in SCHEMAS.iterdir() if p.name.endswith(".json"))


def validator(name: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def assert_valid(name: str, instance: dict, label: str) -> None:
    errors = sorted(validator(name).iter_errors(instance), key=lambda e: list(e.path))
    assert not errors, f"{label}: " + "; ".join(e.message for e in errors)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schema_itself_is_valid_draft_2020_12(name):
    validator(name)


def test_every_recorded_ledger_event_matches_the_schema(home):
    run = Ledger.start(home, run_id="contract")
    run.record("run_start", fingerprint="abc")
    run.record("model_call", step="forge", cost_usd=0.0018, model="claude-haiku-5-5")
    run.record("note", text="diakritika: příliš žluťoučký kůň")
    run.record("run_end", ok=True)
    for event in ledger.read(run.path):
        assert_valid("ledger_event.json", event, f"seq {event['seq']} ({event['type']})")


def test_schema_rejects_what_record_rejects():
    # the schema must be at least as strict as the code on the fields both know about.
    # Not covered here: a trailing newline in "type". Python's re (used by jsonschema) lets `$`
    # match before a final "\n", ECMA (the CLI) does not, and there is no portable `\Z`.
    # The code's re.fullmatch is the enforcer for that edge; see test_ledger.
    base = {
        "seq": 0,
        "ts": "t",
        "run": "r",
        "type": "ok",
        "prev_hash": ledger.GENESIS,
        "hash": ledger.GENESIS,
    }
    assert not list(validator("ledger_event.json").iter_errors(base))
    for bad in (
        {**base, "type": "Run"},
        {**base, "cost_usd": -0.01},
        {**base, "hash": "abc"},
        {**base, "seq": -1},
    ):
        assert list(validator("ledger_event.json").iter_errors(bad)), bad


def test_criteria_schema_is_valid_and_strict():
    # the schema the model is given must be a valid draft and at least as strict as the code
    Draft202012Validator.check_schema(criteria.SCHEMA)
    v = Draft202012Validator(criteria.SCHEMA)
    good = {
        "effect": "Typewriter",
        "slug": "typewriter-reveal",
        "verdict": "try",
        "reason": "2D text",
        "transcript_is_speech": True,
        "task": "Type it.",
        "duration_s": 3.0,
        "params": [{"name": "blinks", "value": 8, "quote": "blink it eight times"}],
        "criteria": ["a", "b", "c"],
    }
    assert not list(v.iter_errors(good))
    for bad in (
        {**good, "slug": "../evil"},
        {**good, "slug": "Typewriter Reveal"},
        {**good, "verdict": "maybe"},
        {**good, "criteria": ["a"]},
        {**good, "criteria": "a b c"},
        {**good, "transcript_is_speech": "yes"},
        {**good, "params": {"blinks": 8}},  # params carry their transcript quote: a list, not a map
        {**good, "params": [{"name": "blinks", "value": 8}]},
    ):
        assert list(v.iter_errors(bad)), bad
