"""The board (creature/board.py) reads ledgers and files only; its numbers must be the ledgers' numbers:
spent is the sum of cost_usd, learned is the installed events, a run without run_end is RUNNING until
it has been quiet for STALE_S, the picture is the newest pairs sheet (else strip), and every path on the
page resolves from the board's folder, for homes inside it and outside it."""

import os
import re

import pytest

from creature import board
from creature.ledger import Ledger

PNG = b"\x89PNG\r\n\x1a\n"


def _home(root, name):
    home = root / name
    (home / "runs").mkdir(parents=True)
    return home


def _run(home, run_id, *events, source="reel.mp4"):
    """A ledger with run_start, a task, the given events; the run's folder next to it."""
    log = Ledger.start(home, run_id=run_id)
    log.record("run_start", fingerprint="f")
    log.record("task", source=source, text="Stay curious.")
    for kind, fields in events:
        log.record(kind, **fields)
    folder = home / "runs" / run_id
    folder.mkdir(exist_ok=True)
    return folder


def _page(homes, out):
    board.render(homes, out)
    return out.read_text(encoding="utf-8")


def _refs(text):
    return re.findall(r"(?:src|href)='([^']*)'", text)


def test_spent_and_learned_are_the_ledgers_numbers(tmp_path):
    base = tmp_path / "creature-homes"
    home = _home(base, "one")
    _run(
        home, "r1",
        ("model_call", {"step": "criteria", "cost_usd": 0.1, "model": "m"}),
        ("perceive_apify", {"cost_usd": 0.0026}),
        ("installed", {"skill": "typewriter", "version": 1, "learn_usd": 0.1026}),  # derived, not a cost
        ("installed", {"skill": "slide", "version": 2, "learn_usd": 0.0}),
        ("run_end", {"status": "BUILT"}),
    )  # fmt: skip
    _run(
        home,
        "r2",
        ("model_call", {"step": "planner", "cost_usd": 0.02, "model": "m"}),
        ("run_end", {"status": "FAILED", "gap": "no"}),
    )
    [one, two] = sorted(board.runs([home]), key=lambda r: r["id"])
    assert one["spent"] == pytest.approx(0.1026) and two["spent"] == pytest.approx(0.02)
    assert one["learned"] == ["typewriter v1", "slide v2"] and two["learned"] == []
    text = _page([home], base / "board.html")
    assert "<b>2</b>naučeno" in text and "<b>$0.12</b>utraceno" in text and "<b>2</b>běhů" in text
    assert "Chybí: no" in text


def test_a_run_without_an_end_is_running_until_it_goes_quiet(tmp_path, monkeypatch):
    base = tmp_path / "creature-homes"
    home = _home(base, "one")
    _run(home, "going", ("round", {"round": 1, "note": "first"}))
    _run(home, "ended", ("run_end", {"status": "DONE"}))
    by_id = {r["id"]: r for r in board.runs([home])}
    assert by_id["going"]["status"] == "RUNNING" and by_id["ended"]["status"] == "DONE"
    assert next(r["id"] for r in board.runs([home])) == "going"  # running runs come first
    later = board.time.time() + board.STALE_S + 1
    monkeypatch.setattr(board.time, "time", lambda: later)
    assert {r["id"]: r["status"] for r in board.runs([home])} == {"going": "STOPPED", "ended": "DONE"}
    text = _page([home], base / "board.html")
    assert "<b>0</b>běží" in text


def test_the_picture_is_the_newest_pairs_sheet_else_the_strip_and_a_clip_is_played(tmp_path):
    base = tmp_path / "creature-homes"
    home = _home(base, "one")
    folder = _run(home, "rounds", ("round", {"round": 2, "note": "n"}))
    for name, age in (
        ("rounds/round-01/pairs.png", 300),
        ("rounds/round-02/pairs.png", 100),
        ("attempt-1/strip.png", 10),
    ):
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        os.utime(path, (board.time.time() - age, board.time.time() - age))
    assert board._latest_picture(folder) == folder / "rounds" / "round-02" / "pairs.png"
    (folder / "rounds" / "round-01" / "pairs.png").unlink()
    (folder / "rounds" / "round-02" / "pairs.png").unlink()
    assert board._latest_picture(folder) == folder / "attempt-1" / "strip.png"
    done = _run(home, "clip", ("run_end", {"status": "BUILT", "clip": "attempt-1/clip.mp4"}))
    (done / "attempt-1").mkdir()
    (done / "attempt-1" / "clip.mp4").write_bytes(b"mp4")
    text = _page([home], base / "board.html")
    assert "<video src='one/runs/clip/attempt-1/clip.mp4'" in text
    assert "<img src='one/runs/rounds/attempt-1/strip.png'" in text


def test_every_path_resolves_from_the_boards_folder_for_homes_inside_and_outside_it(tmp_path):
    base = tmp_path / "creature-homes"
    inside = _home(base, "inside")
    outside = _home(tmp_path / "elsewhere", "outside")
    for home in (inside, outside):
        folder = _run(home, "r", ("run_end", {"status": "BUILT", "clip": "clip.mp4"}))
        (folder / "clip.mp4").write_bytes(b"mp4")
        (folder / "page.html").write_text("<p>run</p>")
        (folder / "reel-strip.png").write_bytes(PNG)
    out = base / "board.html"
    text = _page([inside, outside], out)
    refs = _refs(text)
    assert refs and all((out.parent / ref).exists() for ref in refs), refs
    assert any(ref.startswith("../elsewhere/outside/") for ref in refs)
    assert str(tmp_path) not in text  # no absolute paths on the page


def test_a_run_with_no_task_event_still_shows(tmp_path):
    # `creature use` records run_start, tool_used and run_end and no task: the board must show it as a
    # run of its own, never die on it (one bad run would freeze the live page for every home)
    base = tmp_path / "creature-homes"
    home = _home(base, "one")
    log = Ledger.start(home, run_id="used")
    log.record("run_start", fingerprint="f")
    log.record("tool_used", skill="loop-seam-check", version=1, ok=True)
    log.record("run_end", status="DONE", skill="use:loop-seam-check")
    (home / "runs" / "used").mkdir()
    [run] = board.runs([home])
    assert run["status"] == "DONE" and run["spent"] == 0
    text = _page([home], base / "board.html")
    assert "used" in text and "DONE" in text
