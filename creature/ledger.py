"""Append-only run log: one JSON event per line, chained by hashes.

Every event carries seq, ts, run, type, prev_hash and hash, where hash is the sha256 of the event
without its own hash. Editing, removing or reordering a line breaks the chain and verify() names
the first broken line. Cutting lines off the end leaves a valid shorter chain; only the run_end
event (written last) shows that a run finished.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
RESERVED = frozenset({"seq", "ts", "run", "type", "prev_hash", "hash"})
TYPE = re.compile(r"^[a-z][a-z0-9_]*$")


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def _canonical(event: dict[str, Any]) -> bytes:
    text = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return text.encode()


def _digest(event: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in event.items() if k != "hash"})).hexdigest()


def _cost(event: dict[str, Any]) -> float:
    return float(event.get("cost_usd", 0.0))


def _check_cost(value: Any) -> None:
    # a negative or NaN cost would let a run hand itself budget back
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"cost_usd must be a finite number >= 0, got {value!r}")


class Ledger:
    """Writer for one run's log. Reopening an existing file continues its chain."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.run_id = self.path.stem
        events = read(self.path) if self.path.exists() else []
        problem = verify_events(events)
        if problem:
            raise ValueError(f"ledger {self.path} is broken: {problem}")
        self._seq = len(events)
        self._prev = events[-1]["hash"] if events else GENESIS
        self._spent = sum(_cost(event) for event in events)

    @classmethod
    def start(cls, home: str | Path, run_id: str | None = None) -> Ledger:
        path = Path(home) / "runs" / f"{run_id or new_run_id()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=False)  # a run id is never reused
        return cls(path)

    @property
    def spent_usd(self) -> float:
        return self._spent

    def record(self, event_type: str, /, **data: Any) -> dict[str, Any]:
        """Append one event and return it. Nothing is written if the event is invalid."""
        if not TYPE.match(event_type):
            raise ValueError(f"bad event type {event_type!r}")
        clash = RESERVED & data.keys()
        if clash:
            raise ValueError(f"reserved keys in event data: {sorted(clash)}")
        if "cost_usd" in data:
            _check_cost(data["cost_usd"])
        event = {
            "seq": self._seq,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "run": self.run_id,
            "type": event_type,
            **data,
            "prev_hash": self._prev,
        }
        event["hash"] = _digest(event)  # raises before writing if data is not JSON
        with self.path.open("a", encoding="utf-8") as file:
            file.write(_canonical(event).decode() + "\n")
        self._seq += 1
        self._prev = event["hash"]
        self._spent += _cost(event)
        return event


def read(path: str | Path) -> list[dict[str, Any]]:
    """All events of a ledger file. Raises ValueError on a line that is not a JSON object."""
    events = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise ValueError(f"line {number}: not JSON") from None
        if not isinstance(event, dict):
            raise ValueError(f"line {number}: not an object")
        events.append(event)
    return events


def verify_events(events: list[dict[str, Any]]) -> str | None:
    """The first break in the chain, or None when it is intact."""
    prev = GENESIS
    for index, event in enumerate(events):
        if event.get("seq") != index:
            return f"line {index + 1}: seq {event.get('seq')!r}, expected {index}"
        if event.get("prev_hash") != prev:
            return f"line {index + 1}: prev_hash does not match line {index}"
        try:
            digest = _digest(event)
        except (TypeError, ValueError):
            return f"line {index + 1}: not canonical JSON"
        if event.get("hash") != digest:
            return f"line {index + 1}: hash does not match content"
        prev = digest
    return None


def verify(path: str | Path) -> str | None:
    """The first problem in a ledger file, or None when it is intact."""
    try:
        return verify_events(read(path))
    except ValueError as error:
        return str(error)
