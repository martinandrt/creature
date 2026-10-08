"""The static gate's allow/deny verdicts (F5). Skips until creature/gate.py exists.

Interface (confirmed with the main session):
    gate.check(code: str) -> Verdict(ok: bool, reasons: list[str])
with each reason formatted "line N: message".
"""

import pytest

from tests.payloads import GATE_PAYLOADS

gate = pytest.importorskip("creature.gate")


@pytest.mark.parametrize("payload", GATE_PAYLOADS, ids=lambda p: p.id)
def test_gate_verdict(payload):
    verdict = gate.check(payload.code)
    assert verdict.ok == (payload.verdict == "allow"), f"{payload.id}: {payload.note}"
    # A reject must say why; an allow must be silent. The jury reads these reasons.
    if payload.verdict == "reject":
        assert verdict.reasons, f"{payload.id}: rejected with no reason"
    else:
        assert not verdict.reasons, f"{payload.id}: allowed but reported {verdict.reasons}"
