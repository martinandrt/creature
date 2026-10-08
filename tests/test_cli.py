"""The command line a human uses between runs: list, show, queue. `try` is the loop, tested there."""

import json

import pytest

from creature import cli, criteria, registry


def test_list_and_queue_are_quiet_on_a_fresh_home(home, capsys):
    assert cli.main(["--home", str(home), "list"]) == 0
    assert cli.main(["--home", str(home), "queue"]) == 0
    assert capsys.readouterr().out == ""


def test_queue_shows_what_a_human_must_decide(home, capsys):
    request = {
        "run": "r1",
        "reel": "x",
        "effect": "Particle burst",
        "needs": "a 3D renderer",
        "status": "waiting",
    }
    (home / "queue" / "r1.json").write_text(json.dumps(request), encoding="utf-8")
    assert cli.main(["--home", str(home), "queue"]) == 0
    out = capsys.readouterr().out
    assert "r1" in out and "waiting" in out and "Particle burst" in out and "a 3D renderer" in out


def test_show_unknown_skill_fails_loudly(home):
    with pytest.raises(KeyError, match="nope"):
        cli.main(["--home", str(home), "show", "nope"])


def test_home_is_required(monkeypatch):
    monkeypatch.delenv("CREATURE_HOME", raising=False)
    with pytest.raises(RuntimeError, match="CREATURE_HOME"):
        cli.main(["list"])


def test_list_names_a_broken_seal_and_keeps_working(home, tmp_path, capsys):
    output = criteria.clip_format(2)
    spec = criteria.Spec(
        effect="Typewriter", slug="typewriter", verdict="try", reason="", task="Type it.", text="Hi",
        params={}, output=output, criteria=("a", "b"), held_out=("c",),
        checks=tuple(criteria.checks_for(output)), param_sources={},
    )  # fmt: skip
    reference = tmp_path / "ref.png"
    reference.write_bytes(b"\x89PNG\r\n\x1a\n")
    root = home / "registry"
    skill = registry.install(
        root, spec, "def run(input, work):\n    return {}", origin={"reel": "r", "run": "x"},
        cost={"learn_usd": 0.01}, reference=reference,
    )  # fmt: skip
    (skill.path / "tests.json").write_text("{}", encoding="utf-8")
    assert cli.main(["--home", str(home), "list"]) == 0
    out = capsys.readouterr().out
    assert "SEAL BROKEN" in out and "typewriter" in out
