"""POSTUP.md: how a run went, written by fixed code from its ledger and its own spec.json. No model.

Source, what the creature saw and how it meant to make it (its own task and visible criteria), the
rounds with its own notes and scores, the result with cost and time, what it took from the registry
and what it learned. Held-out criteria never appear: they stay hidden from the creature's own record.
The same ledger always gives the same text. A skill installed by the run gets the same record beside
its sealed files, through the registry. For now the creature only writes this; it does not read it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from creature import ledger, page, registry

NAME = "POSTUP.md"
STEPS = {  # what each event says in the rounds, in ledger order
    "plan", "have_try", "evolve", "composed", "part", "forge_attempt", "round", "round_score",
    "attempt_result", "other_length", "install_refused", "design_step", "design_join", "montage_source",
    "montage", "make_catalog",
    "make_choice", "card", "cards_task", "make_shot", "make_judged", "practice_return", "gaps", "scored",
}  # fmt: skip
SHOWN_SPEC = ("effect", "task", "reason", "verdict", "params")  # never held_out


def text(events: list[dict[str, Any]], spec: dict[str, Any] | None = None) -> str:
    """The record of one run as Markdown. Deterministic: only the ledger and the spec go in."""
    first = events[0] if events else {}
    end = next((e for e in reversed(events) if e["type"] == "run_end"), {})
    run = first.get("run", "?")
    out = [f"# How run {run} went", ""]

    task = _first(events, "task", "design_task", "make_task")
    seen = _first(events, "perceived")
    out += ["## Source", ""]
    if task:
        for key in ("source", "design", "text", "lines"):
            if key in task:
                out.append(f"- {key}: {_value(task[key])}")
    if seen:
        out.append(
            f"- measured: {seen.get('duration_s')} s, {seen.get('width')}x{seen.get('height')} at "
            f"{seen.get('fps')} fps, {seen.get('cuts')} cuts, palette {_value(seen.get('palette', []))}, "
            f"transcript {seen.get('transcript_chars', 0)} chars from {seen.get('transcript_from', '?')}"
        )
        for key in ("author", "caption"):
            if seen.get(key):
                out.append(f"- {key}: {_value(seen[key])}")
    if not task and not seen:
        out.append("- (none recorded)")
    out.append("")

    if spec:
        out += ["## What it saw and how it meant to make it", ""]
        for key in SHOWN_SPEC:
            if spec.get(key) not in (None, "", {}, []):
                out.append(f"- {key}: {_value(spec[key])}")
        if spec.get("criteria"):
            out += ["", "Visible criteria:", *[f"- {c}" for c in spec["criteria"]]]
        out.append("")

    steps = [e for e in events if e["type"] in STEPS]
    out += ["## Steps", ""]
    out += [f"{n}. {e['type']}: {_step(e)}" for n, e in enumerate(steps, start=1)] or ["- (no steps)"]
    out.append("")

    calls = [e for e in events if e["type"] == "model_call"]
    out += ["## Result", ""]
    out.append(f"- status: {end.get('status', 'not finished')}")
    if end.get("gap"):
        out.append(f"- what did not pass: {end['gap']}")
    out.append(f"- spent: ${float(end.get('spent_usd', 0) or 0):.4f} in {len(calls)} model calls")
    by_step: dict[str, float] = {}
    for call in calls:
        by_step[call.get("step", "?")] = by_step.get(call.get("step", "?"), 0.0) + float(
            call.get("cost_usd") or 0
        )
    if by_step:
        out.append("- by step: " + ", ".join(f"{k} ${v:.4f}" for k, v in sorted(by_step.items())))
    out.append(f"- time: {_seconds(first.get('ts'), end.get('ts'))}")
    out.append(f"- authority unchanged: {end.get('fingerprint_same')}")
    out.append("")

    used, learned = _registry(events)
    out += ["## Registry", ""]
    out.append("- used: " + (", ".join(used) or "nothing"))
    out.append("- learned: " + (", ".join(learned) or "nothing"))
    out.append("")
    return "\n".join(out)


def write(folder: Path, events: list[dict[str, Any]]) -> Path:
    path = folder / NAME
    path.write_text(hide(text(events, _spec(folder)), _held_out(folder)), encoding="utf-8")
    return path


def hide(record: str, held_out: list[str]) -> str:
    """A held-out criterion quoted anywhere (a judge's feedback in a gap, say) is replaced, never shown."""
    for criterion in sorted({h.strip() for h in held_out if h.strip()}, key=len, reverse=True):
        record = record.replace(criterion, "[a held-out criterion]")
    return record


def _held_out(folder: Path) -> list[str]:
    """Every held-out criterion in the run's spec, its parts' included."""
    try:
        data = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "held_out" and isinstance(value, list):
                    found.extend(str(v) for v in value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def for_skills(root: Path, events: list[dict[str, Any]], record: str) -> list[str]:
    """Each skill this run installed gets the run's record, with its origin and known limits."""
    done = []
    for e in events:
        if e["type"] != "installed":
            continue
        try:
            skill = registry.get(root, e["skill"], e["version"])
        except (KeyError, ValueError):
            continue
        registry.save_postup(root, skill.slug, skill.version, skill_text(skill, events, record))
        done.append(f"{skill.slug}@v{skill.version}")
    return done


def skill_text(skill: registry.Skill, events: list[dict[str, Any]], record: str) -> str:
    cap = skill.capability
    origin = cap.get("origin", {})
    failed = [e for e in events if e["type"] == "attempt_result" and not e.get("ok")]
    limits = [
        f"learned and tested at {cap['output'].get('duration_s')} s, "
        f"{cap['output'].get('width')}x{cap['output'].get('height')}",
        f"{len(skill.tests.get('held_out', []))} criteria are held out and not shown here",
    ]
    if cap.get("assets"):
        limits.append("needs the asset library: " + ", ".join(cap["assets"]))
    for e in failed:
        limits.append(f"attempt {e.get('attempt')} failed at {_step(e)}")
    other = _first(events, "other_length")
    if other and not other.get("ok"):
        limits.append(
            f"at another length ({other.get('seconds')} s) it failed: {_value(other.get('problems'))}"
        )
    head = [
        f"# {skill.slug} v{skill.version}: how it was made",
        "",
        f"- origin: reel {origin.get('reel', '?')}, run {origin.get('run', '?')}",
        f"- learned for ${float(cap.get('cost', {}).get('learn_usd', 0) or 0):.4f}",
        "",
        "## Known limits",
        "",
        *[f"- {line}" for line in limits],
        "",
        "## The run that made it",
        "",
    ]
    return "\n".join(head) + "\n" + record.split("\n", 2)[-1]


def rebuild(home: Path) -> list[Path]:
    """`creature postup`: the record of every finished run in a home again, from its ledger (no model)."""
    written = []
    for path in sorted((home / "runs").glob("*.jsonl")):
        events = ledger.read(path)
        if not any(e["type"] == "run_end" for e in events):
            continue
        folder = home / "runs" / path.stem
        folder.mkdir(exist_ok=True)
        written.append(write(folder, events))
        for_skills(home / "registry", events, written[-1].read_text(encoding="utf-8"))
    return written


def _spec(folder: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {k: data[k] for k in (*SHOWN_SPEC, "criteria") if k in data} if isinstance(data, dict) else None


def _first(events: list[dict[str, Any]], *kinds: str) -> dict[str, Any] | None:
    return next((e for e in events if e["type"] in kinds), None)


def _step(e: dict[str, Any]) -> str:
    if e["type"] == "round":
        problems = "; ".join(e.get("problems") or [])
        return f"round {e.get('round')}: {e.get('note', '')}" + (
            f" (problems: {problems})" if problems else ""
        )
    if e["type"] == "attempt_result":
        d = e.get("detail", {})
        if d.get("stage") == "judge":
            judged = f"{d.get('passed')}/{d.get('of')} criteria (visible and held out)"
            return f"attempt {e.get('attempt')}: judge {judged}"
        why = "; ".join(d.get("problems", [])) or d.get("error", "")
        return f"attempt {e.get('attempt')}: stopped at {d.get('stage')}: {why}"[:400]
    if e["type"] == "forge_attempt":
        return f"attempt {e.get('attempt')}: {e.get('approach', '')}"
    if e["type"] == "practice_return":
        keep = ("reel", "attempt", "status", "took", "spent_usd")
        return ", ".join(f"{k}={_value(e[k])}" for k in keep if k in e)
    said = page._describe(e)
    if said:
        return said
    keep = {k: v for k, v in e.items() if k not in ("type", "run", "seq", "ts", "hash", "prev_hash")}
    return _value(keep)[:400]


def _registry(events: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    used: list[str] = []
    for e in events:
        name = None
        if e["type"] in ("have_try", "design_step", "montage_source", "make_shot") and e.get("skill"):
            name = f"{e['skill']}@v{e['version']}" if e.get("version") else e["skill"]
        elif e.get("skill") and (e["type"] == "plan" or (e["type"] == "part" and e.get("status") == "have")):
            name = e["skill"]
        if name and name not in used:
            used.append(name)
    learned = [f"{e['skill']}@v{e['version']}" for e in events if e["type"] == "installed"]
    return used, learned


def _value(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _seconds(start: str | None, end: str | None) -> str:
    try:
        return f"{(datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds():.0f} s"
    except (TypeError, ValueError):
        return "unknown"
