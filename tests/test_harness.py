"""The test harness itself: isolation guards and the fake model."""

import os
import socket
import subprocess
from pathlib import Path

import pytest

from tests.fakes import FakeModel, ModelCall


def _call(step: str, required: list[str] | None = None) -> ModelCall:
    schema = {"type": "object", "required": required or []}
    return ModelCall(step=step, model="m", system="s", prompt="p", schema=schema, max_usd=0.02)


def test_home_is_temp_and_exported(home):
    assert os.environ["CREATURE_HOME"] == str(home)
    assert {p.name for p in home.iterdir()} == {"registry", "queue", "runs"}
    assert Path(__file__).resolve().parent.parent not in home.parents


def test_secrets_are_hidden():
    assert "CREATURE_SECRETS" not in os.environ


def test_offline_test_cannot_open_network():
    with socket.socket() as sock, pytest.raises(RuntimeError, match="network"):
        sock.connect(("127.0.0.1", 9))


@pytest.mark.parametrize("argv", [["claude", "-p", "hi"], ["/usr/local/bin/claude"], "claude -p hi"])
def test_offline_test_cannot_call_the_model(argv):
    with pytest.raises(RuntimeError, match="live model call"):
        subprocess.run(argv, shell=isinstance(argv, str), check=False)


def test_other_processes_still_run():
    assert subprocess.run(["true"], check=False).returncode == 0


def test_fake_model_replays_in_order_per_step(fake_model):
    fake_model.queue("planner", {"plan": 1}).queue("planner", {"plan": 2}, cost_usd=0.5)
    fake_model.queue("forge", {"code": "x"})

    assert fake_model.complete(_call("forge")).data == {"code": "x"}
    assert fake_model.complete(_call("planner")).data == {"plan": 1}
    second = fake_model.complete(_call("planner"))
    assert (second.data, second.cost_usd) == ({"plan": 2}, 0.5)
    assert [c.step for c in fake_model.calls] == ["forge", "planner", "planner"]
    fake_model.assert_drained()


def test_fake_model_raises_scripted_errors(fake_model):
    fake_model.queue("forge", TimeoutError("model timed out"))
    with pytest.raises(TimeoutError):
        fake_model.complete(_call("forge"))


def test_fake_model_rejects_unscripted_calls_and_schema_drift():
    fake = FakeModel().queue("examiner", {"tests": []})
    with pytest.raises(AssertionError, match="unscripted"):
        fake.complete(_call("planner"))
    with pytest.raises(AssertionError, match="lacks required keys"):
        fake.complete(_call("examiner", required=["tests", "spec"]))


def test_fake_model_reports_unused_replies():
    fake = FakeModel().queue("forge", {"code": "x"})
    with pytest.raises(AssertionError, match="never requested"):
        fake.assert_drained()
