"""Practice: on command, the creature returns to its own failures.

Which reels: from the ledgers alone, no model. A reel whose latest `try` in this home ended FAILED and whose
file is still there, the oldest failure first. Each reel gets at most RETURNS returns in all (counted from
earlier practice ledgers). The whole practice spends at most caps.practice_budget_usd from authority.json,
a human's decision: every return runs with the smaller of its run budget and what is left of it, so the
total can never pass the cap. Each return is an ordinary run (its own ledger, page and checks); the
practice's own ledger records one event per return: which reel, which return, what it took from the
registry, the result and its cost.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from creature import authority, ledger
from creature.ledger import Ledger
from creature.llm import Transport

RETURNS = 3  # returns per reel, ever: after three the failure is left for a human to look at


@dataclass
class Return:
    reel: str
    attempt: int
    run: str
    status: str
    spent_usd: float
    took: list[str] = field(default_factory=list)


@dataclass
class Practice:
    run: str
    returns: list[Return]
    stopped: str  # why it ended: "no failures left", "budget", …
    spent_usd: float


def _events(path: Path) -> list[dict[str, Any]]:
    try:
        return ledger.read(path)
    except (OSError, ValueError):
        return []


def failures(home: Path) -> list[tuple[str, str]]:
    """(reel, text) for every reel whose latest try here FAILED and whose file still exists, oldest first."""
    latest: dict[str, tuple[str, str, str]] = {}  # reel -> (start, status, text)
    for path in sorted((home / "runs").glob("*.jsonl")):
        events = _events(path)
        task = next((e for e in events if e["type"] == "task"), None)
        end = next((e for e in events if e["type"] == "run_end"), None)
        if task is None or end is None or not task.get("source"):
            continue
        start = events[0]["ts"]
        reel = str(task["source"])
        if reel not in latest or start > latest[reel][0]:
            latest[reel] = (start, str(end.get("status")), str(task.get("text", "")))
    ordered = sorted(latest.items(), key=lambda kv: kv[1][0])
    return [
        (reel, text) for reel, (_, status, text) in ordered if status == "FAILED" and Path(reel).is_file()
    ]


def returned(home: Path) -> dict[str, int]:
    """How many times practice already returned to each reel, from the practice ledgers."""
    counts: dict[str, int] = {}
    for path in (home / "runs").glob("*.jsonl"):
        for event in _events(path):
            if event["type"] == "practice_return":
                counts[event["reel"]] = counts.get(event["reel"], 0) + 1
    return counts


def took(home: Path, run: str) -> list[str]:
    """What a run took from the registry instead of learning it: whole skills (HAVE) and montage screens."""
    found = []
    for event in _events(home / "runs" / f"{run}.jsonl"):
        if event["type"] == "have_try" and event.get("ok"):
            found.append(f"{event['skill']}@v{event['version']}")
        if event["type"] == "part" and event.get("status") == "have":
            found.append(f"{event['skill']}@v{event['version']}")
    return found


def practice(home: Path, *, transport: Transport | None = None, returns: int = RETURNS) -> Practice:
    from creature.loop import Creature  # Docker and the model only when practice really runs

    caps = authority.load(home).caps
    budget = caps.practice_budget_usd
    log = Ledger.start(home)
    log.record("practice_task", budget_usd=budget, returns_per_reel=returns)
    done: list[Return] = []
    spent, stopped = 0.0, "no failures left"
    counts = returned(home)
    if budget <= 0:
        stopped = "no practice budget in authority.json (caps.practice_budget_usd)"
    for reel, text in failures(home) if budget > 0 else []:
        attempt = counts.get(reel, 0) + 1
        if attempt > returns:
            continue
        left = budget - spent
        if left < caps.call_reserve_usd:
            stopped = "budget"
            break
        creature = Creature(home, transport)
        # the practice cap narrows this return's own budget: the total cannot pass what the human allowed
        creature.model.budget_usd = min(creature.model.budget_usd, left)
        creature.ledger.record("practice_of", practice=log.run_id, attempt=attempt)
        report = creature.try_reel(reel, text)
        spent += report.spent_usd
        back = Return(Path(reel).name, attempt, report.run_id, report.status, round(report.spent_usd, 6))
        back.took = took(home, report.run_id)
        done.append(back)
        log.record(
            "practice_return", reel=reel, attempt=attempt, return_run=report.run_id, result=report.status,
            took=back.took, cost_usd_of_run=back.spent_usd,
        )  # fmt: skip
    log.record("run_end", status="DONE", gap=stopped, spent_usd=round(spent, 6), returns=len(done))
    (home / "runs" / f"{log.run_id}.json").write_text(
        json.dumps([r.__dict__ for r in done], indent=1, ensure_ascii=False), encoding="utf-8"
    )
    return Practice(log.run_id, done, stopped, spent)
