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
    assert started[1][5:] == ["design", "typewriter-reveal", "--text", "A.", "--text", "B.", "--silent"]
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
