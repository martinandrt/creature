# ruff: noqa: F811  (world is a fixture from test_loop, used as an argument)
"""POSTUP.md: fixed code from the ledger; the same ledger gives the same text; held-out criteria never
appear; an installed skill gets the record through the registry; the command rebuilds every run."""

import json

from creature import cli, ledger, postup, registry
from tests.test_loop import REPLY, _build, world  # noqa: F401

HIDDEN = "The full line stays on screen at the end."  # REPLY's last criterion: the build holds it out


def test_a_built_run_writes_its_postup_without_hidden_criteria(world, fake_model):
    report = _build(world, fake_model)
    text = (report.folder / postup.NAME).read_text(encoding="utf-8")
    assert text.startswith(f"# How run {report.run_id} went")
    for part in (
        "## Source",
        "- source: reel.mp4",
        "- text: Stay curious.",
        "## What it saw",
        "Visible criteria:",
    ):
        assert part in text, part
    assert REPLY["effect"] in text and "status: BUILT" in text and "learned: typewriter-reveal@v1" in text
    assert (
        "forge_attempt: attempt 1: pillow + ffmpeg" in text
        and "judge 3/3 criteria (visible and held out)" in text
    )
    spec = json.loads((report.folder / "spec.json").read_text(encoding="utf-8"))
    assert spec["held_out"] and all(h not in text for h in spec["held_out"])
    assert all(c in text for c in spec["criteria"])


def test_the_same_ledger_gives_the_same_postup(world, fake_model):
    report = _build(world, fake_model)
    events = ledger.read(world / "runs" / f"{report.run_id}.jsonl")
    spec = postup._spec(report.folder)
    assert postup.text(events, spec) == postup.text(events, spec)
    assert postup.text(events, spec) == (report.folder / postup.NAME).read_text(encoding="utf-8")


def test_the_installed_skill_gets_its_record_through_the_registry(world, fake_model):
    report = _build(world, fake_model)
    skill = registry.get(world / "registry", "typewriter-reveal")  # the seal still holds
    text = (skill.path / registry.POSTUP).read_text(encoding="utf-8")
    assert text.startswith("# typewriter-reveal v1: how it was made")
    assert f"run {report.run_id}" in text and "## Known limits" in text and "held out and not shown" in text
    spec = json.loads((report.folder / "spec.json").read_text(encoding="utf-8"))
    assert all(h not in text for h in spec["held_out"])


def test_the_command_rebuilds_every_finished_run(world, fake_model, capsys):
    report = _build(world, fake_model)
    (report.folder / postup.NAME).unlink()
    assert cli.main(["--home", str(world), "postup"]) == 0
    assert "1 runs have a POSTUP.md" in capsys.readouterr().out
    assert (report.folder / postup.NAME).is_file()


def test_postup_of_an_unknown_event_mix_still_reads():
    events = [
        {"type": "run_start", "run": "r1", "ts": "2026-10-09T01:00:00+00:00"},
        {"type": "make_task", "lines": ["a", "b"]},
        {"type": "something_new", "x": 1},
        {"type": "run_end", "status": "DONE", "spent_usd": 0.01, "ts": "2026-10-09T01:00:30+00:00"},
    ]
    text = postup.text(events)
    assert '- lines: ["a", "b"]' in text and "time: 30 s" in text and "status: DONE" in text
    assert HIDDEN not in text


def test_a_held_out_criterion_quoted_in_a_gap_is_hidden(world, fake_model, tmp_path):
    report = _build(world, fake_model)
    spec = json.loads((report.folder / "spec.json").read_text(encoding="utf-8"))
    hidden = spec["held_out"][0]
    events = ledger.read(world / "runs" / f"{report.run_id}.jsonl")
    events[-1] = {**events[-1], "gap": f"the judge said no: {hidden}"}
    folder = tmp_path / "run"
    folder.mkdir()
    (folder / "spec.json").write_text(
        json.dumps({"parts": [spec]}), encoding="utf-8"
    )  # a part's held-out too
    text = postup.write(folder, events).read_text(encoding="utf-8")
    assert hidden not in text and "the judge said no: [a held-out criterion]" in text
