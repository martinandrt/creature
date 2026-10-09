"""The board: one page over every home and every run, live. Input, output, time, cost, what was learned,
and for a run still going its latest steps and the latest picture of reel next to output. No model; it only
reads ledgers and files.

Every run of the same reel (the source file name, in any home, any number of times) is one section with
tabs V1..Vn, oldest to newest, the newest shown; sections go by their newest run, newest first.

The page never reloads. OUT.html is a static shell (style, header, an empty container, a small script);
OUT-data.js sits next to it and holds `window.BOARD_DATA`: the summary and one {id, sig, html} per reel, sig
being a short hash of that section's html. The script injects the data file as a <script> every POLL_S seconds
(works on file://, unlike fetch) and patches the page in place: a section whose sig is unchanged is not
touched, so a playing video keeps playing, the scroll position stays and so does the tab the viewer picked.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from creature import ledger

POLL_S = 10  # how often the open page asks for fresh data; the page itself is never reloaded
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
display:grid;grid-template-columns:minmax(0,.7fr) minmax(0,1fr) minmax(0,1fr) minmax(0,1.2fr);gap:12px}
@media (max-width:900px){.run{grid-template-columns:1fr}}
.head{grid-column:1/-1;display:flex;gap:12px;flex-wrap:wrap;align-items:baseline}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 8px}
.tab{font:inherit;font-weight:700;color:var(--fg);background:var(--card);border:1px solid var(--line);
border-radius:6px;padding:2px 10px;cursor:pointer}
.tab.on{border-color:currentColor;box-shadow:inset 0 -2px currentColor}
.st{font-weight:700}.BUILT,.HAVE,.DONE{color:var(--ok)}.FAILED,.REFUSE,.STOPPED{color:var(--bad)}.RUNNING,.ASK{color:var(--run)}
.mute{color:var(--mute)}img,video{max-width:100%;max-height:360px;border-radius:6px;background:#000;display:block}
ol{margin:4px 0 0 18px;padding:0}li{margin:0 0 2px}a{color:inherit}
#err{margin:0 0 12px}#err:empty{display:none}
"""

PLAYER = "controls muted loop playsinline preload='metadata'"  # many runs on one page: first frame only
OUTPUT_PLAYER = (
    "controls loop playsinline preload='metadata'"  # the creature's clip may carry sound: not muted
)


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


def _design_origin(home: Path, name: str) -> tuple[str | None, Path | None]:
    """The reel a saved design was learned from, and that run's copy of it: a replay belongs with it."""
    try:
        index = json.loads((home / "registry" / "index.json").read_text(encoding="utf-8"))
        origin = index["designs"][name]["origin"]
        return Path(str(origin["reel"])).name, home / "runs" / str(origin["run"]) / "reel.mp4"
    except (OSError, ValueError, KeyError, TypeError):
        return None, None


def runs(homes: list[Path], now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
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
            design = str(task.get("design") or "") if task.get("type") == "design_task" else ""
            origin, origin_video = _design_origin(home, design) if design else (None, None)
            found.append(
                {
                    "home": home.name,
                    "id": path.stem,
                    "folder": folder,
                    "start": _when(events[0]["ts"]),
                    "last": _when(events[-1]["ts"]),
                    "status": end["status"]
                    if end
                    else ("RUNNING" if now - _when(events[-1]["ts"]) < STALE_S else "STOPPED"),
                    "source": Path(
                        str(
                            task.get("source")
                            or origin
                            or task.get("design")
                            or task.get("wish")
                            or task.get("type")
                            or (end or {}).get("skill")
                            or "run"
                        )
                    ).name,
                    "text": str(task.get("text", "")),
                    "design": design,
                    "reel_video": origin_video,
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


SCRIPT = """
(function () {
  var SRC = __SRC__, EVERY = __EVERY__ * 1000;
  var runsEl = document.getElementById('runs'), sumEl = document.getElementById('sum');
  var updEl = document.getElementById('upd'), homesEl = document.getElementById('homes');
  var errEl = document.getElementById('err'), lastSummary = null;
  var chosen = {};  // group id -> run the viewer picked; without a pick the newest run is shown

  // Show one run of a group (tab on, panel visible); a clip in a panel that is hidden now is paused.
  function show(g, run) {
    Array.prototype.forEach.call(g.querySelectorAll('.tab'), function (b) {
      b.classList.toggle('on', b.getAttribute('data-run') === run);
    });
    Array.prototype.forEach.call(g.querySelectorAll('.panel'), function (p) {
      var on = p.getAttribute('data-run') === run;
      p.hidden = !on;
      if (!on) Array.prototype.forEach.call(p.querySelectorAll('video'), function (v) { v.pause(); });
    });
  }

  // After a group was built: the run the viewer picked if it is still there, else the one the html shows.
  function restore(g, id) {
    var panels = Array.prototype.slice.call(g.querySelectorAll('.panel'));
    var names = panels.map(function (p) { return p.getAttribute('data-run'); });
    if (chosen[id] !== undefined && names.indexOf(chosen[id]) < 0) delete chosen[id];
    var shown = g.querySelector('.panel:not([hidden])');
    show(g, chosen[id] !== undefined ? chosen[id] : shown && shown.getAttribute('data-run'));
  }

  runsEl.addEventListener('click', function (ev) {
    var b = ev.target.closest && ev.target.closest('button.tab');
    var g = b && b.closest('[data-id]');
    if (!g) return;
    chosen[g.getAttribute('data-id')] = b.getAttribute('data-run');
    show(g, b.getAttribute('data-run'));
  });

  // The group's <section> from its html. Parsed in an inert document so nothing loads until it is imported;
  // a <video> the old section already has (same src) is carried over instead of being created again,
  // so a changed section (its minutes tick) does not restart the clip that is playing.
  function build(r, old) {
    var sec = new DOMParser().parseFromString(r.html, 'text/html').body.firstElementChild;
    var held = [];
    if (old) {
      var olds = Array.prototype.slice.call(old.querySelectorAll('video'));
      Array.prototype.slice.call(sec.querySelectorAll('video')).forEach(function (v) {
        var src = v.getAttribute('src');
        var i = olds.findIndex(function (o) { return o && o.getAttribute('src') === src; });
        if (i < 0) return;
        var mark = sec.ownerDocument.createElement('i');
        mark.setAttribute('data-keep', held.length);
        held.push(olds[i]);
        olds[i] = null;
        v.parentNode.replaceChild(mark, v);
      });
    }
    var el = document.importNode(sec, true);
    Array.prototype.slice.call(el.querySelectorAll('i[data-keep]')).forEach(function (m) {
      m.parentNode.replaceChild(held[+m.getAttribute('data-keep')], m);
    });
    el.setAttribute('data-id', r.id);
    el.setAttribute('data-sig', r.sig);
    restore(el, r.id);
    return el;
  }

  function apply(d) {
    errEl.textContent = d.error ? 'Přehled se nepodařilo nakreslit: ' + d.error : '';
    updEl.textContent = 'aktualizováno ' + d.updated;
    if (!d.runs) return;  // a failed render: keep what is on the page, say why
    homesEl.textContent = ' · domovy: ' + d.homes;
    if (d.summary !== lastSummary) { sumEl.innerHTML = d.summary; lastSummary = d.summary; }
    var have = new Map(), want = new Set();
    Array.prototype.forEach.call(runsEl.children, function (el) {
      have.set(el.getAttribute('data-id'), el);
    });
    d.runs.forEach(function (r) { want.add(r.id); });
    have.forEach(function (el, id) { if (!want.has(id)) { runsEl.removeChild(el); have.delete(id); } });
    var prev = null;
    d.runs.forEach(function (r) {
      var el = have.get(r.id);
      if (!el) {
        el = build(r, null);
      } else if (el.getAttribute('data-sig') !== r.sig) {
        var fresh = build(r, el);
        runsEl.replaceChild(fresh, el);
        el = fresh;
      }
      var at = prev ? prev.nextSibling : runsEl.firstChild;
      if (el !== at) runsEl.insertBefore(el, at);
      prev = el;
    });
  }

  function load() {
    var s = document.createElement('script');
    window.BOARD_DATA = null;
    s.charset = 'utf-8';
    s.src = SRC + '?t=' + Date.now();
    function done(ok) {
      s.remove();
      try {
        if (ok && window.BOARD_DATA) apply(window.BOARD_DATA);
        else errEl.textContent = 'Data se nepodařilo načíst, zkouším znovu.';
      } catch (e) {
        errEl.textContent = 'Stránku se nepodařilo upravit: ' + e;
      }
      setTimeout(load, EVERY);
    }
    s.onload = function () { done(true); };
    s.onerror = function () { done(false); };
    document.head.appendChild(s);
  }
  load();
})();
"""


def data_path(out: Path) -> Path:
    """The data file that belongs to a board page: board.html -> board-data.js, next to it."""
    return out.with_name(f"{out.stem}-data.js")


def shell(out: Path) -> str:
    """The static page: no run in it, nothing that changes with time. Same text for the same OUT."""
    script = SCRIPT.replace("__SRC__", json.dumps(data_path(out).name)).replace("__EVERY__", str(POLL_S))
    return (
        "<!doctype html><html lang='cs'><head><meta charset='utf-8'><meta name='viewport' "
        "content='width=device-width,initial-scale=1'>"
        f"<title>Tvor naživo</title><style>{STYLE}</style></head><body>"
        "<h1>Tvor naživo</h1>"
        "<p class='sub'><span id='upd'>načítám…</span><span id='homes'></span></p>"
        "<p id='err' class='FAILED'></p><div class='sum' id='sum'></div><div id='runs'></div>"
        f"<script>{script}</script></body></html>"
    )


def _sig(text: str) -> str:
    return hashlib.blake2s(text.encode("utf-8"), digest_size=6).hexdigest()


def _section(r: dict[str, Any], base: Path, now: float) -> str:
    seconds = (now if r["status"] == "RUNNING" else r["last"]) - r["start"]
    page = _rel(r["folder"] / "page.html", base)
    reel = _rel(r["folder"] / "reel-strip.png", base)
    clip = _rel(r["clip"], base) if r["clip"] else None
    picture = None if clip else _rel(_latest_picture(r["folder"]), base)
    head = (
        f"<div class='head'><span class='st {r['status']}'>{r['status']}</span>"
        f"<b>{html.escape(r['source'])}</b><span class='mute'>{html.escape(r['home'])} · {r['id']}</span>"
        f"<span>{seconds / 60:.1f} min</span><span>${r['spent']:.3f}</span>"
        + (f"<span>přehrání designu {html.escape(r['design'])}</span>" if r.get("design") else "")
        + (f"<span>naučeno: {html.escape(', '.join(r['learned']))}</span>" if r["learned"] else "")
        + (f"<a href='{html.escape(page)}'>stránka běhu</a>" if page else "")
        + "</div>"
    )
    original = _rel(r.get("reel_video") or r["folder"] / "reel.mp4", base)
    source = (
        "<div><div class='mute'>Zdrojové video</div>"
        + (
            f"<video src='{html.escape(original)}' {PLAYER}></video>"
            if original
            else "<div class='mute'>není v běhu</div>"
        )
        + "</div>"
    )
    left = (
        "<div><div class='mute'>Vstup (snímky)</div>"
        + (f"<img src='{html.escape(reel)}' alt='snímky reelu'>" if reel else "")
        + (f"<div>„{html.escape(r['text'][:160])}“</div>" if r["text"] else "")
        + "</div>"
    )
    if clip:
        player = f"<video src='{html.escape(clip)}' {OUTPUT_PLAYER}></video>"
        middle = f"<div><div class='mute'>Výstup</div>{player}</div>"
    elif picture:
        shown = f"<img src='{html.escape(picture)}' alt=''>"
        middle = f"<div><div class='mute'>Poslední obrázek (reel | výstup)</div>{shown}</div>"
    elif r["status"] == "STOPPED":
        why = "žádný: proces skončil bez závěru (přerušený běh), poslední krok je v postupu"
        middle = f"<div><div class='mute'>Výstup</div><div class='mute'>{why}</div></div>"
    else:
        middle = "<div><div class='mute'>Výstup</div><div class='mute'>zatím nic</div></div>"
    steps = "".join(f"<li>{html.escape(s)}</li>" for s in r["steps"][-STEPS_SHOWN:])
    gap = f"<div class='mute'>Chybí: {html.escape(r['gap'][:240])}</div>" if r["gap"] else ""
    right = f"<div><div class='mute'>Postup</div><ol>{steps}</ol>{gap}</div>"
    return f"<section class='run'>{head}{source}{left}{middle}{right}</section>"


def _group(members: list[tuple[str, dict[str, Any]]], base: Path, now: float) -> str:
    """Every run of one reel in one section: tabs V1..Vn oldest to newest, the newest shown."""
    last = len(members) - 1
    tabs = ""
    if last:
        buttons = "".join(
            f"<button type='button' class='tab {r['status']}{' on' if n == last else ''}' "
            f"data-run='{html.escape(key)}' title='{html.escape(key)}'>V{n + 1}</button>"
            for n, (key, r) in enumerate(members)
        )
        tabs = f"<div class='tabs'>{buttons}</div>"
    panels = "".join(
        f"<div class='panel' data-run='{html.escape(key)}'{'' if n == last else ' hidden'}>"
        f"{_section(r, base, now)}</div>"
        for n, (key, r) in enumerate(members)
    )
    return f"<section class='grp'>{tabs}{panels}</section>"


def snapshot(homes: list[Path], base: Path, now: float) -> dict[str, Any]:
    """Everything the page shows, as data. A pure function of the ledgers, the files and `now`.

    One entry per reel (the source file name), however many homes or times it was run; its runs are the
    tabs. Entries go by their newest run, newest first. An entry's sig covers all of its runs."""
    items = runs(homes, now)
    spent = sum(r["spent"] for r in items)
    learned = sum(len(r["learned"]) for r in items)
    going = sum(r["status"] == "RUNNING" for r in items)
    by_source: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    seen: set[str] = set()
    for r in sorted(items, key=lambda r: (r["start"], r["home"], r["id"])):  # oldest first
        key = f"{r['home']}/{r['id']}"
        while key in seen:  # two homes may hold a run of the same name
            key += "+"
        seen.add(key)
        by_source.setdefault(r["source"], []).append((key, r))
    listed = []
    for source, members in sorted(by_source.items(), key=lambda kv: (-kv[1][-1][1]["start"], kv[0])):
        group = _group(members, base, now)
        listed.append({"id": f"src:{source}", "sig": _sig(group), "html": group})
    return {
        "updated": f"{datetime.fromtimestamp(now):%H:%M:%S}",
        "homes": ", ".join(h.name for h in homes),
        "summary": f"<div><b>{len(items)}</b>běhů</div><div><b>{going}</b>běží</div>"
        f"<div><b>{learned}</b>naučeno</div><div><b>${spent:.2f}</b>utraceno</div>",
        "runs": listed,
    }


def _write(path: Path, text: str) -> None:
    """Atomic: the page never loads half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_if_changed(path: Path, text: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == text:
            return
    except (OSError, ValueError):
        pass
    _write(path, text)


def _write_data(out: Path, data: dict[str, Any]) -> None:
    # ASCII-only JSON: valid JavaScript whatever encoding the browser guesses for the file
    _write(data_path(out), "window.BOARD_DATA = " + json.dumps(data, separators=(",", ":")) + ";\n")


def render(homes: list[Path], out: Path, now: float | None = None) -> Path:
    now = time.time() if now is None else now
    _write_if_changed(out, shell(out))
    _write_data(out, snapshot(homes, out.parent, now))
    return out


def main(homes: list[Path], out: Path, every: float | None) -> None:
    while True:
        try:
            render([h for h in homes if (h / "runs").is_dir()], out)
        except Exception as error:  # one bad ledger must not freeze the page: say so on it
            _write_if_changed(out, shell(out))
            _write_data(out, {"updated": f"{datetime.now():%H:%M:%S}", "error": repr(error)})
        if not every:
            return
        time.sleep(every)


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    every = float(args.pop(0)[len("--every=") :]) if args and args[0].startswith("--every=") else None
    target = Path(args.pop(0))
    main([Path(a) for a in args], target, every)
    print(json.dumps({"board": str(target), "data": str(data_path(target))}))
