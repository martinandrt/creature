import json
import math
from importlib import resources

import pytest

from creature import ledger
from creature.ledger import GENESIS, Ledger


def _lines(log: Ledger) -> list[str]:
    return log.path.read_text().splitlines()


def _rewrite(log: Ledger, lines: list[str]) -> None:
    log.path.write_text("".join(line + "\n" for line in lines))


@pytest.fixture
def log(home) -> Ledger:
    run = Ledger.start(home, run_id="test-run")
    run.record("run_start", fingerprint="abc")
    run.record("model_call", step="forge", cost_usd=0.0018)
    run.record("run_end", ok=True)
    return run


def test_start_creates_file_under_runs(home):
    run = Ledger.start(home)
    assert run.path.parent == home / "runs" and run.path.exists()
    assert run.run_id == run.path.stem


def test_run_id_is_never_reused(home, log):
    with pytest.raises(FileExistsError):
        Ledger.start(home, run_id="test-run")


def test_events_are_chained(log):
    events = ledger.read(log.path)
    assert [e["seq"] for e in events] == [0, 1, 2]
    assert events[0]["prev_hash"] == GENESIS
    assert events[1]["prev_hash"] == events[0]["hash"] and events[2]["prev_hash"] == events[1]["hash"]
    assert all(e["run"] == "test-run" for e in events)
    assert ledger.verify(log.path) is None


def test_edited_line_is_detected(log):
    lines = _lines(log)
    event = json.loads(lines[1])
    event["cost_usd"] = 0.0
    lines[1] = json.dumps(event)
    _rewrite(log, lines)
    assert ledger.verify(log.path).startswith("line 2:")


def test_removed_line_is_detected(log):
    lines = _lines(log)
    _rewrite(log, [lines[0], lines[2]])
    assert ledger.verify(log.path).startswith("line 2:")


def test_reordered_lines_are_detected(log):
    lines = _lines(log)
    _rewrite(log, [lines[1], lines[0], lines[2]])
    assert ledger.verify(log.path).startswith("line 1:")


def test_rehashed_edit_still_breaks_the_next_link(log):
    # an editor who recomputes the edited line's own hash still breaks prev_hash of the line after it
    lines = _lines(log)
    event = json.loads(lines[1])
    event["cost_usd"] = 0.0
    event["hash"] = ledger._digest(event)
    lines[1] = json.dumps(event)
    _rewrite(log, lines)
    assert ledger.verify(log.path).startswith("line 3:")


def test_garbage_line_is_reported(log):
    _rewrite(log, [*_lines(log), "{not json"])
    assert ledger.verify(log.path) == "line 4: not JSON"


def test_spent_sums_costs_and_survives_reopen(log):
    log.record("model_call", step="examiner", cost_usd=0.0042)
    assert math.isclose(log.spent_usd, 0.006)
    reopened = Ledger(log.path)
    assert math.isclose(reopened.spent_usd, 0.006)
    event = reopened.record("note")
    assert event["seq"] == 4 and ledger.verify(log.path) is None


def test_reopening_a_broken_ledger_fails(log):
    lines = _lines(log)
    _rewrite(log, [lines[0], lines[2]])
    with pytest.raises(ValueError, match="broken"):
        Ledger(log.path)


@pytest.mark.parametrize("key", sorted(ledger.RESERVED))
def test_reserved_keys_are_refused(log, key):
    before = _lines(log)
    with pytest.raises(ValueError, match="reserved"):
        log.record("note", **{key: "x"})
    assert _lines(log) == before


@pytest.mark.parametrize("cost", [-0.01, float("nan"), float("inf"), True, "0.1", None])
def test_bad_costs_are_refused(log, cost):
    before = _lines(log)
    with pytest.raises(ValueError, match="cost_usd"):
        log.record("model_call", cost_usd=cost)
    assert _lines(log) == before


@pytest.mark.parametrize("bad", ["", "Run", "run-start", "1st", "run start"])
def test_bad_event_types_are_refused(log, bad):
    with pytest.raises(ValueError, match="event type"):
        log.record(bad)


def test_non_json_data_writes_nothing(log):
    before = _lines(log)
    with pytest.raises(TypeError):
        log.record("note", value={1, 2})
    assert _lines(log) == before
    assert log.record("note")["seq"] == 3


def test_events_have_the_schema_fields(log):
    schema = json.loads(resources.files("creature").joinpath("schemas/ledger_event.json").read_text())
    for event in ledger.read(log.path):
        assert set(schema["required"]) <= event.keys()
