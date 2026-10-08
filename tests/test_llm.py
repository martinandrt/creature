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


@pytest.mark.parametrize(
    ("budget", "reserve"),
    [(float("nan"), 0.03), (float("inf"), 0.03), (-1.0, 0.03), (1.0, 0.0), (1.0, -1.0), (1.0, float("nan"))],
)
def test_bad_budget_or_reserve_is_refused_at_construction(fake_model, run, budget, reserve):
    # NaN compares False everywhere, so a NaN budget never refuses; inf is no budget at all;
    # a reserve <= 0 lets the run overshoot by |reserve| (measured: $0.16 spent on a $0.10 budget)
    with pytest.raises(ValueError):
        Model(fake_model, run, budget_usd=budget, reserve_usd=reserve)


@pytest.mark.parametrize("cap", [0.0, -0.01, float("nan"), float("inf")])
def test_bad_cap_is_refused_before_the_model_is_reached(fake_model, run, cap):
    # today a NaN cap reaches the transport (paid), then the ledger write raises: the call is lost
    fake_model.queue("forge", {"words": 1})
    with pytest.raises(ValueError):
        _model(fake_model, run).ask("forge", "s", "p", SCHEMA, cap_usd=cap)
    assert fake_model.calls == []
    assert _events(run, "model_call") == []


def test_calls_within_the_reserve_never_exceed_the_budget(fake_model, run):
    # the reserve is sized to one large fallback-priced call. While every call costs at most
    # the reserve, the budget is a hard ceiling. Powers of two keep the float arithmetic exact.
    budget, reserve = 0.125, 0.03125
    for _ in range(10):
        fake_model.queue("forge", {"words": 1}, cost_usd=reserve)
    model = Model(fake_model, run, budget_usd=budget, reserve_usd=reserve)
    with pytest.raises(BudgetRefused):
        for _ in range(10):
            model.ask("forge", "s", "p", SCHEMA, cap_usd=0.05)
    assert len(fake_model.calls) == 4
    assert run.spent_usd <= budget


def test_overshoot_is_bounded_by_one_call_above_the_reserve_then_refused(fake_model, run):
    # --max-budget-usd is checked after the turn, so one call can cost more than its cap;
    # the damage is bounded by (that call's cost - reserve) and the very next call is refused
    budget, reserve, big = 0.05, 0.03, 0.08
    fake_model.queue("forge", {"words": 1}, cost_usd=0.0018).queue("forge", {"words": 1}, cost_usd=big)
    fake_model.queue("forge", {"words": 1})
    model = Model(fake_model, run, budget_usd=budget, reserve_usd=reserve)
    model.ask("forge", "s", "p", SCHEMA, cap_usd=0.05)
    model.ask("forge", "s", "p", SCHEMA, cap_usd=0.05)  # 0.0482 left >= reserve, so it proceeds
    with pytest.raises(BudgetRefused):
        model.ask("forge", "s", "p", SCHEMA, cap_usd=0.05)
    assert len(fake_model.calls) == 2
    assert run.spent_usd - budget <= big - reserve


def test_reply_missing_required_keys_is_an_error_but_still_costs(run):
    # the fake checks required keys; production must be at least as strict as the fake
    class Loose:
        def complete(self, call):
            return llm.ModelReply(data={"other": 1}, cost_usd=0.001)

    with pytest.raises(ModelError):
        Model(Loose(), run, budget_usd=1.0).ask("planner", "s", "p", SCHEMA, cap_usd=0.05)
    [event] = _events(run, "model_call")
    assert event["cost_usd"] == 0.001


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
    assert seen["files"] == []
    assert json.loads(seen["input"])["message"]["content"] == [{"type": "text", "text": "--prompt"}]


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


def test_reply_with_missing_keys_is_logged_as_failed(run):
    class Loose:
        def complete(self, call):
            return llm.ModelReply(data={"other": 1}, cost_usd=0.001)

    with pytest.raises(ModelError):
        Model(Loose(), run, budget_usd=1.0).ask("planner", "s", "p", SCHEMA, cap_usd=0.05)
    [event] = _events(run, "model_call")
    assert event["ok"] is False and "words" in event["error"]


@pytest.mark.parametrize(
    ("usage", "model_usage"),
    [({"input_tokens": "x", "output_tokens": None}, {"m": 5}), ("bad", ["bad"]), ({"output_tokens": -3}, {})],
)
def test_parse_survives_garbage_bookkeeping(usage, model_usage):
    # the call is paid for by now: bad token counts must not lose the reply or its cost
    reply = llm.parse(_cli_result(usage=usage, modelUsage=model_usage), CALL)
    assert reply.data == {"words": 5} and reply.cost_usd == 0.0004341
    assert reply.model == CALL.model and reply.input_tokens >= 0 and reply.output_tokens == 0


def _png(path: Path, rgb=(255, 0, 0), size=8) -> Path:
    # a solid-colour PNG from the standard library, so tests need no imaging package
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )
    return path


def test_message_puts_each_image_after_its_label(tmp_path):
    reel, out = _png(tmp_path / "reel.png"), _png(tmp_path / "out.jpg")
    call = ModelCall(
        "judge", llm.MODEL, "sys", "criteria", SCHEMA, 0.05, images=(("reel", reel), ("output", out))
    )
    content = json.loads(llm.message(call))["message"]["content"]
    assert [block["type"] for block in content] == ["text", "image", "text", "image", "text"]
    assert (
        content[0]["text"] == "reel:" and content[2]["text"] == "output:" and content[4]["text"] == "criteria"
    )
    assert (
        content[1]["source"]["media_type"] == "image/png"
        and content[3]["source"]["media_type"] == "image/jpeg"
    )


def test_command_uses_stream_json_both_ways():
    command = ClaudeCLI(binary="claude").command(CALL)
    assert command[command.index("--input-format") + 1] == "stream-json"
    assert command[command.index("--output-format") + 1] == "stream-json" and "--verbose" in command


def test_parse_takes_the_last_result_line_of_the_stream():
    stream = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init"}),
            "not json",
            _cli_result(),
            json.dumps({"type": "x"}),
        ]
    )
    assert llm.parse(stream, CALL).data == {"words": 5}


@pytest.mark.parametrize("name", ["missing.png", "frame.gif"])
def test_bad_image_is_refused_before_the_model_is_reached(fake_model, run, tmp_path, name):
    if name.endswith(".gif"):
        (tmp_path / name).write_bytes(b"GIF89a")
    with pytest.raises(ValueError):
        _model(fake_model, run).ask(
            "judge", "s", "p", SCHEMA, cap_usd=0.05, images=(("reel", tmp_path / name),)
        )
    assert fake_model.calls == [] and _events(run, "model_call") == []


def test_oversized_image_is_refused(fake_model, run, tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "MAX_IMAGE_BYTES", 10)
    with pytest.raises(ValueError, match="over"):
        _model(fake_model, run).ask(
            "judge", "s", "p", SCHEMA, cap_usd=0.05, images=(("r", _png(tmp_path / "a.png")),)
        )
    assert fake_model.calls == []


def test_images_reach_the_transport(fake_model, run, tmp_path):
    fake_model.queue("judge", {"words": 1})
    image = _png(tmp_path / "a.png")
    _model(fake_model, run).ask("judge", "s", "p", SCHEMA, cap_usd=0.05, images=(("reel", image),))
    assert fake_model.calls[0].images == (("reel", image),)


@pytest.mark.slow
def test_live_call_sees_an_image(run, tmp_path):
    schema = {"type": "object", "properties": {"color": {"type": "string"}}, "required": ["color"]}
    image = _png(tmp_path / "red.png", rgb=(230, 20, 20), size=64)
    answer = Model(ClaudeCLI(), run, budget_usd=0.2).ask(
        "judge", "You name colours in one word.", "What colour fills this image?", schema,
        cap_usd=0.05, images=(("image", image),),
    )  # fmt: skip
    assert "red" in answer["color"].lower()
