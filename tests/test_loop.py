"""The run itself, offline: every status the creature can end in, with the model, perception, the
workshop and the file checks faked. This is the demo path, so the organizers' claims are pinned here:
a gap comes from a task, nothing installs without passing, a fresh process reuses a skill without
rebuilding, caps hold in code, and the authority fingerprint catches a change."""

import json

import pytest

from creature import authority, ledger, loop, registry, verdict, workshop
from creature.perceive import Reel
from tests.conftest import REPO
from tests.fakes import FakeModel

IMAGE = "sha256:" + "a" * 64
PNG = b"\x89PNG\r\n\x1a\n"
CODE = "def run(input, work):\n    return {}"
CRITERIA = [
    "Characters appear one at a time, left to right.",
    "A caret sits after the last character.",
    "The full line stays on screen at the end.",
]
REPLY = {
    "effect": "Typewriter reveal",
    "slug": "typewriter-reveal",
    "verdict": "try",
    "reason": "2D text",
    "transcript_is_speech": True,
    "task": "Type the text with a caret.",
    "duration_s": 3.0,
    "params": [{"name": "blinks", "value": 8, "quote": "blink it eight times"}],
    "criteria": CRITERIA,
}
RENDER = workshop.WorkshopResult(
    ok=True, value={}, outputs={"clip.mp4": b"clip"}, error=None, killed=None, duration_s=0.1, log=""
)
PASSED = verdict.Checked((), {"frames": 90}, PNG)


def _judge(*passes):
    return {
        "criteria": [
            {"criterion": c, "pass": p, "evidence": "seen"} for c, p in zip(CRITERIA, passes, strict=True)
        ],
        "verdict": "pass",
        "reason": "looks right",
    }


def _authority(home, change=None):
    raw = json.loads((REPO / "authority.json").read_text(encoding="utf-8"))
    if change:
        change(raw)
    (home / "authority.json").write_text(json.dumps(raw, indent=1), encoding="utf-8")


@pytest.fixture
def world(home, tmp_path, monkeypatch):
    """A home with the shipped authority, and every outside dependency faked."""
    _authority(home)
    strip = tmp_path / "reel-strip.png"
    strip.write_bytes(PNG)
    reel = Reel(
        source="reel.mp4", caption="Typewriter tutorial", transcript="type it slowly", author="anet",
        video=tmp_path / "reel.mp4", strip=strip, times=(0.5, 1.5), duration_s=12.0,
        width=720, height=1280, fps=30.0,
    )  # fmt: skip
    monkeypatch.setattr(loop.perceive, "perceive", lambda *a, **k: reel)
    monkeypatch.setattr(loop.workshop, "ensure_image", lambda *a, **k: IMAGE)
    monkeypatch.setattr(loop.workshop, "image_id", lambda *a, **k: IMAGE)
    monkeypatch.setattr(loop.workshop, "run", lambda *a, **k: RENDER)
    monkeypatch.setattr(loop.verdict, "check", lambda *a, **k: PASSED)
    return home


def _creature(home, fake):
    return loop.Creature(home, transport=fake)


def _build(home, fake):
    fake.queue("criteria", REPLY).queue("forge", {"code": CODE, "approach": "pillow + ffmpeg"})
    fake.queue("judge", _judge(True, True, True))
    return _creature(home, fake).try_reel("reel.mp4", "Stay curious.")


def _steps(fake):
    return [call.step for call in fake.calls]


def _kinds(creature):
    return [e["type"] for e in ledger.read(creature.ledger.path)]


# --- the statuses ----------------------------------------------------------------


def test_built_from_an_empty_registry(world, fake_model):
    report = _build(world, fake_model)
    assert report.status == "BUILT" and report.skill == "typewriter-reveal@v1" and report.attempts == 1
    assert _steps(fake_model) == ["criteria", "forge", "judge"]  # an empty registry: the planner is not asked
    skill = registry.get(world / "registry", "typewriter-reveal")
    assert skill.version == 1 and skill.code == CODE and len(skill.tests["held_out"]) == 1
    assert (skill.path / registry.REFERENCE).read_bytes() == PNG
    assert registry.designs(world / "registry") == ["typewriter-reveal"]
    assert report.fingerprint_same and report.spent_usd > 0


def test_built_run_is_fully_in_the_ledger(world, fake_model):
    fake_model.queue("criteria", REPLY).queue("forge", {"code": CODE, "approach": "a"})
    fake_model.queue("judge", _judge(True, True, True))
    creature = _creature(world, fake_model)
    report = creature.try_reel("reel.mp4", "Stay curious.")
    kinds = _kinds(creature)
    for kind in (
        "run_start",
        "task",
        "plan",
        "spec",
        "forge_attempt",
        "attempt_result",
        "installed",
        "run_end",
    ):
        assert kind in kinds, kind
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    end = ledger.read(creature.ledger.path)[-1]
    assert end["status"] == "BUILT" and end["fingerprint_same"] is True and end["changed"] == []
    assert report.run_id == creature.ledger.run_id


def test_fresh_process_reuses_the_skill_without_criteria_or_forge(world, fake_model):
    _build(world, fake_model)
    fresh = FakeModel()
    fresh.queue("planner", {"skill": "typewriter-reveal", "reason": "same technique"})
    fresh.queue("judge", _judge(True, True, True))
    report = _creature(world, fresh).try_reel("another.mp4", "A different line.")
    assert report.status == "HAVE" and report.skill == "typewriter-reveal@v1"
    assert _steps(fresh) == ["planner", "judge"]  # no fresh criteria, no forge: nothing is rebuilt
    judge_call = fresh.calls[1]
    assert judge_call.images[0][1].name == registry.REFERENCE  # judged against ITS stored reel frames
    assert sum(c in judge_call.prompt for c in CRITERIA) == 3  # by ITS stored criteria, hidden included


def test_skill_that_fails_its_own_tests_evolves_against_the_same_tests(world, fake_model):
    _build(world, fake_model)
    fresh = FakeModel()
    fresh.queue("planner", {"skill": "typewriter-reveal", "reason": "same technique"})
    fresh.queue("judge", _judge(False, True, True))  # v1 does not hold on this text
    fresh.queue("forge", {"code": CODE + "  # v2", "approach": "fix"}).queue(
        "judge", _judge(True, True, True)
    )
    report = _creature(world, fresh).try_reel("another.mp4", "A different line.")
    assert report.status == "BUILT" and report.skill == "typewriter-reveal@v2"
    assert _steps(fresh) == ["planner", "judge", "forge", "judge"]  # still no fresh criteria
    forge_prompt = fresh.calls[2].prompt
    assert sum(c in forge_prompt for c in CRITERIA) == 2  # the stored visible two, never the held-out one
    root = world / "registry"
    assert (
        registry.get(root, "typewriter-reveal").version == 2 and (root / "typewriter-reveal" / "1").is_dir()
    )
    assert any(note.startswith("typewriter-reveal v1 failed its own tests") for note in report.notes)


def test_refuse_stops_before_any_attempt(world, fake_model):
    fake_model.queue("criteria", {**REPLY, "verdict": "refuse", "reason": "it would send messages"})
    report = _creature(world, fake_model).try_reel("reel.mp4", "Stay curious.")
    assert report.status == "REFUSE" and report.gap == "it would send messages"
    assert _steps(fake_model) == ["criteria"]
    assert registry.skills(world / "registry") == [] and list((world / "queue").glob("*.json")) == []


def test_ask_waits_in_the_queue_for_a_human(world, fake_model):
    fake_model.queue("criteria", {**REPLY, "verdict": "ask", "reason": "needs a 3D renderer"})
    report = _creature(world, fake_model).try_reel("reel.mp4", "Stay curious.")
    assert report.status == "ASK" and _steps(fake_model) == ["criteria"]
    [request] = [json.loads(p.read_text()) for p in (world / "queue").glob("*.json")]
    assert request["status"] == "waiting" and request["needs"] == "needs a 3D renderer"
    assert request["run"] == report.run_id and registry.skills(world / "registry") == []


def test_failed_after_the_attempt_cap_installs_nothing(world, fake_model):
    attempts = authority.load(world).caps.forge_attempts
    fake_model.queue("criteria", REPLY)
    for n in range(attempts):
        fake_model.queue("forge", {"code": f"{CODE}  # {n}", "approach": "try"}).queue(
            "judge", _judge(False, True, True)
        )
    report = _creature(world, fake_model).try_reel("reel.mp4", "Stay curious.")
    assert report.status == "FAILED" and report.attempts == attempts and report.gap
    assert _steps(fake_model).count("forge") == attempts
    assert registry.skills(world / "registry") == []
    fake_model.assert_drained()


def test_new_skills_per_run_cap_holds_in_code(world, fake_model):
    _authority(world, lambda a: a["caps"].update(new_skills_per_run=1))
    creature = _creature(world, fake_model)
    fake_model.queue("criteria", REPLY).queue("forge", {"code": CODE, "approach": "a"})
    fake_model.queue("judge", _judge(True, True, True))
    assert creature.try_reel("reel.mp4", "One.").status == "BUILT"
    fake_model.queue("planner", {"skill": "none", "reason": "different effect"})
    fake_model.queue("criteria", {**REPLY, "slug": "slide-in", "effect": "Slide in"})
    report = creature.try_reel("other.mp4", "Two.")
    assert report.status == "FAILED" and report.gap.startswith("cap:")
    assert _steps(fake_model).count("forge") == 1  # the second gap never reached the forge


# --- authority -------------------------------------------------------------------


def test_authority_edited_during_the_run_is_caught_by_the_fingerprint(world, fake_model):
    creature = _creature(world, fake_model)
    text = (world / "authority.json").read_text(encoding="utf-8")
    (world / "authority.json").write_text(text + "\n", encoding="utf-8")  # one byte, mid-run
    fake_model.queue("criteria", {**REPLY, "verdict": "skip", "reason": "nothing to reproduce"})
    report = creature.try_reel("reel.mp4", "Stay curious.")
    assert report.status == "SKIP" and report.fingerprint_same is False
    end = ledger.read(creature.ledger.path)[-1]
    assert end["fingerprint_same"] is False and end["changed"] == [authority.AUTHORITY_FILE]


def test_refuse_and_ask_classes_come_from_authority_not_from_a_prompt_constant(world, fake_model):
    # authority.json lists what is refused and what needs a human; the criteria model must be told
    # THOSE lists, or editing the file changes nothing and the authority is decorative
    _authority(world, lambda a: a["refuse"].append("SENTINEL-REFUSE-CLASS"))
    fake_model.queue("criteria", {**REPLY, "verdict": "skip"})
    _creature(world, fake_model).try_reel("reel.mp4", "Stay curious.")
    [call] = [c for c in fake_model.calls if c.step == "criteria"]
    assert "SENTINEL-REFUSE-CLASS" in call.system + call.prompt


def test_stored_tests_are_tamper_evident(world, fake_model):
    # HAVE judges a skill by the tests stored at install; an edited tests.json must not pass as the original
    _build(world, fake_model)
    root = world / "registry"
    path = root / "typewriter-reveal" / "1" / "tests.json"
    tests = json.loads(path.read_text(encoding="utf-8"))
    tests["held_out"] = []
    path.write_text(json.dumps(tests), encoding="utf-8")
    with pytest.raises(ValueError, match="tests"):
        registry.get(root, "typewriter-reveal")
