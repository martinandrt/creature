"""Test doubles.

The model interface here is a proposal for creature/llm.py (F3). Once llm.py lands,
ModelCall, ModelReply and Transport move there and this module imports them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ModelCall:
    step: str  # spine step that asks: "planner", "examiner", "forge", "perceive", "skill:<name>"
    model: str
    system: str
    prompt: str
    schema: dict[str, Any]
    max_usd: float


@dataclass(frozen=True)
class ModelReply:
    data: dict[str, Any]  # structured_output from the CLI
    cost_usd: float  # total_cost_usd from the CLI, list-price equivalent
    model: str = "claude-haiku-4-5-20251001"
    input_tokens: int = 0
    output_tokens: int = 0


class Transport(Protocol):
    """One isolated model call. Caps, ledger events and retries live above it, in llm.py."""

    def complete(self, call: ModelCall) -> ModelReply: ...


class FakeModel:
    """Scripted Transport: replies are queued per step and returned in order."""

    def __init__(self) -> None:
        self.calls: list[ModelCall] = []
        self._script: dict[str, list[ModelReply | Exception]] = {}

    def queue(self, step: str, reply: dict[str, Any] | Exception, cost_usd: float = 0.0018) -> FakeModel:
        item = reply if isinstance(reply, Exception) else ModelReply(data=reply, cost_usd=cost_usd)
        self._script.setdefault(step, []).append(item)
        return self

    def complete(self, call: ModelCall) -> ModelReply:
        self.calls.append(call)
        pending = self._script.get(call.step)
        if not pending:
            raise AssertionError(f"unscripted model call for step {call.step!r}")
        reply = pending.pop(0)
        if isinstance(reply, Exception):
            raise reply
        missing = [key for key in call.schema.get("required", []) if key not in reply.data]
        if missing:
            raise AssertionError(f"scripted reply for {call.step!r} lacks required keys {missing}")
        return reply

    def assert_drained(self) -> None:
        left = {step: len(replies) for step, replies in self._script.items() if replies}
        assert not left, f"scripted replies never requested: {left}"
