import json
import math
import subprocess
from pathlib import Path

import pytest

from creature import ledger, llm
from creature.ledger import Ledger
from creature.llm import BudgetRefused, ClaudeCLI, Model, ModelCall, ModelError

SCHEMA = {"type": "object", "properties": {"words": {"type": "integer"}}, "required": ["words"]}


@pytest.fixture
def run(home) -> Ledger:
    return Ledger.start(home, run_id="llm")


def _model(fake_model, run, budget=1.0) -> Model:
    return Model(fake_model, run, budget_usd=budget)


def _events(run, kind):
    return [e for e in ledger.read(run.path) if e["type"] == kind]


def test_answer_is_returned_and_logged(fake_model, run):
    fake_model.queue("planner", {"words": 5}, cost_usd=0.0004)
    assert _model(fake_model, run).ask("planner", "sys", "count", SCHEMA, cap_usd=0.05) == {"words": 5}
    [event] = _events(run, "model_call")
    assert event["step"] == "planner" and event["ok"] and event["cost_usd"] == 0.0004
    assert math.isclose(run.spent_usd, 0.0004)


def test_call_cap_is_the_smaller_of_step_cap_and_budget_left(fake_model, run):
    fake_model.queue("forge", {"words": 1}).queue("forge", {"words": 1})
    model = _model(fake_model, run, budget=0.1)
    model.ask("forge", "s", "p", SCHEMA, cap_usd=0.05)
    run.record("note", cost_usd=0.06)
    model.ask("forge", "s", "p", SCHEMA, cap_usd=0.05)
    assert fake_model.calls[0].max_usd == 0.05
    assert math.isclose(fake_model.calls[1].max_usd, 0.1 - 0.06 - 0.0018)


def test_refused_below_reserve_without_reaching_the_model(fake_model, run):
    model = _model(fake_model, run, budget=0.02)
    with pytest.raises(BudgetRefused):
        model.ask("forge", "s", "p", SCHEMA, cap_usd=0.05)
    assert fake_model.calls == []
    [event] = _events(run, "model_refused")
    assert event["reason"] == "budget" and event["step"] == "forge"


def test_budget_runs_out_across_calls(fake_model, run):
    for _ in range(5):
        fake_model.queue("forge", {"words": 1}, cost_usd=0.02)
    model = _model(fake_model, run, budget=0.1)
    done = 0
    with pytest.raises(BudgetRefused):
        for _ in range(5):
            model.ask("forge", "s", "p", SCHEMA, cap_usd=0.05)
            done += 1
    assert done == 4  # 0.08 spent, 0.02 left < 0.03 reserve
    assert run.spent_usd <= 0.1


def test_failed_call_cost_is_counted(fake_model, run):
    fake_model.queue("forge", ModelError("CLI error: error_max_budget_usd", cost_usd=0.03))
    with pytest.raises(ModelError):
        _model(fake_model, run).ask("forge", "s", "p", SCHEMA, cap_usd=0.05)
    [event] = _events(run, "model_call")
    assert not event["ok"] and event["cost_usd"] == 0.03 and "max_budget" in event["error"]
    assert math.isclose(run.spent_usd, 0.03)


def _cli_result(**over):
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "total_cost_usd": 0.0004341,
        "structured_output": {"words": 5},
        "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1276, "output_tokens": 121},
        "modelUsage": {"claude-haiku-5-5": {"outputTokens": 121}},
    }
    return json.dumps({**result, **over})


CALL = ModelCall("planner", llm.MODEL, "sys", "--prompt", SCHEMA, max_usd=0.05)


def test_parse_success():
    reply = llm.parse(_cli_result(), CALL)
    assert reply.data == {"words": 5} and reply.cost_usd == 0.0004341
    assert reply.model == "claude-haiku-5-5" and reply.input_tokens == 1278 and reply.output_tokens == 121


def test_parse_names_the_model_that_answered():
    usage = {"claude-haiku-5-5": {"outputTokens": 30}, llm.FALLBACK_MODEL: {"outputTokens": 400}}
    assert llm.parse(_cli_result(modelUsage=usage), CALL).model == llm.FALLBACK_MODEL


def test_parse_over_budget_raises_with_cost():
    stdout = _cli_result(subtype="error_max_budget_usd", is_error=True, structured_output=None)
    with pytest.raises(ModelError, match="error_max_budget_usd") as caught:
        llm.parse(stdout, CALL)
    assert caught.value.cost_usd == 0.0004341


@pytest.mark.parametrize("stdout", ["", "Not logged in", "[]", _cli_result(structured_output="five")])
def test_parse_rejects_bad_output(stdout):
    with pytest.raises(ModelError):
        llm.parse(stdout, CALL)


def test_command_is_isolated_and_keeps_the_prompt_out_of_argv():
    command = ClaudeCLI(binary="claude").command(CALL)
    for flag in ("--strict-mcp-config", "--system-prompt", "--json-schema", "--fallback-model"):
        assert flag in command
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--setting-sources") + 1] == ""
    assert command[command.index("--max-budget-usd") + 1] == "0.050000"
    assert "--prompt" not in command


def test_child_gets_no_secrets_and_an_empty_directory(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "x")
    monkeypatch.setenv("CREATURE_SECRETS", "/secret")
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs)
        seen["files"] = list(Path(kwargs["cwd"]).iterdir())
        return subprocess.CompletedProcess(command, 0, _cli_result(), "")

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    ClaudeCLI(binary="claude").complete(CALL)
    assert "APIFY_TOKEN" not in seen["env"] and "CREATURE_SECRETS" not in seen["env"]
    assert seen["files"] == [] and seen["input"] == "--prompt"


def test_timeout_counts_the_whole_cap(monkeypatch):
    def slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(llm.subprocess, "run", slow)
    with pytest.raises(ModelError) as caught:
        ClaudeCLI(binary="claude", timeout_s=1).complete(CALL)
    assert caught.value.cost_usd == CALL.max_usd


@pytest.mark.slow
def test_live_call_on_haiku_5_5(run):
    model = Model(ClaudeCLI(), run, budget_usd=0.2)
    prompt = "Count the words: the quick brown fox jumps"
    answer = model.ask("planner", "You are terse.", prompt, SCHEMA, cap_usd=0.05)
    assert answer == {"words": 5}
    [event] = _events(run, "model_call")
    assert event["ok"] and 0 < event["cost_usd"] < 0.05
