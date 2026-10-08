"""The static gate's allow/deny verdicts (F5). Skips until creature/gate.py exists.

Assumed interface (confirm with the main session): gate.check(code: str) -> verdict with a
bool .ok. Adjust the single call below if the gate exposes a different name.
"""

import pytest

from tests.payloads import GATE_PAYLOADS

gate = pytest.importorskip("creature.gate")


@pytest.mark.parametrize("payload", GATE_PAYLOADS, ids=lambda p: p.id)
def test_gate_verdict(payload):
    allowed = gate.check(payload.code).ok
    assert allowed == (payload.verdict == "allow"), f"{payload.id}: {payload.note}"
