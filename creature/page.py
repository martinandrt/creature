"""The side-by-side page for one run: what the creature saw next to what it made.

Two players on top (the reel and the clip), the verdict in one sentence, the details folded away:
frame strips, criteria with the judge's evidence, values from the tutorial with the sentence they came
from, costs per step, the skill's code. Written into the run's folder at the end of every run; the
same page is the preview a human approves an install from.
"""

from __future__ import annotations

import dataclasses
import html
import json
import shlex
from pathlib import Path
from typing import Any

PAGE = "page.html"
HELD_OUT = " <span class=h>(held out from the forge)</span>"

STYLE = """
body{font:15px/1.5 -apple-system,system-ui,sans-serif;background:#111318;color:#e9e9ec;margin:0;padding:24px}
main{max-width:1200px;margin:0 auto}h1{font-size:20px;margin:0 0 4px}.sub{color:#9aa0a8;margin:0 0 18px}
.players{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.players figure{margin:0;background:#181b22;border-radius:10px;padding:12px}
figcaption{font-size:12px;font-weight:600;letter-spacing:.05em;color:#8b919a;margin:0 0 8px}
video{width:100%;max-height:70vh;background:#000;border-radius:6px;display:block}
.verdict{font-size:18px;margin:18px 0;padding:14px 16px;border-radius:10px;background:#181b22}
.ok{border-left:5px solid #4cc46a}.no{border-left:5px solid #e0605a}.wait{border-left:5px solid #e0b54a}
details{background:#181b22;border-radius:10px;padding:12px 16px;margin:0 0 10px}
summary{cursor:pointer;font-weight:600}img{max-width:100%;border-radius:6px;display:block;margin:8px 0}
table{border-collapse:collapse;width:100%;margin:8px 0}td,th{text-align:left;padding:6px 8px;
border-top:1px solid #262a33;vertical-align:top}th{color:#8b919a;font-weight:600;font-size:13px}
.p{color:#7fd18b}.f{color:#ff8a80}.h{color:#9aa0a8;font-size:12px}
pre{background:#0b0c10;padding:12px;border-radius:6px;overflow:auto;font-size:12px;line-height:1.4}
@media (max-width:800px){.players{grid-template-columns:1fr}}
"""


def _e(value: Any) -> str:
    return html.escape(str(value))


def _rel(path: Path | None, folder: Path) -> str | None:
    if path is None or not Path(path).exists():
        return None
    try:
        return str(Path(path).resolve().relative_to(folder.resolve()))
    except ValueError:
        return None


def sentence(report: Any) -> str:
    """The verdict in one sentence."""
    spent = f"${report.spent_usd:.4f}"
    status = report.status
    if status == "BUILT":
        tries = "1 attempt" if report.attempts == 1 else f"{report.attempts} attempts"
        passed = "the clip passed every file check and every criterion"
        return f"Learned {report.skill} in {tries} for {spent}: {passed}."
    if status == "HAVE":
        return f"Already knew it: {report.skill} passed its own stored tests on this text, for {spent}."
    if status == "DONE" and report.skill == "wishes":
        return f"Read its own runs and named what it lacks, for {spent}: " + "; ".join(report.notes[:3])
    if status == "DONE" and str(report.skill).startswith("use:"):
        return f"Used the installed tool {report.skill[4:]} with no model call, for {spent}."
    if status == "DONE":
        return f"Ran {report.skill} on new text with no model call, for {spent}."
    if status in ("ASK", "REFUSE", "SKIP"):
        word = {"ASK": "Asked a human for", "REFUSE": "Refused", "SKIP": "Skipped"}[status]
        return f"{word}: {report.gap.splitlines()[0][:240] if report.gap else 'no reason given'} ({spent})."
    gap = report.gap.splitlines()[0][:240] if report.gap else "no reason recorded"
    return f"Failed after {report.attempts} attempts ({spent}). Missing: {gap}"


def render(folder: Path, report: Any, events: list[dict[str, Any]]) -> Path:
    """Write the run's page next to its files and return its path."""
    spec = report.spec
    clip = _rel(report.clip, folder)
    reel = _rel(folder / "reel.mp4", folder)
    tone = {"BUILT": "ok", "HAVE": "ok", "DONE": "ok", "ASK": "wait"}.get(report.status, "no")
    attempt = _attempt_folder(folder, report)
    name = spec.slug if spec else report.skill or "run"
    headline = spec.effect.splitlines()[0][:120] if spec else report.skill or ""
    parts = [
        "<!doctype html><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{_e(report.status)} · {_e(name)}</title><style>{STYLE}</style><main>",
        f"<h1>{_e(report.status)} · {_e(headline)}</h1>",
        f"<p class='sub'>Input text: “{_e(spec.text if spec else '')}” · run {_e(report.run_id)}</p>",
        "<div class='players'>",
        _player("WHAT THE REEL SHOWED", reel),
        _player("WHAT THE CREATURE MADE", clip),
        "</div>",
        f"<p class='verdict {tone}'>{_e(sentence(report))}</p>",
    ]
    parts.append(_source(events))
    parts.append(_timeline(events))
    parts.append(_replay(report, events))
    parts.append(_strips(folder, attempt))
    if attempt is not None and (attempt / "judge.json").exists():
        parts.append(_criteria(json.loads((attempt / "judge.json").read_text(encoding="utf-8"))))
    if spec and spec.params:
        parts.append(_params(spec.params, spec.param_sources))
    parts.append(_costs(events))
    if attempt is not None and (attempt / "skill.py").exists():
        code = (attempt / "skill.py").read_text(encoding="utf-8")
        parts.append(f"<details><summary>The skill's code</summary><pre>{_e(code)}</pre></details>")
    parts.append(
        f"<p class='h'>Authority fingerprint unchanged: {_e(report.fingerprint_same)} · "
        f"every step is in the run's ledger ({_e(report.run_id)}.jsonl)</p></main>"
    )
    path = folder / PAGE
    path.write_text("".join(parts), encoding="utf-8")
    return path


def _source(events: list[dict[str, Any]]) -> str:
    task = next((e for e in events if e["type"] in ("task", "design_task")), {})
    apify = next((e for e in events if e["type"] == "perceive_apify"), None)
    seen = next((e for e in events if e["type"] == "perceived"), None)
    source = _shown(task.get("source")) or f"design {task.get('design', '')}"
    link = f"<a href='{_e(source)}'>{_e(source)}</a>" if source.startswith("http") else _e(source)
    facts = [f"Source: {link}"]
    if seen:
        facts.append(f"{seen['duration_s']:.1f} s, {seen['width']}x{seen['height']}, {seen['fps']:g} fps")
    if apify:
        facts.append(f"fetched by Apify for ${apify['cost_usd']:.4f}")
    return f"<p class='sub'>{' · '.join(facts)}</p>"


def _timeline(events: list[dict[str, Any]]) -> str:
    """Every ledger event in order: what happened, what was called, what it cost."""
    if not events:
        return ""
    start = _seconds(events[0]["ts"])
    rows = []
    for e in events:
        detail = _describe(e)
        cost = f"${e['cost_usd']:.4f}" if e.get("cost_usd") else ""
        rows.append(
            f"<tr><td class='h'>+{_seconds(e['ts']) - start:.1f} s</td><td>{_e(e['type'])}</td>"
            f"<td>{_e(detail)}</td><td>{cost}</td></tr>"
        )
    return (
        f"<details open><summary>What happened: {len(events)} steps, all in the ledger</summary><table>"
        f"<tr><th>When</th><th>Step</th><th>Detail</th><th>Cost</th></tr>{''.join(rows)}</table></details>"
    )


def _describe(e: dict[str, Any]) -> str:
    kind = e["type"]
    if kind == "model_call":
        tail = "" if e.get("ok") else f" FAILED: {e.get('error', '')[:120]}"
        tokens = f"{e.get('input_tokens', 0)} in / {e.get('output_tokens', 0)} out"
        return f"{e.get('step')} → {e.get('model')}, {tokens}{tail}"
    if kind == "attempt_result":
        d = e.get("detail", {})
        if d.get("stage") == "judge":
            return f"attempt {e['attempt']}: judge {d.get('passed')}/{d.get('of')} criteria"
        why = "; ".join(d.get("problems", []))[:160] or d.get("error", "")
        return f"attempt {e['attempt']}: stopped at {d.get('stage')}: {why}"
    keys = {
        "run_start": ("image",),
        "task": ("text",),
        "design_task": ("design", "text"),
        "perceived": ("duration_s",),
        "plan": ("skill", "reason"),
        "spec": ("verdict", "slug", "visible", "held_out"),
        "forge_attempt": ("attempt", "approach"),
        "installed": ("skill", "version", "learn_usd"),
        "have_try": ("skill", "version", "ok"),
        "evolve": ("skill", "from_version"),
        "design_step": ("skill", "version", "checks"),
        "design_join": ("parts", "frames", "checks"),
        "install_refused": ("changed",),
        "run_end": ("status", "fingerprint_same", "changed"),
    }.get(kind, ())
    return ", ".join(f"{k}={e[k]}" for k in keys if k in e)[:220]


def _shown(source: Any) -> str:
    """A reel URL as it is; a local file by its name only, so no home path ends up on a page."""
    text = str(source or "")
    return text if text.startswith("http") else Path(text).name


def _seconds(ts: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(ts).timestamp()


def _replay(report: Any, events: list[dict[str, Any]]) -> str:
    """How to repeat the result: the sealed tests and the command that replays the skill at $0."""
    installed = next((e for e in events if e["type"] == "installed"), None)
    task = next((e for e in events if e["type"] in ("task", "design_task")), {})
    lines = []
    if installed and installed.get("kind") == "tool":
        name = ""  # a tool replays with `creature use`, below
    elif installed:
        name = installed.get("design") or installed.get("skill", "")
    else:
        name = (report.skill or "").split("@")[0].removeprefix("design:")
    if name:
        texts = task.get("texts") or [task.get("text", "")]
        given = " ".join(f"--text {shlex.quote(t)}" for t in texts)
        lines.append(f"python -m creature design {name} {given}   # no model, $0")
        lines.append(
            f"python -m creature show {name.split(':')[-1]}   # its sealed tests: file checks + criteria"
        )
    if task.get("source"):
        lines.append(
            f"python -m creature try {shlex.quote(_shown(task['source']))}"
            f" --text {shlex.quote(task.get('text', ''))}   # the whole run again"
            + ("" if str(task["source"]).startswith("http") else " (from the reel's folder)")
        )
    if not lines:
        return ""
    return f"<details open><summary>Replay</summary><pre>{_e(chr(10).join(lines))}</pre></details>"


def _attempt_folder(folder: Path, report: Any) -> Path | None:
    """The folder of the attempt the verdict is about: the last one, even when it crashed before a clip."""
    last = folder / f"attempt-{report.attempts}"
    if report.attempts and last.is_dir():
        return last
    for pattern in ("have-*", "step-*"):
        found = sorted(folder.glob(pattern))
        if found:
            return found[-1]
    return Path(report.clip).parent if report.clip else None


def _player(label: str, src: str | None) -> str:
    body = (
        f"<video src='{_e(src)}' controls loop muted autoplay playsinline></video>"
        if src
        else "<p>(none)</p>"
    )
    return f"<figure><figcaption>{label}</figcaption>{body}</figure>"


def _strips(folder: Path, attempt: Path | None) -> str:
    rows = []
    for label, path in (
        ("Reel frames", folder / "reel-strip.png"),
        ("Clip frames", attempt and attempt / "strip.png"),
        ("Clip frames, continued", attempt and attempt / "strip-2.png"),
    ):
        src = _rel(path, folder) if path else None
        if src:
            rows.append(f"<p class='h'>{label}</p><img src='{_e(src)}' alt='{label}'>")
    return f"<details><summary>Frames side by side</summary>{''.join(rows)}</details>" if rows else ""


def _criteria(results: list[dict[str, Any]]) -> str:
    rows = "".join(
        f"<tr><td class='{'p' if r['pass'] else 'f'}'>{'pass' if r['pass'] else 'fail'}</td>"
        f"<td>{_e(r['criterion'])}{HELD_OUT if r.get('hidden') else ''}</td>"
        f"<td class='h'>{_e(r['evidence'])}</td></tr>"
        for r in results
    )
    passed = sum(r["pass"] for r in results)
    return (
        f"<details><summary>Criteria: {passed} of {len(results)} passed</summary><table>"
        "<tr><th></th><th>Criterion (written before any attempt)</th><th>What the judge saw</th></tr>"
        f"{rows}</table></details>"
    )


def _params(params: dict[str, Any], sources: dict[str, str]) -> str:
    rows = "".join(
        f"<tr><td>{_e(name)}</td><td>{_e(json.dumps(value))}</td>"
        f"<td class='h'>{_e(sources.get(name, '(on screen only)'))}</td></tr>"
        for name, value in params.items()
    )
    return (
        "<details><summary>Values taken from the tutorial</summary><table>"
        f"<tr><th>Value</th><th></th><th>Where the reel says it</th></tr>{rows}</table></details>"
    )


def _costs(events: list[dict[str, Any]]) -> str:
    paid = [e for e in events if e.get("cost_usd")]
    rows = "".join(
        f"<tr><td>{_e(e.get('step') or e['type'])}</td><td>{_e(e.get('model', e.get('actor', '')))}</td>"
        f"<td>${e['cost_usd']:.4f}</td></tr>"
        for e in paid
    )
    total = sum(e["cost_usd"] for e in paid)
    return (
        f"<details><summary>Cost: ${total:.4f} in {len(paid)} paid calls</summary><table>"
        f"<tr><th>Step</th><th>By</th><th>Cost</th></tr>{rows}</table></details>"
    )


OVERVIEW = "overview.html"


def overview(home: Path) -> Path:
    """One page for the whole night: every run, newest first, refreshing itself while runs happen."""
    rows = []
    total = 0.0
    for ledger_file in sorted((home / "runs").glob("*.jsonl"), reverse=True):
        events = [
            json.loads(line) for line in ledger_file.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        if not events:
            continue
        task = next((e for e in events if e["type"] in ("task", "design_task")), {})
        end = next((e for e in events if e["type"] == "run_end"), None)
        spent = sum(e.get("cost_usd", 0) for e in events)
        total += spent
        status = end["status"] if end else "RUNNING"
        page = Path("runs") / ledger_file.stem / PAGE
        link = f"<a href='{_e(page)}'>open</a>" if (home / page).exists() else ""
        source = str(task.get("source") or f"design {task.get('design', '')}").split("/")[-1][:48]
        tone = {"BUILT": "p", "HAVE": "p", "DONE": "p"}.get(status, "f" if end else "h")
        same = ("yes" if end.get("fingerprint_same") else "no") if end else ""
        rows.append(
            f"<tr><td class='h'>{_e(events[0]['ts'][11:19])}</td><td>{_e(source)}</td>"
            f"<td>{_e(task.get('text', ''))[:60]}</td>"
            f"<td class='{tone}'>{_e(status)}</td><td>{_e((end or {}).get('skill') or '')}</td>"
            f"<td>{_e((end or {}).get('attempts', ''))}</td><td>${spent:.4f}</td>"
            f"<td>{same}</td><td>{link}</td></tr>"
        )
    body = (
        f"<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='15'>"
        f"<title>Creature · the night</title><style>{STYLE}</style><main><h1>The night so far</h1>"
        f"<p class='sub'>{len(rows)} runs · ${total:.4f} spent · refreshes every 15 s</p><table>"
        "<tr><th>Started</th><th>Reel</th><th>Text</th><th>Result</th><th>Skill</th><th>Attempts</th>"
        f"<th>Cost</th><th>Authority unchanged</th><th></th></tr>{''.join(rows)}</table></main>"
    )
    path = home / OVERVIEW
    path.write_text(body, encoding="utf-8")
    return path


def rebuild(home: Path, run_id: str) -> Path:
    """The page of a finished run again, from its ledger and its folder alone (no model, $0)."""
    from types import SimpleNamespace

    from creature import ledger
    from creature.criteria import Spec

    folder = home / "runs" / run_id
    events = ledger.read(home / "runs" / f"{run_id}.jsonl")
    end = next((e for e in events if e["type"] == "run_end"), None)
    if end is None:
        raise ValueError(f"run {run_id} has not finished")
    spec = None
    if (folder / "spec.json").is_file():
        data = json.loads((folder / "spec.json").read_text(encoding="utf-8"))
        known = {f.name for f in dataclasses.fields(Spec)}  # older runs may lack newer fields
        spec = Spec(
            **{
                k: tuple(v) if k in ("criteria", "held_out", "checks") else v
                for k, v in data.items()
                if k in known
            }
        )
    clip = folder / end["clip"] if end.get("clip") else _find_clip(folder, end)
    report = SimpleNamespace(
        run_id=run_id, status=end["status"], spec=spec, skill=end.get("skill"),
        attempts=end.get("attempts", 0), gap=end.get("gap", ""), spent_usd=end.get("spent_usd", 0.0),
        fingerprint_same=end.get("fingerprint_same"), clip=clip,
    )  # fmt: skip
    return render(folder, report, events)


def _find_clip(folder: Path, end: dict[str, Any]) -> Path | None:
    """For runs logged before run_end carried the clip: the joined clip, the last attempt, or a reuse."""
    for pattern in (
        "joined.mp4",
        f"attempt-{end.get('attempts', 0)}/clip.mp4",
        "have-*/clip.mp4",
        "step-*/clip.mp4",
    ):
        found = sorted(folder.glob(pattern))
        if found:
            return found[-1]
    return None
