"""A small local page for the operator: pick a home, see what it can do, write a script, make or replay.

    uv run python -m creature.ui ~/Desktop/creature-homes        # then open http://127.0.0.1:8765

It adds no authority: every action is one of the creature's own commands (`make`, `design`), run as a
child process exactly as from the terminal, one at a time. It listens on 127.0.0.1 only, serves files
only from a home's runs/ folder, and takes homes and designs only by names it listed itself.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from creature import ledger, registry

PORT = 8765
MAX_LINES = 12
MAX_LINE = 160
STYLE = Path(__file__).resolve().parents[1] / "examples" / "style-martin.json"
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

    def runs(self, root: Path, last: int = 8) -> list[dict[str, Any]]:
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
            elif kind == "design":
                name = str(data.get("design", ""))
                if name not in registry.designs(root / "registry"):
                    raise KeyError(f"no design {name!r}")
                command += ["design", name]
                for line in lines:
                    command += ["--text", line]
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
            self.jobs[job].update(state="done", out=out[-4000:], **_result(out, self.homes))
        except Exception as error:  # the page shows it; the server keeps running
            self.jobs[job].update(state="done", out=f"{type(error).__name__}: {error}")
        finally:
            self.lock.release()

    def file(self, home: str, path: str) -> tuple[bytes, str]:
        """A file from the home's runs/ only: an mp4, a run page, a POSTUP.md or a picture."""
        runs = (self.home(home) / "runs").resolve()
        wanted = (runs / path).resolve()
        if runs not in wanted.parents or wanted.suffix not in SHOWN or not wanted.is_file():
            raise PermissionError("not a file of this home's runs")
        return wanted.read_bytes(), SHOWN[wanted.suffix]


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
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif url.path == "/api/homes":
                self._json(self.desk.home_names())
            elif url.path == "/api/home":
                self._json(self.desk.describe(query.get("name", "")))
            elif url.path == "/api/job":
                self._json(self.desk.jobs.get(query.get("id", ""), {"state": "unknown"}))
            elif url.path == "/file":
                body, kind = self.desk.file(query.get("home", ""), query.get("path", ""))
                self._send(200, body, kind)
            else:
                self._json({"error": "not found"}, 404)
        except (KeyError, PermissionError, ValueError) as error:
            self._json({"error": str(error)}, 400)

    def do_POST(self) -> None:
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
