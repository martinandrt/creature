"""The board (creature/board.py) reads ledgers and files only; its numbers must be the ledgers' numbers:
spent is the sum of cost_usd, learned is the installed events, a run without run_end is RUNNING until
it has been quiet for STALE_S, the picture is the newest pairs sheet (else strip), and every path on the
page resolves from the board's folder, for homes inside it and outside it. The page itself is a static shell
that never reloads; what it shows is in the data file next to it: one section per reel (a reel run several
times is one section, its runs the tabs V1..Vn), each with a sig."""

import json
import os
import re
import time

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


def _data(out):
    text = board.data_path(out).read_text(encoding="utf-8")
    assert text.startswith("window.BOARD_DATA = ") and text.endswith(";\n")
    return json.loads(text[len("window.BOARD_DATA = ") : -2])


def _page(homes, out):
    """What the viewer ends up with: the summary and every section from the data file."""
    board.render(homes, out)
    data = _data(out)
    return data["summary"] + "".join(r["html"] for r in data["runs"])


def _later():
    time.sleep(0.005)  # ledger timestamps are milliseconds; runs made in a test must differ


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


def test_the_shell_is_static_and_never_reloads(tmp_path):
    base = tmp_path / "creature-homes"
    home = _home(base, "one")
    _run(home, "r1", ("run_end", {"status": "BUILT"}))
    out = base / "board.html"
    board.render([home], out)
    shell = out.read_text(encoding="utf-8")
    assert "http-equiv" not in shell and "refresh" not in shell.lower() and "location.reload" not in shell
    assert "class='run'" not in shell and "r1" not in shell  # no run in it
    assert "<div id='runs'></div>" in shell and "board-data.js" in shell
    before = out.stat()
    time.sleep(0.01)
    _run(home, "r2", ("run_end", {"status": "FAILED"}))
    board.render([home], out)  # new runs, same shell: it is not even rewritten
    after = out.stat()
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)
    assert out.read_text(encoding="utf-8") == shell
    assert sorted(p.name for p in base.iterdir()) == ["board-data.js", "board.html", "one"]  # no temp files


def test_the_data_file_holds_every_run_with_an_id_and_a_sig(tmp_path):
    base = tmp_path / "creature-homes"
    one, two = _home(base, "one"), _home(base, "two")
    _run(one, "r1", ("run_end", {"status": "BUILT"}), source="a.mp4")
    _run(one, "r2", ("run_end", {"status": "BUILT"}), source="b.mp4")
    _run(two, "r3", ("run_end", {"status": "FAILED", "gap": "no"}), source="c.mp4")
    board.render([one, two], base / "board.html", now=1_700_000_000)
    data = _data(base / "board.html")
    assert re.fullmatch(r"\d\d:\d\d:\d\d", data["updated"]) and data["homes"] == "one, two"
    assert "<b>3</b>běhů" in data["summary"]
    ids = [r["id"] for r in data["runs"]]
    assert len(ids) == 3 and len(set(ids)) == 3
    for r in data["runs"]:
        assert set(r) == {"id", "sig", "html"} and r["sig"] == board._sig(r["html"])
        assert r["html"].startswith("<section") and r["html"].endswith("</section>")
    for needle in ("r1", "r2", "r3", "Chybí: no"):
        assert any(needle in r["html"] for r in data["runs"]), needle
    assert (
        board.data_path(base / "board.html").read_text(encoding="utf-8").isascii()
    )  # valid whatever the charset


def test_a_section_that_did_not_change_keeps_its_sig_between_renders(tmp_path):
    base = tmp_path / "creature-homes"
    home = _home(base, "one")
    _run(home, "r1", ("run_end", {"status": "BUILT"}), source="a.mp4")
    _run(home, "r2", ("round", {"round": 1, "note": "n"}), source="b.mp4")
    out = base / "board.html"
    now = board.time.time()
    board.render([home], out, now=now)
    first = {r["id"]: r["sig"] for r in _data(out)["runs"]}
    board.render([home], out, now=now)  # nothing happened: nothing differs
    assert {r["id"]: r["sig"] for r in _data(out)["runs"]} == first
    board.render([home], out, now=now + 3)  # a few seconds on: an ended run reads the same
    assert {r["id"]: r["sig"] for r in _data(out)["runs"]}["src:a.mp4"] == first["src:a.mp4"]
    # something happens in b only: b's sig moves, a's does not
    Ledger(home / "runs" / "r2.jsonl").record("round", round=2, note="second")  # reopened: the chain goes on
    board.render([home], out, now=now)
    third = {r["id"]: r["sig"] for r in _data(out)["runs"]}
    assert third["src:a.mp4"] == first["src:a.mp4"] and third["src:b.mp4"] != first["src:b.mp4"]


def test_render_is_a_pure_function_of_the_ledgers(tmp_path):
    base = tmp_path / "creature-homes"
    home = _home(base, "one")
    _run(home, "r1", ("model_call", {"step": "s", "cost_usd": 0.01, "model": "m"}), source="a.mp4")
    _run(home, "r2", ("run_end", {"status": "BUILT"}), source="b.mp4")
    out = base / "board.html"
    board.render([home], out, now=1_700_000_000)
    first = (out.read_bytes(), board.data_path(out).read_bytes())
    board.render([home], out, now=1_700_000_000)
    assert (out.read_bytes(), board.data_path(out).read_bytes()) == first
    # only the clock differs when `now` does: the header time; the ended run keeps its text
    board.render([home], out, now=1_700_000_000 + 4000)
    later = _data(out)
    now_data = json.loads(first[1].decode()[len("window.BOARD_DATA = ") : -2])
    assert later["updated"] != now_data["updated"] or later["runs"] != now_data["runs"]
    assert {r["id"]: r["html"] for r in later["runs"]}["src:b.mp4"] == {
        r["id"]: r["html"] for r in now_data["runs"]
    }["src:b.mp4"]


def test_runs_of_the_same_reel_are_one_group_with_tabs_oldest_to_newest(tmp_path):
    base = tmp_path / "creature-homes"
    one, two = _home(base, "one"), _home(base, "two")
    _run(one, "old", ("run_end", {"status": "FAILED"}), source="Dcs-MiOpO2V.mp4")
    _later()
    _run(two, "other", ("run_end", {"status": "BUILT"}), source="Other.mp4")
    _later()
    _run(two, "new", ("run_end", {"status": "BUILT"}), source="Dcs-MiOpO2V.mp4")
    out = base / "board.html"
    board.render([one, two], out)
    groups = _data(out)["runs"]
    assert [g["id"] for g in groups] == ["src:Dcs-MiOpO2V.mp4", "src:Other.mp4"]  # newest run first
    html = groups[0]["html"]
    assert html.count("<section class='run'>") == 2
    tabs = re.findall(r"<button[^>]*data-run='([^']*)'[^>]*>(V\d+)</button>", html)
    assert tabs == [("one/old", "V1"), ("two/new", "V2")]  # oldest to newest
    assert "class='tab FAILED'" in html and "class='tab BUILT on'" in html  # the newest is the selected one
    old_panel = re.search(r"<div class='panel' data-run='one/old'([^>]*)>", html)
    new_panel = re.search(r"<div class='panel' data-run='two/new'([^>]*)>", html)
    assert old_panel and "hidden" in old_panel.group(1) and new_panel and "hidden" not in new_panel.group(1)
    assert "V1" not in groups[1]["html"] and "class='tabs'" not in groups[1]["html"]  # one run: no tabs
    assert "<b>3</b>běhů" in _data(out)["summary"]  # the summary still counts runs, not groups


def test_the_same_run_name_in_two_homes_is_two_tabs(tmp_path):
    base = tmp_path / "creature-homes"
    one, two = _home(base, "one"), _home(base, "two")
    _run(one, "r", ("run_end", {"status": "BUILT"}))
    _later()
    _run(two, "r", ("run_end", {"status": "BUILT"}))
    board.render([one, two], base / "board.html")
    [group] = _data(base / "board.html")["runs"]
    assert "data-run='one/r'" in group["html"] and "data-run='two/r'" in group["html"]


def test_a_render_that_fails_leaves_the_shell_and_says_why_in_the_data(tmp_path, monkeypatch):
    base = tmp_path / "creature-homes"
    home = _home(base, "one")
    _run(home, "r1", ("run_end", {"status": "BUILT"}))
    out = base / "board.html"

    def broken(*args, **kwargs):
        raise ValueError("bad ledger")

    monkeypatch.setattr(board, "runs", broken)
    board.main([home], out, None)
    data = _data(out)
    assert "bad ledger" in data["error"] and "runs" not in data  # the page keeps what it shows
    assert "http-equiv" not in out.read_text(encoding="utf-8")
