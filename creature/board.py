"""The board: one page over every home and every run, live. Input, output, time, cost, what was learned,
and for a run still going its latest steps and the latest picture of reel next to output. No model; it only
reads ledgers and files.
"""

from __future__ import annotations

import html
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from creature import ledger

REFRESH_S = 10
STEPS_SHOWN = 8
STALE_S = 900  # a run with no end and nothing new for this long was stopped

STYLE = """
:root{--bg:#f6f5f2;--fg:#1b1d22;--mute:#6b6f78;--line:#dddad3;--card:#fff;--ok:#1f7a45;--bad:#b3261e;--run:#9a6700}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#15171b;--fg:#e9e7e2;--mute:#9aa0aa;
--line:#2b2f36;--card:#1d2026;--ok:#5fc48a;--bad:#ff8a80;--run:#e3b341}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);
font:14px/1.45 -apple-system,system-ui,sans-serif}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mute);margin:0 0 16px}
.sum{display:flex;gap:16px;flex-wrap:wrap;margin:0 0 16px}
.sum div{background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:8px 12px}.sum b{font-size:18px;display:block}
.run{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin:0 0 12px;
display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) minmax(0,1.2fr);gap:12px}
@media (max-width:900px){.run{grid-template-columns:1fr}}
.head{grid-column:1/-1;display:flex;gap:12px;flex-wrap:wrap;align-items:baseline}
.st{font-weight:700}.BUILT,.HAVE,.DONE{color:var(--ok)}.FAILED,.REFUSE,.STOPPED{color:var(--bad)}.RUNNING,.ASK{color:var(--run)}
.mute{color:var(--mute)}img,video{max-width:100%;max-height:360px;border-radius:6px;background:#000;display:block}
ol{margin:4px 0 0 18px;padding:0}li{margin:0 0 2px}a{color:inherit}
"""


def _when(ts: str) -> float:
    return datetime.fromisoformat(ts).timestamp()


def _rel(path: Path | None, base: Path) -> str | None:
    return os.path.relpath(path, base) if path is not None and path.exists() else None


def _step(e: dict[str, Any]) -> str:
    kind = e["type"]
    if kind == "round":
        return f"kolo {e.get('round')}: {e.get('note', '')[:140]}"
    if kind == "round_score":
        best = f"nejlepší {e.get('best')} v kole {e.get('best_round')}"
        return f"kolo {e.get('round')}: skóre {e.get('score')} ({best})"
    if kind == "model_call":
        return f"model {e.get('step')} → {e.get('model')}, ${e.get('cost_usd', 0):.3f}" + (
            "" if e.get("ok") else " (chyba)"
        )
    if kind == "attempt_result":
        d = e.get("detail", {})
        return f"pokus {e.get('attempt')}: {d.get('stage')} {d.get('passed', '')}/{d.get('of', '')}".strip()
    if kind == "spec":
        return f"kritéria: {e.get('verdict')}, {e.get('kind', 'effect')}, {e.get('slug')}"
    if kind == "part":
        return f"plocha {e.get('slug')}: {e.get('status')}"
    if kind == "installed":
        return f"nainstalováno {e.get('skill')} v{e.get('version')}"
    if kind == "run_end":
        return f"konec: {e.get('status')}"
    return kind


def _latest_picture(folder: Path) -> Path | None:
    found = sorted(folder.glob("**/pairs.png"), key=lambda p: p.stat().st_mtime)
    found = found or sorted(folder.glob("**/strip.png"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def runs(homes: list[Path]) -> list[dict[str, Any]]:
    found = []
    for home in homes:
        for path in (home / "runs").glob("*.jsonl"):
            try:
                events = ledger.read(path)
            except (OSError, ValueError):
                continue
            if not events:
                continue
            folder = path.with_suffix("")
            end = next((e for e in events if e["type"] == "run_end"), None)
            task = next(
                (e for e in events if e["type"] in ("task", "design_task", "tool_task", "wishes_task")), {}
            )
            clip = folder / end["clip"] if end and end.get("clip") else None
            found.append(
                {
                    "home": home.name,
                    "id": path.stem,
                    "folder": folder,
                    "start": _when(events[0]["ts"]),
                    "last": _when(events[-1]["ts"]),
                    "status": end["status"]
                    if end
                    else ("RUNNING" if time.time() - _when(events[-1]["ts"]) < STALE_S else "STOPPED"),
                    "source": Path(
                        str(
                            task.get("source")
                            or task.get("design")
                            or task.get("wish")
                            or task.get("type")
                            or (end or {}).get("skill")
                            or "run"
                        )
                    ).name,
                    "text": str(task.get("text", "")),
                    "spent": sum(float(e.get("cost_usd") or 0) for e in events),
                    "learned": [f"{e['skill']} v{e['version']}" for e in events if e["type"] == "installed"],
                    "gap": str(end.get("gap") or "") if end else "",
                    "clip": clip,
                    "steps": [
                        _step(e) for e in events if e["type"] not in ("run_start", "task", "perceive_local")
                    ],
                }
            )
    return sorted(found, key=lambda r: (r["status"] != "RUNNING", -r["start"]))


def render(homes: list[Path], out: Path) -> Path:
    base = out.parent
    items = runs(homes)
    now = time.time()
    spent = sum(r["spent"] for r in items)
    learned = sum(len(r["learned"]) for r in items)
    going = sum(r["status"] == "RUNNING" for r in items)
    parts = [
        f"<!doctype html><html lang='cs'><head><meta charset='utf-8'><meta name='viewport' "
        f"content='width=device-width,initial-scale=1'><meta http-equiv='refresh' content='{REFRESH_S}'>"
        f"<title>Tvor naživo</title><style>{STYLE}</style></head><body>",
        "<h1>Tvor naživo</h1>",
        f"<p class='sub'>Obnovuje se každých {REFRESH_S} s · {datetime.now():%H:%M:%S} · domovy: "
        f"{html.escape(', '.join(h.name for h in homes))}</p>",
        f"<div class='sum'><div><b>{len(items)}</b>běhů</div><div><b>{going}</b>běží</div>"
        f"<div><b>{learned}</b>naučeno</div><div><b>${spent:.2f}</b>utraceno</div></div>",
    ]
    for r in items:
        seconds = (now if r["status"] == "RUNNING" else r["last"]) - r["start"]
        page = _rel(r["folder"] / "page.html", base)
        reel = _rel(r["folder"] / "reel-strip.png", base)
        clip = _rel(r["clip"], base) if r["clip"] else None
        picture = None if clip else _rel(_latest_picture(r["folder"]), base)
        head = (
            f"<div class='head'><span class='st {r['status']}'>{r['status']}</span>"
            f"<b>{html.escape(r['source'])}</b><span class='mute'>{html.escape(r['home'])} · {r['id']}</span>"
            f"<span>{seconds / 60:.1f} min</span><span>${r['spent']:.3f}</span>"
            + (f"<span>naučeno: {html.escape(', '.join(r['learned']))}</span>" if r["learned"] else "")
            + (f"<a href='{html.escape(page)}'>stránka běhu</a>" if page else "")
            + "</div>"
        )
        left = (
            "<div><div class='mute'>Vstup</div>"
            + (f"<img src='{html.escape(reel)}' alt='snímky reelu'>" if reel else "")
            + (f"<div>„{html.escape(r['text'][:160])}“</div>" if r["text"] else "")
            + "</div>"
        )
        if clip:
            player = f"<video src='{html.escape(clip)}' controls muted loop playsinline></video>"
            middle = f"<div><div class='mute'>Výstup</div>{player}</div>"
        elif picture:
            shown = f"<img src='{html.escape(picture)}' alt=''>"
            middle = f"<div><div class='mute'>Poslední obrázek (reel | výstup)</div>{shown}</div>"
        else:
            middle = "<div><div class='mute'>Výstup</div><div class='mute'>zatím nic</div></div>"
        steps = "".join(f"<li>{html.escape(s)}</li>" for s in r["steps"][-STEPS_SHOWN:])
        gap = f"<div class='mute'>Chybí: {html.escape(r['gap'][:240])}</div>" if r["gap"] else ""
        right = f"<div><div class='mute'>Postup</div><ol>{steps}</ol>{gap}</div>"
        parts.append(f"<section class='run'>{head}{left}{middle}{right}</section>")
    parts.append("</body></html>")
    out.write_text("".join(parts), encoding="utf-8")
    return out


def main(homes: list[Path], out: Path, every: float | None) -> None:
    while True:
        try:
            render([h for h in homes if (h / "runs").is_dir()], out)
        except Exception as error:  # one bad ledger must not freeze the page: say so on it
            note = f"<p>The board could not be drawn: {html.escape(repr(error))}</p>"
            out.write_text(
                f"<!doctype html><meta http-equiv='refresh' content='{REFRESH_S}'>{note}", encoding="utf-8"
            )
        if not every:
            return
        time.sleep(every)


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    every = float(args.pop(0)[len("--every=") :]) if args and args[0].startswith("--every=") else None
    target = Path(args.pop(0))
    main([Path(a) for a in args], target, every)
    print(json.dumps({"board": str(target)}))
