"""A small local page for the operator: pick a home, see what it can do, write a script, make or replay.

    uv run python -m creature.ui ~/Desktop/creature-homes        # then open http://127.0.0.1:8765

It adds no authority: every action is one of the creature's own commands (`make`, `design`), run as a
child process exactly as from the terminal, one at a time. It listens on 127.0.0.1 only, serves files
only from a home's runs/ folder, and takes homes and designs only by names it listed itself.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from creature import ledger, registry

PORT = 8765
MAX_LINES = 12
MAX_LINE = 160
STYLE = Path(__file__).resolve().parents[1] / "examples" / "style-martin.json"
ASSETS = Path(os.environ.get("CREATURE_ASSETS", "~/Desktop/creature-assets")).expanduser()
FONTS = {  # the page's two fonts, from the asset library when it is there (else the system's)
    "barlow-regular.ttf": "pisma/barlow/Barlow-Regular.ttf",
    "barlow-semibold.ttf": "pisma/barlow/Barlow-SemiBold.ttf",
    "barlow-extrabold.ttf": "pisma/barlow/Barlow-ExtraBold.ttf",
    "jetbrainsmono.ttf": "pisma/jetbrainsmono/JetBrainsMono-Variable.ttf",
}
SHOWN = {".mp4": "video/mp4", ".html": "text/html; charset=utf-8", ".md": "text/plain; charset=utf-8",
         ".png": "image/png"}  # fmt: skip


class Desk:
    """The state behind the page: the folder of homes, and the one job that may run."""

    def __init__(self, homes: Path) -> None:
        self.homes = homes.expanduser().resolve()
        self.lock = threading.Lock()
        self.jobs: dict[str, dict[str, Any]] = {}

    def home_names(self) -> list[str]:
        return sorted(
            p.name
            for p in self.homes.iterdir()
            if (p / "authority.json").is_file() and (p / "registry").is_dir()
        )

    def home(self, name: str) -> Path:
        if name not in self.home_names():
            raise KeyError(f"no home {name!r}")
        return self.homes / name

    def describe(self, name: str) -> dict[str, Any]:
        root = self.home(name)
        skills = []
        for skill in registry.skills(root / "registry"):
            if skill.capability.get("kind") == "tool":
                continue
            card = registry.card(skill) or {}
            skills.append({
                "slug": skill.slug, "effect": skill.capability.get("effect", "")[:160],
                "colors": [c.get("hex") for c in card.get("colors", [])], "tone": card.get("tone"),
                "text_share": card.get("text_share"),
                "seconds": skill.capability.get("output", {}).get("duration_s"),
            })  # fmt: skip
        designs = []
        for design in registry.designs(root / "registry"):
            data = registry.design(root / "registry", design)
            if "steps" in data:
                designs.append({"name": design, "steps": len(data["steps"])})
        return {"name": name, "skills": skills, "designs": designs, "runs": self.runs(root)}

    def runs(self, root: Path, last: int = 12) -> list[dict[str, Any]]:
        found = []
        for path in sorted((root / "runs").glob("*.jsonl"), reverse=True)[:last]:
            events = ledger.read(path)
            end = next((e for e in events if e["type"] == "run_end"), None)
            task = next((e for e in events if e["type"].endswith("task") or e["type"] == "task"), {})
            what = task.get("lines") or task.get("text") or task.get("design") or task.get("source") or ""
            what = (
                " | ".join(what)
                if isinstance(what, list)
                else Path(str(what)).name
                if "/" in str(what)
                else what
            )
            clip = f"{path.stem}/{end['clip']}" if end and end.get("clip") else None
            found.append({
                "run": path.stem, "status": end["status"] if end else "STOPPED / RUNNING",
                "spent": (end or {}).get("spent_usd"), "what": str(what)[:120], "clip": clip,
                "gap": ((end or {}).get("gap") or "")[:300],
            })  # fmt: skip
        return found

    def start(self, kind: str, data: dict[str, Any]) -> str:
        """One job at a time: `make` from lines, or `design` replay with one text per step."""
        if not self.lock.acquire(blocking=False):
            raise RuntimeError("a job is already running; wait for it")
        try:
            root = self.home(str(data.get("home", "")))
            lines = [str(x).strip()[:MAX_LINE] for x in data.get("lines", []) if str(x).strip()][:MAX_LINES]
            if not lines:
                raise ValueError("write at least one line")
            command = [sys.executable, "-m", "creature", "--home", str(root)]
            if kind == "make":
                script = Path(tempfile.mkdtemp(prefix="creature-ui-")) / "script.txt"
                script.write_text("\n".join(lines) + "\n", encoding="utf-8")
                command += ["make", "--script", str(script)]
                if data.get("style") and STYLE.is_file():
                    command += ["--style", str(STYLE)]
                if not data.get("sound"):
                    command.append("--silent")
            elif kind == "design":
                name = str(data.get("design", ""))
                if name not in registry.designs(root / "registry"):
                    raise KeyError(f"no design {name!r}")
                command += ["design", name]
                for line in lines:
                    command += ["--text", line]
                if not data.get("sound"):
                    command.append("--silent")
            else:
                raise ValueError(f"unknown job {kind!r}")
        except Exception:
            self.lock.release()
            raise
        job = uuid.uuid4().hex[:8]
        self.jobs[job] = {
            "kind": kind,
            "home": root.name,
            "state": "running",
            "started": time.time(),
            "out": "",
            "lines": len(lines),
            "texts": lines,
            "known": sorted(p.name for p in (root / "runs").glob("*.jsonl")),  # the run is the new one
        }
        threading.Thread(target=self._run, args=(job, command), daemon=True).start()
        return job

    def _run(self, job: str, command: list[str]) -> None:
        try:
            done = subprocess.run(command, capture_output=True, text=True, cwd=str(STYLE.parents[1]))
            out = "\n".join(
                line for line in (done.stdout + done.stderr).splitlines()
                if not line.startswith(("Using ", "Creating ", "Installed "))
            )  # fmt: skip
            self.jobs[job].update(out=out[-4000:], **_result(out, self.homes))
            self.jobs[job]["state"] = "done"
        except Exception as error:  # the page shows it; the server keeps running
            self.jobs[job].update(state="done", out=f"{type(error).__name__}: {error}")
        finally:
            self.lock.release()

    def current(self) -> dict[str, Any]:
        """The job still running, if any, so a reloaded page picks it up again."""
        for job, found in self.jobs.items():
            if found["state"] == "running":
                return {
                    "job": job,
                    "kind": found["kind"],
                    "home": found["home"],
                    "lines": found.get("texts", []),
                }
        return {}

    def job(self, job: str) -> dict[str, Any]:
        """A job as the page sees it: its state, and the progress read from its run's own ledger."""
        found = self.jobs.get(job)
        if found is None:
            return {"state": "unknown"}
        runs = self.homes / found["home"] / "runs"
        if found.get("run"):
            path = runs / f"{found['run']}.jsonl"
        else:
            new = sorted(p for p in runs.glob("*.jsonl") if p.name not in found["known"])
            path = new[-1] if new else None
        progress = progress_of(ledger.read(path), found["lines"]) if path and path.is_file() else {}
        return {k: v for k, v in found.items() if k != "known"} | {"progress": progress}

    def run(self, home: str, run: str) -> dict[str, Any]:
        """One finished run as the page shows it: state, clip, progress (cost, calls, picks), and for each
        skill it used the reel that skill was learned from, with that skill's learning cost."""
        root = self.home(home)
        path = root / "runs" / f"{run}.jsonl"
        if not RUN_ID.fullmatch(run) or not path.is_file():
            raise KeyError(f"no run {run!r}")
        events = ledger.read(path)
        task = next((e for e in events if e["type"] in ("make_task", "design_task")), {})
        lines = task.get("lines") or task.get("texts") or [task.get("text", "")]
        end = next((e for e in events if e["type"] == "run_end"), {})
        progress = progress_of(events, len(lines))
        used = [p["skill"] for p in progress["picks"] if p.get("skill")]
        used += [e["skill"] for e in events if e["type"] == "design_step" and e.get("skill")]
        learned = []
        for slug in dict.fromkeys(used):
            reel = self.reel(home, slug)
            if reel:
                learned.append(reel)
        return {
            "kind": "make" if task.get("type") == "make_task" else "design", "run": run, "lines": lines,
            "status": end.get("status", "STOPPED"),
            "clip": f"{run}/{end['clip']}" if end.get("clip") else None,
            "gap": (end.get("gap") or "")[:300], "progress": progress, "learned": learned,
            "seconds": _seconds(events[0].get("ts"), end.get("ts")) if events and end else None,
        }  # fmt: skip

    def reel(self, home: str, slug: str) -> dict[str, Any] | None:
        """Where a skill came from: its reel (served by /reel when the file is still on this machine), the
        run that learned it, and what learning it cost."""
        try:
            skill = registry.get(self.home(home) / "registry", slug)
        except (KeyError, ValueError):
            return None
        origin, cost = skill.capability.get("origin", {}), skill.capability.get("cost", {})
        source = Path(str(origin.get("reel", "")))
        return {
            "skill": slug, "reel": source.stem, "has_video": source.suffix == ".mp4" and source.is_file(),
            "learn_usd": cost.get("learn_usd"), "attempts": cost.get("forge_attempts"),
        }  # fmt: skip

    def reel_file(self, home: str, slug: str) -> bytes:
        """The reel a skill of this home was learned from: the path comes from its sealed record only."""
        skill = registry.get(self.home(home) / "registry", slug)
        source = Path(str(skill.capability.get("origin", {}).get("reel", "")))
        if source.suffix != ".mp4" or not source.is_file():
            raise PermissionError("the reel is not on this machine")
        return source.read_bytes()

    def file(self, home: str, path: str) -> tuple[bytes, str]:
        """A file from the home's runs/ only: an mp4, a run page, a POSTUP.md or a picture."""
        runs = (self.home(home) / "runs").resolve()
        wanted = (runs / path).resolve()
        if runs not in wanted.parents or wanted.suffix not in SHOWN or not wanted.is_file():
            raise PermissionError("not a file of this home's runs")
        return wanted.read_bytes(), SHOWN[wanted.suffix]


RUN_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}")


def _seconds(start: str | None, end: str | None) -> float | None:
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except (TypeError, ValueError):
        return None


def progress_of(events: list[dict[str, Any]], lines: int) -> dict[str, Any]:
    """Where a make or a replay is, from its ledger: one word, a step of n, the model calls so far, and
    for a make the skill picked for each line and why (from its last composition)."""
    kinds = [e["type"] for e in events]
    calls = sum(1 for e in events if e["type"] == "model_call")
    choices = [e for e in events if e["type"] == "make_choice"]
    picks = (
        [{"line": s["line"], "skill": s["skill"], "why": s.get("why", "")} for s in choices[-1]["shots"]]
        if choices
        else []
    )
    attempt = choices[-1]["attempt"] if choices else 0
    since = 0
    if choices:
        since = max(i for i, k in enumerate(kinds) if k == "make_choice")
    shots = sum(1 for e in events[since:] if e["type"] in ("make_shot", "design_step"))
    end = next((e for e in events if e["type"] == "run_end"), None)
    if end:
        word = end["status"]
    elif "make_judged" in kinds[since:]:
        word = "JUDGED"
    elif "design_join" in kinds[since:]:
        word = "JUDGING" if choices else "JOINING"
    elif shots or "make_choice" in kinds or "design_task" in kinds:
        word = "RENDERING"
    elif "card" in kinds or "make_task" in kinds:
        word = "READING SKILLS"
    else:
        word = "STARTING"
    return {
        "word": word, "step": min(shots, lines), "of": lines, "attempt": attempt, "calls": calls,
        "spent": round(sum(float(e.get("cost_usd") or 0) for e in events), 4), "picks": picks,
    }  # fmt: skip


def _result(out: str, homes: Path) -> dict[str, Any]:
    """What a finished command printed: its status, the clip and the run, as paths inside runs/."""
    found: dict[str, Any] = {"status": (out.strip().splitlines() or [""])[0].split(" ")[0]}
    for line in out.splitlines():
        for key in ("clip", "run", "spent", "gap"):
            if line.startswith(f"{key}: "):
                found[key] = line[len(key) + 2 :].strip()
    for key in ("clip", "run"):
        if key in found and "/runs/" in found[key]:
            found[key] = found[key].split("/runs/", 1)[1]
    return found


def allowed(headers: Mapping[str, str], port: int, *, post: bool) -> bool:
    """Only the desk's own page may use it. Host must be this server (no DNS rebinding); a POST must come
    from this page's own origin and carry JSON, which a form or a simple request from another site cannot."""
    own = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if headers.get("Host") not in own:
        return False
    if not post:
        return True
    kind = (headers.get("Content-Type") or "").split(";")[0].strip()
    return headers.get("Origin") in {f"http://{h}" for h in own} and kind == "application/json"


class Handler(BaseHTTPRequestHandler):
    desk: Desk

    def log_message(self, *args: Any) -> None:  # quiet terminal
        return

    def _send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if not allowed(self.headers, self.server.server_address[1], post=False):
            self._json({"error": "not the desk's own page"}, 403)
            return
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif url.path == "/api/homes":
                self._json(self.desk.home_names())
            elif url.path == "/api/run":
                self._json(self.desk.run(query.get("home", ""), query.get("run", "")))
            elif url.path == "/reel":
                self._send(
                    200, self.desk.reel_file(query.get("home", ""), query.get("skill", "")), "video/mp4"
                )
            elif url.path == "/api/home":
                self._json(self.desk.describe(query.get("name", "")))
            elif url.path == "/api/current":
                self._json(self.desk.current())
            elif url.path == "/api/job":
                self._json(self.desk.job(query.get("id", "")))
            elif url.path.startswith("/font/") and url.path[6:] in FONTS:
                path = ASSETS / FONTS[url.path[6:]]
                if not path.is_file():
                    raise PermissionError("font not in the asset library")
                self._send(200, path.read_bytes(), "font/ttf")
            elif url.path == "/file":
                body, kind = self.desk.file(query.get("home", ""), query.get("path", ""))
                self._send(200, body, kind)
            else:
                self._json({"error": "not found"}, 404)
        except (KeyError, PermissionError, ValueError) as error:
            self._json({"error": str(error)}, 400)

    def do_POST(self) -> None:
        if not allowed(self.headers, self.server.server_address[1], post=True):
            self._json({"error": "not the desk's own page"}, 403)
            return
        url = urlparse(self.path)
        try:
            size = min(int(self.headers.get("Content-Length", 0)), 64_000)
            data = json.loads(self.rfile.read(size) or b"{}")
            kind = {"/api/make": "make", "/api/design": "design"}.get(url.path)
            if kind is None:
                self._json({"error": "not found"}, 404)
                return
            self._json({"job": self.desk.start(kind, data)})
        except (KeyError, RuntimeError, ValueError) as error:
            self._json({"error": str(error)}, 400)


def serve(homes: Path, port: int = PORT) -> None:
    Handler.desk = Desk(homes)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"creature ui: http://127.0.0.1:{port}  (homes: {Handler.desk.homes})  Ctrl+C to stop")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


PAGE = (Path(__file__).with_name("ui.html")).read_text(encoding="utf-8")  # the page: plain HTML, no build


if __name__ == "__main__":
    serve(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("~/Desktop/creature-homes"),
          int(sys.argv[2]) if len(sys.argv) > 2 else PORT)  # fmt: skip
