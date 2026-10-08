"""Test doubles. The model contract lives in creature/llm.py; this module only fakes the transport."""

from __future__ import annotations

from typing import Any

from creature.llm import ModelCall, ModelReply, Transport

__all__ = ["FakeModel", "ModelCall", "ModelReply", "Transport"]


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
