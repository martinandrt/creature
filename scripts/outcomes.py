"""How every run of the night ended, from the ledgers alone: status counts, and FAILED split by cause.

A run with no end in its ledger was stopped by hand (STOPPED). A FAILED run is put down to the
infrastructure when its gap shows a container killed or cut off under load, or a bug in our own code
(the spine, the montage, the preview); otherwise it is a real failure: the judge, a fixed check, the
budget, or the creature's own code. The rules are the patterns below, nothing else.

    python3 scripts/outcomes.py ~/Desktop/creature-homes docs/outcomes.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

# (cause, pattern in the gap) — first match wins
INFRA = [
    ("timeout under load", r"killed: timeout"),
    ("container cut off", r"BrokenPipeError|no result \(exit \d+\)"),
    ("our bug: screens rendered past their learned length (fixed in 32b2d11)", r"render workers failed"),
    ("our bug: preview image over the model's 5 MB cap (fixed)", r"pairs\.png is over"),
    ("our bug: the montage code", r"operands could not be broadcast"),
]
FIXED_AT = "2026-10-09T01:25"  # 32b2d11: after it, a failed render worker is the skill's own


def cause(gap: str, ended: str) -> str | None:
    for name, pattern in INFRA:
        if re.search(pattern, gap):
            if "render workers" in pattern and ended >= FIXED_AT:
                return None
            return name
    return None


def main(homes: Path, out: Path) -> None:
    status: Counter[str] = Counter()
    infra: Counter[str] = Counter()
    examples: dict[str, str] = {}
    real_examples: list[str] = []
    for path in sorted(homes.glob("*/runs/*.jsonl")):
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        end = next((e for e in events if e["type"] == "run_end"), None)
        where = f"{path.parents[1].name}/{path.stem}"
        if end is None:
            status["STOPPED"] += 1
            examples.setdefault("STOPPED", where)
            continue
        status[end["status"]] += 1
        examples.setdefault(end["status"], where)
        if end["status"] != "FAILED":
            continue
        why = cause(end.get("gap") or "", end.get("ts", ""))
        if why:
            infra[why] += 1
            examples.setdefault(why, f"{where}: {(end.get('gap') or '')[:160]}")
        else:
            status["FAILED real"] += 1
            if len(real_examples) < 3:
                real_examples.append(f"{where}: {(end.get('gap') or '')[:160]}")
    status["FAILED infrastructure"] = sum(infra.values())
    result = {
        "runs": sum(v for k, v in status.items() if not k.startswith("FAILED ")),
        "status": dict(status),
        "infrastructure": dict(infra),
        "examples": examples,
        "real_examples": real_examples,
    }
    out.write_text(json.dumps(result, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("runs", "status", "infrastructure")}, ensure_ascii=False))


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser(), Path(sys.argv[2]))
