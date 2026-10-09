# ruff: noqa: F811  (world is a fixture from test_loop, used as an argument)
"""The operator page adds no authority: it lists homes and runs, starts only `make` or `design` as the CLI
would, one at a time, and serves files only from a home's runs/."""

import pytest

from creature import ui
from tests.test_loop import _build, world  # noqa: F401


@pytest.fixture
def desk(world, fake_model):
    _build(world, fake_model)
    return ui.Desk(world.parent)


def test_it_lists_homes_skills_designs_and_runs(desk, world):
    assert desk.home_names() == [world.name]
    assert desk.home_list() == [{"name": world.name, "skills": 1}]
    (world.parent / "empty" / "registry").mkdir(parents=True)
    (world.parent / "empty" / "authority.json").write_text("{}")
    assert [h["name"] for h in desk.home_list()] == [world.name]  # nothing learned: not offered
    seen = desk.describe(world.name)
    assert [s["slug"] for s in seen["skills"]] == ["typewriter-reveal"]
    assert seen["designs"] == [{"name": "typewriter-reveal", "steps": 1}]
    assert seen["runs"][0]["status"] == "BUILT"
    with pytest.raises(KeyError):
        desk.describe("../etc")


def test_it_serves_only_files_of_a_home_runs(desk, world):
    run = desk.describe(world.name)["runs"][0]["run"]
    body, kind = desk.file(world.name, f"{run}/page.html")
    assert body and kind.startswith("text/html")
    for bad in ("../authority.json", f"{run}.jsonl", "/etc/passwd", "../../x.mp4"):
        with pytest.raises(PermissionError):
            desk.file(world.name, bad)


def test_jobs_are_the_cli_commands_one_at_a_time(desk, world, monkeypatch):
    started = []

    class Thread:
        def __init__(self, target, args, daemon):
            started.append(args[1])

        def start(self):
            return None

    monkeypatch.setattr(ui.threading, "Thread", Thread)
    desk.start("make", {"home": world.name, "lines": ["One.", " ", "Two."], "style": True})
    command = started[0]
    assert command[1:5] == ["-m", "creature", "--home", str(world)] and command[5:7] == ["make", "--script"]
    assert command[-3] == "--style" and command[-2].endswith("style-martin.json")
    assert command[-1] == "--silent"  # no sound unless the page asks for it
    with pytest.raises(RuntimeError, match="already running"):
        desk.start("make", {"home": world.name, "lines": ["x"]})
    desk.lock.release()
    desk.start("design", {"home": world.name, "design": "typewriter-reveal", "lines": ["A.", "B."]})
    assert started[1][5:] == ["design", "typewriter-reveal", "--text=A.", "--text=B.", "--silent"]
    desk.lock.release()
    for bad in (
        {"home": "nope", "lines": ["x"]},
        {"home": world.name, "lines": []},
        {"home": world.name, "design": "nope", "lines": ["x"]},
    ):
        with pytest.raises((KeyError, ValueError)):
            desk.start("design" if "design" in bad else "make", bad)
        assert desk.lock.acquire(blocking=False)  # a refused job never keeps the lock
        desk.lock.release()


def test_the_result_is_read_from_what_the_command_printed(tmp_path):
    out = (
        "DONE  make\nspent: $0.0109  authority unchanged: True\n"
        "clip: /h/mikro/runs/20261009T022906Z-f416b5/make-2/joined-sound.mp4\n"
        "run: /h/mikro/runs/20261009T022906Z-f416b5\n"
    )
    found = ui._result(out, tmp_path)
    assert found["status"] == "DONE" and found["run"] == "20261009T022906Z-f416b5"
    assert found["clip"] == "20261009T022906Z-f416b5/make-2/joined-sound.mp4"


@pytest.mark.slow  # a real local server on 127.0.0.1
def test_only_this_page_may_start_a_job(desk, world, monkeypatch):
    # another site open in the same browser must not start a run on localhost
    import json
    import threading
    import urllib.request

    monkeypatch.setattr(ui.Desk, "start", lambda self, kind, data: "job1")
    ui.Handler.desk = desk
    server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    body = json.dumps({"home": world.name, "lines": ["x"]}).encode()

    def post(headers):
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/make", data=body, headers=headers)
        try:
            return urllib.request.urlopen(request).status
        except urllib.error.HTTPError as error:
            return error.code

    try:
        own = f"http://127.0.0.1:{port}"
        assert post({"Origin": own, "Content-Type": "application/json"}) == 200
        assert post({"Origin": "https://evil.example", "Content-Type": "application/json"}) == 403
        assert post({"Content-Type": "application/json"}) == 403  # no origin: not a page of ours
        assert post({"Origin": own, "Content-Type": "text/plain"}) == 403  # a form-like post
        rebound = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/homes", headers={"Host": "evil.example"}
        )
        try:
            code = urllib.request.urlopen(rebound).status
        except urllib.error.HTTPError as error:
            code = error.code
        assert code == 403
    finally:
        server.shutdown()


def test_allowed_is_only_the_desks_own_page():
    own = {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765", "Content-Type": "application/json"}
    assert ui.allowed(own, 8765, post=True)
    assert ui.allowed({**own, "Host": "localhost:8765", "Origin": "http://localhost:8765"}, 8765, post=True)
    assert not ui.allowed({**own, "Origin": "http://evil.example"}, 8765, post=True)
    assert not ui.allowed({**own, "Host": "evil.example"}, 8765, post=True)
    assert not ui.allowed({**own, "Host": "evil.example"}, 8765, post=False)  # DNS rebinding, even a GET
    assert not ui.allowed({**own, "Content-Type": "text/plain"}, 8765, post=True)  # a simple request
    assert not ui.allowed({k: v for k, v in own.items() if k != "Origin"}, 8765, post=True)
    assert ui.allowed({"Host": "127.0.0.1:8765"}, 8765, post=False)


def test_progress_is_read_from_the_runs_ledger():
    events = [
        {"type": "make_task", "lines": ["a", "b"]},
        {"type": "model_call", "cost_usd": 0.002},
        {
            "type": "make_choice",
            "attempt": 1,
            "shots": [{"line": 1, "skill": "s1", "why": "dark"}, {"line": 2, "skill": "s2", "why": "x"}],
        },
        {"type": "make_shot", "line": 1},
    ]
    p = ui.progress_of(events, 2)
    assert p["word"] == "RENDERING" and p["step"] == 1 and p["of"] == 2 and p["calls"] == 1
    assert p["picks"][0] == {"line": 1, "skill": "s1", "why": "dark"}
    events += [{"type": "make_judged", "ok": True}, {"type": "run_end", "status": "DONE"}]
    assert ui.progress_of(events, 2)["word"] == "DONE"


def test_a_run_shows_the_reel_its_skills_were_learned_from(desk, world):
    run = desk.describe(world.name)["runs"][0]["run"]
    with pytest.raises(KeyError):
        desk.run(world.name, "../x")
    seen = desk.run(world.name, run)
    assert seen["run"] == run and seen["status"] == "BUILT"
    learned = desk.reel(world.name, "typewriter-reveal")
    assert (
        learned["skill"] == "typewriter-reveal"
        and learned["reel"] == "reel"
        and learned["has_video"] is False
    )
    with pytest.raises(PermissionError):
        desk.reel_file(world.name, "typewriter-reveal")  # the reel is not on this machine


def test_a_reloaded_page_finds_the_running_job(desk, world, monkeypatch):
    class Thread:
        def __init__(self, target, args, daemon):
            pass

        def start(self):
            return None

    monkeypatch.setattr(ui.threading, "Thread", Thread)
    assert desk.current() == {}
    job = desk.start("make", {"home": world.name, "lines": ["One.", "Two."]})
    assert desk.current() == {"job": job, "kind": "make", "home": world.name, "lines": ["One.", "Two."]}
    desk.jobs[job]["state"] = "done"
    desk.lock.release()
    assert desk.current() == {}


def test_a_learning_run_shows_the_reel_it_learned_on(desk, world):
    run = desk.describe(world.name)["runs"][0]["run"]
    (world / "runs" / run / "reel.mp4").write_bytes(b"mp4")
    seen = desk.run(world.name, run)
    assert seen["kind"] == "learn" and seen["learned"][0]["src"] == f"file:{run}/reel.mp4"
    with pytest.raises(PermissionError):
        desk.reel_file(world.name, run=run)  # its source path is not a file on this machine
    with pytest.raises(KeyError):
        desk.reel_file(world.name, run="../../x")


def test_a_video_can_be_sought_in_parts():
    assert ui.byte_range("bytes=0-", 100) == (0, 99)
    assert ui.byte_range("bytes=10-19", 100) == (10, 19)
    assert ui.byte_range("bytes=90-500", 100) == (90, 99)
    assert ui.byte_range("bytes=-30", 100) == (70, 99)
    for whole in (None, "", "bytes=-", "items=0-1", "bytes=200-300", "bytes=1-2,5-6"):
        assert ui.byte_range(whole, 100) is None


def test_a_reel_the_author_hid_is_not_shown(desk, world):
    run = desk.describe(world.name)["runs"][0]["run"]
    (world.parent / ".hidden").write_text("reel\n")
    assert desk.describe(world.name)["runs"] == []
    with pytest.raises(KeyError):
        desk.run(world.name, run)
    assert desk.reel(world.name, "typewriter-reveal") is None
    with pytest.raises(PermissionError):
        desk.file(world.name, f"{run}/page.html")
