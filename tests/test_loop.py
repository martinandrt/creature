"""The run itself, offline: every status the creature can end in, with the model, perception, the
workshop and the file checks faked. This is the demo path, so the organizers' claims are pinned here:
a gap comes from a task, nothing installs without passing, a fresh process reuses a skill without
rebuilding, caps hold in code, and the authority fingerprint catches a change."""

import json
import re

import pytest

from creature import authority, ledger, loop, page, registry, verdict, workshop
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
REAL_CHECK = verdict.check  # the fixture below fakes it; the design tests put it back


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
    planner_call, judge_call = fresh.calls
    # the planner compares frames with frames: the reel's, then each skill's stored reference
    shown = [(label, path.name) for label, path in planner_call.images]
    assert shown[0][1] == "reel-strip.png" and ("skill typewriter-reveal", registry.REFERENCE) in [
        (label[: len("skill typewriter-reveal")], name) for label, name in shown[1:]
    ]
    assert judge_call.images[0][1].name == registry.REFERENCE  # judged against ITS stored reel frames
    assert sum(c in judge_call.prompt for c in CRITERIA) == 3  # by ITS stored criteria, hidden included


def test_forge_is_told_the_text_of_this_run(world, fake_model):
    # a stored skill once kept the first text's key words, so v2 on new text emphasised nothing:
    # the forge prompt must carry this run's text and the rule that words come from input["text"]
    _build(world, fake_model)
    [forge_call] = [c for c in fake_model.calls if c.step == "forge"]
    assert "Stay curious." in forge_call.prompt
    assert "never hardcode words" in forge_call.system


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


def test_install_is_refused_when_authority_changes_after_the_judge_passed(world, fake_model, monkeypatch):
    # the fingerprint is checked again right before install: an authority edited while the forge
    # ran must leave the registry empty, not install under rules nobody approved
    real_build = loop.forge.build
    path = world / "authority.json"

    def build_then_edit(*args, **kwargs):
        built = real_build(*args, **kwargs)
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return built

    monkeypatch.setattr(loop.forge, "build", build_then_edit)
    fake_model.queue("criteria", REPLY).queue("forge", {"code": CODE, "approach": "a"})
    fake_model.queue("judge", _judge(True, True, True))
    creature = _creature(world, fake_model)
    report = creature.try_reel("reel.mp4", "Stay curious.")
    assert report.status == "FAILED" and report.gap.startswith("not installed")
    assert authority.AUTHORITY_FILE in report.gap and report.fingerprint_same is False
    kinds = _kinds(creature)
    assert "install_refused" in kinds and "installed" not in kinds and kinds[-1] == "run_end"
    root = world / "registry"
    assert registry.skills(root) == [] and registry.designs(root) == []
    assert not (root / "typewriter-reveal").exists()


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


# --- designs ---------------------------------------------------------------------


def test_design_replays_with_no_model_call_and_no_dollars(world, fake_model):
    # Martin's rule: a learned design replays at $0 — no planner, no judge, no model call at all
    _build(world, fake_model)
    silent = FakeModel()  # unscripted: any model call raises
    creature = _creature(world, silent)
    report = creature.run_design("typewriter-reveal", "Replay this.")
    assert report.status == "DONE" and silent.calls == []
    kinds = _kinds(creature)
    assert "model_call" not in kinds and "design_step" in kinds and kinds[-1] == "run_end"
    assert creature.ledger.spent_usd == 0 and report.spent_usd == 0


def _probe(frames):
    """What the fixed probe code reports for a clip in the house format with this many frames."""
    return {
        "readable": True, "video": True, "codec": "h264", "pix_fmt": "yuv420p", "width": 1080,
        "height": 1920, "fps": 30.0, "frames": frames, "duration_s": frames / 30, "luma": [255],
        "times": [0.0],
    }  # fmt: skip


def _two_skills(world, fake_model, monkeypatch, *, joined_frames):
    """Two learned skills (3 s and 2 s), composed into one design; the real file checks run against
    a workshop faked per code: a render, the probe (frames by which clip it is asked about), the join."""
    _build(world, fake_model)  # typewriter-reveal, 3.0 s = 90 frames
    second = FakeModel()
    second.queue("planner", {"skill": "none", "reason": "another effect"})
    second.queue("criteria", {**REPLY, "slug": "slide-in", "effect": "Slide in", "duration_s": 2.0})
    second.queue("forge", {"code": CODE + "  # slide", "approach": "a"}).queue(
        "judge", _judge(True, True, True)
    )
    assert _creature(world, second).try_reel("other.mp4", "Two.").status == "BUILT"  # 2.0 s = 60 frames
    root = world / "registry"
    registry.compose(root, "promo", ["typewriter-reveal", "slide-in"], origin={"by": "test"})
    monkeypatch.setattr(loop.verdict, "check", REAL_CHECK)
    calls = {"renders": [], "probes": [], "joins": []}
    frames_of = {"step-1-typewriter-reveal": 90, "step-2-slide-in": 60}

    def run(code, input, files=None, **kwargs):
        if code == loop.JOIN_CODE:
            calls["joins"].append((input, files))
            return workshop.WorkshopResult(True, {"parts": 2}, {loop.JOINED: b"joined"}, None, None, 0.2, "")
        if code == verdict.PROBE_CODE:
            [clip] = files.values()
            calls["probes"].append(clip)
            frames = frames_of.get(clip.parent.name, joined_frames)
            return workshop.WorkshopResult(True, _probe(frames), {"strip.png": PNG}, None, None, 0.1, "")
        calls["renders"].append(input)
        return RENDER

    monkeypatch.setattr(loop.workshop, "run", run)
    return calls


def test_two_skills_in_one_design_concatenate_to_the_declared_total_frames(world, fake_model, monkeypatch):
    calls = _two_skills(world, fake_model, monkeypatch, joined_frames=150)
    silent = FakeModel()
    creature = _creature(world, silent)
    report = creature.run_design("promo", ["One line.", "Second line."])
    assert report.status == "DONE" and silent.calls == [] and report.spent_usd == 0
    assert report.clip == creature.folder / loop.JOINED
    assert [r["text"] for r in calls["renders"]] == ["One line.", "Second line."]  # each step its own text
    [(join_input, join_files)] = calls["joins"]
    assert join_input["parts"] == ["part00.mp4", "part01.mp4"]  # in the design's order
    assert [join_files[p].parent.name for p in join_input["parts"]] == [
        "step-1-typewriter-reveal", "step-2-slide-in",
    ]  # fmt: skip
    assert [p.name for p in calls["probes"]] == ["clip.mp4", "clip.mp4", loop.JOINED]
    [join_event] = [e for e in ledger.read(creature.ledger.path) if e["type"] == "design_join"]
    assert join_event["parts"] == 2 and join_event["frames"] == 150 and join_event["checks"] == "all passed"
    [task] = [e for e in ledger.read(creature.ledger.path) if e["type"] == "design_task"]
    assert task["texts"] == ["One line.", "Second line."] and task["steps"] == 2


def test_a_joined_clip_one_frame_short_fails_the_design(world, fake_model, monkeypatch):
    # the frames check is exact on purpose: a design that drifts by a frame is not the design
    _two_skills(world, fake_model, monkeypatch, joined_frames=149)
    creature = _creature(world, FakeModel())
    report = creature.run_design("promo", "Same line everywhere.")
    assert report.status == "FAILED" and report.clip is None
    assert report.gap.startswith("joining the steps:") and "frames: got 149, need exactly 150" in report.gap
    [join_event] = [e for e in ledger.read(creature.ledger.path) if e["type"] == "design_join"]
    assert any("149" in problem for problem in join_event["checks"])


def test_a_design_given_the_wrong_number_of_texts_runs_nothing(world, fake_model, monkeypatch):
    calls = _two_skills(world, fake_model, monkeypatch, joined_frames=150)
    creature = _creature(world, FakeModel())
    report = creature.run_design("promo", ["one", "two", "three"])
    assert report.status == "FAILED" and "2 steps" in report.gap
    assert calls["renders"] == [] and calls["joins"] == []
    kinds = _kinds(creature)
    assert "design_step" not in kinds and kinds[-1] == "run_end"


# --- seals -----------------------------------------------------------------------


def test_a_tampered_skill_is_logged_and_never_offered_to_the_planner(world, fake_model):
    # a broken seal must not stop the creature: the skill is reported, skipped, and the run goes on
    _build(world, fake_model)
    root = world / "registry"
    path = root / "typewriter-reveal" / "1" / "tests.json"
    tests = json.loads(path.read_text(encoding="utf-8"))
    tests["held_out"] = []
    path.write_text(json.dumps(tests), encoding="utf-8")
    broken = registry.broken(root)
    assert len(broken) == 1 and "typewriter-reveal" in str(broken[0])
    fresh = FakeModel()
    fresh.queue("criteria", {**REPLY, "verdict": "skip", "reason": "nothing to reproduce"})
    creature = _creature(world, fresh)
    report = creature.try_reel("another.mp4", "A different line.")
    assert report.status == "SKIP"
    assert _steps(fresh) == ["criteria"]  # nothing left to offer, so the planner was not asked
    kinds = _kinds(creature)
    assert "seal_broken" in kinds and kinds[-1] == "run_end"


# --- the page --------------------------------------------------------------------


def _page(report):
    return (report.folder / page.PAGE).read_text(encoding="utf-8")


def _refs(text):
    return [r for r in re.findall(r"(?:src|href)='([^']*)'", text) if not r.startswith("http")]


def test_page_and_overview_use_relative_references_only(world, fake_model):
    report = _build(world, fake_model)
    text = _page(report)
    assert "BUILT" in text and "typewriter-reveal" in text
    assert str(world) not in text and "/Users/" not in text and "/private/" not in text
    refs = _refs(text)
    assert refs and all(not r.startswith("/") and (report.folder / r).exists() for r in refs)
    overview = (world / page.OVERVIEW).read_text(encoding="utf-8")
    assert "BUILT" in overview and str(world) not in overview


def test_failed_page_lists_every_attempt(world, fake_model):
    attempts = authority.load(world).caps.forge_attempts
    fake_model.queue("criteria", REPLY)
    for n in range(attempts):
        fake_model.queue("forge", {"code": f"{CODE}  # try {n + 1}", "approach": "a"})
        fake_model.queue("judge", _judge(False, True, True))
    report = _creature(world, fake_model).try_reel("reel.mp4", "Stay curious.")
    text = _page(report)
    assert report.status == "FAILED" and f"Failed after {attempts} attempts" in text
    for n in range(1, attempts + 1):
        assert f"attempt {n}:" in text  # every attempt in the timeline, none cut
    assert text.count("forge_attempt") == attempts and f"# try {attempts}" in text


def test_failed_page_keeps_the_code_when_the_script_crashed(world, fake_model, monkeypatch):
    # "never cut failures": a crash in the workshop leaves no clip; the page must still show the attempt
    crashed = workshop.WorkshopResult(
        ok=False, value=None, outputs={}, error="NameError: name 'x' (skill.py line 2)",
        killed=None, duration_s=0.1, log="",
    )  # fmt: skip
    monkeypatch.setattr(loop.workshop, "run", lambda *a, **k: crashed)
    attempts = authority.load(world).caps.forge_attempts
    fake_model.queue("criteria", REPLY)
    for n in range(attempts):
        fake_model.queue("forge", {"code": f"{CODE}  # crash {n + 1}", "approach": "a"})
    report = _creature(world, fake_model).try_reel("reel.mp4", "Stay curious.")
    text = _page(report)
    assert report.status == "FAILED" and report.clip is None
    assert "NameError" in text and f"# crash {attempts}" in text


def test_design_run_renders_its_page_and_rebuild_needs_no_model(world, fake_model):
    _build(world, fake_model)
    silent = FakeModel()
    creature = _creature(world, silent)
    report = creature.run_design("typewriter-reveal", "Replay this.")
    assert "DONE" in _page(report) and "no model call" in _page(report) and silent.calls == []
    again = page.rebuild(world, report.run_id)
    assert again == report.folder / page.PAGE and "DONE" in again.read_text(encoding="utf-8")


# --- montage ---------------------------------------------------------------------

SURFACE = ["Cards sit on a grid.", "Each card carries a title.", "The grid fills the frame."]
MONTAGE_REPLY = {
    **REPLY,
    "slug": "grid-promo",
    "effect": "Fast-cut montage of graphic screens",
    "task": "Cut the surfaces every few frames with the mark on top.",
    "duration_s": 6.0,
    "kind": "montage",
    "surfaces": [
        {"slug": "grid-cards", "effect": "A grid of cards", "task": "Draw cards.", "frames": [0.5, 1.5],
         "criteria": SURFACE},
    ],
    "gaps": [{"what": "a product photo", "needs": "footage"}],
}  # fmt: skip
CHOICE = {
    "sources": ["grid-cards"],
    "every_frames": 4,
    "seconds": 6,
    "mark": {"use": True, "x": 0.5, "y": 0.85, "height": 0.06},
    "reason": "one surface, the mark on top",
}
MONTAGE_RESULT = workshop.WorkshopResult(
    True, {"frames": 180, "scenes": 45, "inks": ["dark"], "layers": [[440, 1500, 200, 108]]},
    {"montage.mp4": b"montage"}, None, None, 0.5, "",
)  # fmt: skip


def _judge_of(criteria_texts, *passes):
    return {
        "criteria": [
            {"criterion": c, "pass": p, "evidence": "seen"}
            for c, p in zip(criteria_texts, passes, strict=True)
        ],
        "verdict": "pass",
        "reason": "ok",
    }


@pytest.fixture
def montage_world(world, monkeypatch):
    """The reel is a montage: frames_at and the montage render are faked, everything else as in world."""
    monkeypatch.setattr(
        loop.perceive, "frames_at", lambda reel, times, target, **k: (target.write_bytes(PNG), target)[1]
    )
    calls = {"renders": [], "montages": []}

    def run(code, input, files=None, **kwargs):
        if code == loop.montage.MONTAGE_CODE:
            calls["montages"].append((input, files))
            return MONTAGE_RESULT
        calls["renders"].append(input)
        return RENDER

    monkeypatch.setattr(loop.workshop, "run", run)
    return world, calls


def _learn_montage(home, fake):
    fake.queue("criteria", MONTAGE_REPLY).queue("forge", {"code": CODE, "approach": "pillow"})
    fake.queue("judge", _judge_of(SURFACE, True, True, True))  # the surface, by its own criteria
    fake.queue("compose", CHOICE).queue("judge", _judge(True, True, True))  # the montage, by the reel's
    creature = _creature(home, fake)
    return creature, creature.try_reel("reel.mp4", "Stay curious.")


def test_a_montage_reel_learns_its_surface_composes_and_saves_a_timeline(montage_world, fake_model):
    home, calls = montage_world
    creature, report = _learn_montage(home, fake_model)
    assert report.status == "BUILT" and report.skill == "design:grid-promo", report.gap
    assert _steps(fake_model) == ["criteria", "forge", "judge", "compose", "judge"]
    fake_model.assert_drained()
    root = home / "registry"
    assert registry.get(root, "grid-cards").version == 1
    design = registry.design(root, "grid-promo")
    t = design["timeline"]
    assert (
        t["frames"] == 180
        and len(t["scenes"]) == 45
        and t["sources"] == {"a": {"skill": "grid-cards", "version": 1, "params": {}}}
    )
    assert design["tests"]["criteria"] and len(design["tests"]["held_out"]) == 1
    assert registry._index(root)["designs"]["grid-promo"]["kind"] == "timeline"
    kinds = _kinds(creature)
    for kind in (
        "spec",
        "part",
        "installed",
        "gaps",
        "composed",
        "montage_source",
        "montage",
        "montage_judged",
    ):
        assert kind in kinds, kind
    assert kinds[-1] == "run_end" and any(n.startswith("gap: a product photo") for n in report.notes)
    [(job, files)] = calls["montages"]
    assert job["frames"] == 180 and job["sources"] == ["a"] and job["mark_text"] == "STAY"
    assert set(files) == {"a.mp4"} and job["inks"] == {"dark": "#111111", "light": "#f4f4f4"}
    # the source was rendered at the montage's length, not the 3 s it was learned on
    assert calls["renders"][-1]["output"]["duration_s"] == 6.0
    assert report.clip == creature.folder / "montage-1" / "montage.mp4"


def test_a_saved_timeline_replays_with_the_users_style_at_no_cost(montage_world, fake_model, tmp_path):
    home, calls = montage_world
    _learn_montage(home, fake_model)
    style = {"colors": {"night": "#1B2135", "paper": "#EBEDF2"}, "fonts": {"title": "Barlow"}}
    mark = tmp_path / "logo.png"
    mark.write_bytes(PNG)
    silent = FakeModel()
    creature = _creature(home, silent)
    report = creature.run_design("grid-promo", "New words here", mark=mark, style=style)
    assert report.status == "DONE" and silent.calls == [] and report.spent_usd == 0, report.gap
    source_input = calls["renders"][-1]
    assert source_input["text"] == "New words here" and source_input["params"]["palette"] == [
        "#1B2135",
        "#EBEDF2",
    ]
    assert source_input["params"]["fonts"] == {"title": loop.montage.FONT_FILES["Barlow"]}
    job, files = calls["montages"][-1]
    assert job["inks"] == {"dark": "#1B2135", "light": "#EBEDF2"} and files["mark.png"] == mark
    [task] = [e for e in ledger.read(creature.ledger.path) if e["type"] == "design_task"]
    assert task["kind"] == "timeline" and task["style"] is True and task["mark"] is True
    assert report.clip == creature.folder / "montage" / "montage.mp4"


def test_a_choice_naming_no_known_skill_is_fed_back_not_rendered(montage_world, fake_model):
    # the composer's first choice names nothing in the catalog: the spine must say so and ask again,
    # never crash and never send an empty timeline to the workshop
    home, calls = montage_world
    fake_model.queue("criteria", MONTAGE_REPLY).queue("forge", {"code": CODE, "approach": "a"})
    fake_model.queue("judge", _judge_of(SURFACE, True, True, True))
    fake_model.queue("compose", {**CHOICE, "sources": ["ghost"]})
    fake_model.queue("compose", CHOICE).queue("judge", _judge(True, True, True))
    creature = _creature(home, fake_model)
    report = creature.try_reel("reel.mp4", "Stay curious.")
    assert report.status == "BUILT", report.gap
    assert _steps(fake_model) == ["criteria", "forge", "judge", "compose", "compose", "judge"]
    assert len(calls["montages"]) == 1  # the empty choice never reached the workshop
    assert "Your last montage failed" in fake_model.calls[4].prompt
    assert _kinds(creature).count("composed") == 2 and _kinds(creature)[-1] == "run_end"


# --- a run always ends: run_end and a page, whatever went wrong after the task ----------


def test_a_bad_criteria_answer_after_the_paid_call_ends_the_run_not_the_process(world, fake_model):
    # the criteria model answered (and was paid) but gave too few usable criteria: that is a FAILED run
    # with its ledger closed and its page, not a ValueError traceback with the money unaccounted for
    fake_model.queue("criteria", {**REPLY, "criteria": ["only one", "and two"]})
    creature = _creature(world, fake_model)
    report = creature.try_reel("reel.mp4", "Stay curious.")
    assert report.status == "FAILED" and "criteria" in report.gap
    kinds = _kinds(creature)
    assert "model_call" in kinds and kinds[-1] == "run_end" and (report.folder / page.PAGE).exists()


def test_a_reel_that_cannot_be_read_ends_the_run_with_its_page(world, fake_model, monkeypatch):
    def unreadable(*args, **kwargs):
        raise loop.perceive.PerceiveError("not a video reel")

    monkeypatch.setattr(loop.perceive, "perceive", unreadable)
    creature = _creature(world, fake_model)
    report = creature.try_reel("https://www.instagram.com/p/X/", "Stay curious.")
    assert report.status == "FAILED" and "not a video reel" in report.gap and fake_model.calls == []
    kinds = _kinds(creature)
    assert kinds[-1] == "run_end" and (report.folder / page.PAGE).exists()


def test_a_montage_is_checked_for_the_reels_rhythm_and_palette(montage_world, fake_model, monkeypatch):
    # look_matters: the spine measures colours and cut rhythm against the reel. The rhythm is a property
    # of the MONTAGE (the surfaces are single screens), so the montage render must carry the rhythm
    # check, and every render from this reel the palette check
    home, _ = montage_world
    strip = home / "cut-reel-strip.png"
    strip.write_bytes(PNG)
    cut_reel = Reel(
        source="reel.mp4", caption="Brand film", transcript="", author="anet", video=home / "reel.mp4",
        strip=strip, times=(0.5, 1.5), duration_s=6.0, width=1080, height=1920, fps=30.0,
        cuts=tuple(0.5 * i for i in range(1, 12)),  # 11 cuts: a shot lasts 15 frames
        palette=({"hex": "#1b2135", "share": 0.7}, {"hex": "#ebedf2", "share": 0.25}),
    )  # fmt: skip
    monkeypatch.setattr(loop.perceive, "perceive", lambda *a, **k: cut_reel)
    seen = {}

    def check(outputs, checks, *, folder, **kwargs):
        seen[folder.name] = sorted({c["kind"] for c in checks})
        return PASSED

    monkeypatch.setattr(loop.verdict, "check", check)
    fake_model.queue("criteria", {**MONTAGE_REPLY, "look_matters": True})
    fake_model.queue("forge", {"code": CODE, "approach": "pillow"})
    fake_model.queue("judge", _judge_of(SURFACE, True, True, True))
    fake_model.queue("compose", CHOICE).queue("judge", _judge(True, True, True))
    report = _creature(home, fake_model).try_reel("reel.mp4", "Stay curious.")
    assert report.status == "BUILT", report.gap
    assert "palette" in seen["grid-cards-attempt-1"] and "rhythm" not in seen["grid-cards-attempt-1"]
    assert {"palette", "rhythm", "smooth", "frames"} <= set(seen["montage-1"]), seen["montage-1"]


# --- the round forge (a home whose authority names a forge model) -------------------

FRAME_PY = (
    "from PIL import Image\ndef frame(n, ctx):\n    return Image.new('RGB', (ctx['width'], ctx['height']))\n"
)
DEEP_FILES = [
    {"path": "frame.py", "content": FRAME_PY},
    {"path": "layout.json", "content": json.dumps({"frames": 90})},
]
DEEP_HEAD = loop.deep.WRAPPER.split("__FILES__")[0]


@pytest.fixture
def deep_world(world, monkeypatch):
    """The authority names a forge model; the workshop is faked per code: preview frames, pair sheets,
    the probe (frames by which clip it is asked about), the full render. verdict.check is real."""
    _authority(world, lambda a: a["models"].update(forge="claude-opus-5-5"))
    monkeypatch.setattr(loop.verdict, "check", REAL_CHECK)
    calls = {"previews": [], "pairs": [], "renders": [], "probes": []}

    def run(code, input, files=None, **kwargs):
        if code == verdict.PROBE_CODE:
            [clip] = files.values()
            calls["probes"].append(clip)
            frames = 30 if clip.parent.name == "shorter" else 90
            return workshop.WorkshopResult(True, _probe(frames), {"strip.png": PNG}, None, None, 0.1, "")
        if code == loop.deep.PAIRS_CODE:
            calls["pairs"].append(input)
            value = {
                "pairs": len(input["pairs"]),
                "crops": 0,
                "score": 12.5,
                "frame_diff": 12.5,
                "palette_misses": 0,
            }
            return workshop.WorkshopResult(True, value, {"pairs.png": PNG}, None, None, 0.2, "")
        if code.startswith(DEEP_HEAD) and input.get("frames_only") is not None:
            calls["previews"].append(input["frames_only"])
            outputs = {f"f_{n:04d}.png": PNG for n in input["frames_only"]}
            return workshop.WorkshopResult(True, {"frames": len(outputs)}, outputs, None, None, 0.3, "")
        calls["renders"].append(input)
        return RENDER

    monkeypatch.setattr(loop.workshop, "run", run)
    return world, calls


def test_a_home_with_a_forge_model_builds_in_rounds_and_installs_the_wrapper(deep_world, fake_model):
    home, calls = deep_world
    fake_model.queue("criteria", REPLY)
    fake_model.queue("forge", {"note": "first, done", "files": DEEP_FILES, "edits": [], "done": True})
    fake_model.queue("judge", _judge(True, True, True))
    creature = _creature(home, fake_model)
    report = creature.try_reel("reel.mp4", "Stay curious.")
    assert report.status == "BUILT" and report.skill == "typewriter-reveal@v1", report.gap
    assert _steps(fake_model) == ["criteria", "forge", "judge"]
    forge_call = fake_model.calls[1]
    assert forge_call.model == "claude-opus-5-5" and [label for label, _ in forge_call.images] == [
        "reel frames"
    ]
    rounds = authority.load(home).caps.forge_attempts
    assert f"Round 1 of at most {rounds}" in forge_call.prompt and "SECRET" not in forge_call.prompt
    skill = registry.get(home / "registry", "typewriter-reveal")
    assert skill.code == loop.deep.skill_code({f["path"]: f["content"] for f in DEEP_FILES})
    assert skill.code.startswith(DEEP_HEAD) and "def run(input, work)" in skill.code
    kinds = _kinds(creature)
    for kind in ("round", "round_score", "other_length", "installed", "run_end"):
        assert kind in kinds, kind
    assert len(calls["previews"]) == 1 and len(calls["pairs"]) == 1  # one round: frames, then pairs
    # the whole clip, then the same skill 2 s shorter, both through the real file checks
    assert [p.parent.name for p in calls["probes"]] == ["attempt-1", "shorter"]
    assert [r["output"]["duration_s"] for r in calls["renders"]] == [3.0, 1.0]
    [other] = [e for e in ledger.read(creature.ledger.path) if e["type"] == "other_length"]
    assert other["ok"] is True and other["seconds"] == 1.0
    assert (creature.folder / "attempt-1" / "shorter" / "clip.mp4").exists()
    assert (creature.folder / "rounds" / "round-01" / "files.json").exists()


# --- the asset library in the round forge ----------------------------------------------


@pytest.fixture
def library_world(deep_world, tmp_path):
    from tests.test_assets import library

    home, calls = deep_world
    lib = library(tmp_path / "lib")
    _authority(
        home, lambda a: (a["models"].update(forge="claude-opus-5-5"), a["workshop"].update(assets=str(lib)))
    )
    return home, calls, lib


def _learn_with_library(home, fake):
    files = [
        {"path": "frame.py", "content": FRAME_PY + "FONT = '/assets/pisma/font.ttf'\n"},
        {"path": "layout.json", "content": json.dumps({"frames": 90})},
    ]
    fake.queue("criteria", {**REPLY, "effect": "A circle grows", "task": "Draw a circle."})
    said = ["/assets/tvary/kruh.svg", "/assets/tvary/kruh.svg", "/etc/passwd"]
    fake.queue("forge", {"note": "done", "files": files, "edits": [], "done": True, "assets": said})
    fake.queue("judge", _judge(True, True, True))
    creature = _creature(home, fake)
    return creature, creature.try_reel("reel.mp4", "Stay curious.")


def test_the_forge_is_told_about_the_library_and_the_skill_records_what_it_uses(library_world, fake_model):
    home, _, _ = library_world
    creature, report = _learn_with_library(home, fake_model)
    assert report.status == "BUILT" and report.fingerprint_same, report.gap
    forge_prompt = fake_model.calls[1].prompt
    assert "Asset library, read-only at /assets" in forge_prompt and "/assets/tvary/kruh.svg" in forge_prompt
    assert "/assets/pisma/font.ttf" in forge_prompt and "sipka" not in forge_prompt
    skill = registry.get(home / "registry", "typewriter-reveal")
    assert skill.capability["assets"] == ["/assets/pisma/font.ttf", "/assets/tvary/kruh.svg"]
    [start] = [e for e in ledger.read(creature.ledger.path) if e["type"] == "run_start"]
    assert start["fingerprint"] == authority.fingerprint(home, image_id=IMAGE).digest


def test_a_library_edited_during_the_run_refuses_the_install(library_world, fake_model, monkeypatch):
    home, _, lib = library_world
    real_build = loop.deep.build

    def build_then_edit(*args, **kwargs):
        result = real_build(*args, **kwargs)
        (lib / "tvary" / "kruh.svg").write_text("<svg/>")  # one byte of what the workshop may read
        return result

    monkeypatch.setattr(loop.deep, "build", build_then_edit)
    creature, report = _learn_with_library(home, fake_model)
    assert report.status == "FAILED" and report.gap.startswith("not installed")
    assert authority.ASSETS_PART in report.gap and registry.skills(home / "registry") == []
    [refused] = [e for e in ledger.read(creature.ledger.path) if e["type"] == "install_refused"]
    assert refused["changed"] == [authority.ASSETS_PART]


def test_a_skill_that_needs_the_library_is_refused_cleanly_where_there_is_none(library_world, fake_model):
    # capability.assets says "it needs that library to run": a home without one must say so before
    # the workshop fails on a missing file, and spend nothing on it
    home, calls, _ = library_world
    _learn_with_library(home, fake_model)
    _authority(home)  # the shipped authority: no asset library
    before = len(calls["renders"])
    creature = _creature(home, FakeModel())
    report = creature.run_design("typewriter-reveal", "Other words.")
    assert report.status == "FAILED" and "asset library" in report.gap and "/assets/" in report.gap
    assert len(calls["renders"]) == before  # nothing rendered
    assert _kinds(creature)[-1] == "run_end"


def test_a_style_on_a_step_design_is_named_as_not_applied(world, fake_model):
    # "replay with my style" on a plain (step) design changes nothing: the style belongs to a montage.
    # Saying nothing would let a recording claim a style that was never used
    _build(world, fake_model)
    style = {"colors": {"night": "#1B2135"}, "fonts": {"title": "Barlow"}}
    report = _creature(world, FakeModel()).run_design("typewriter-reveal", "Replay this.", style=style)
    assert report.status == "DONE"
    assert any("style" in note and "montage" in note for note in report.notes), report.notes


# --- a montage reuses installed surfaces -----------------------------------------------

SURFACE2 = ["A wall of cards.", "Each card carries a number.", "The wall scrolls up."]
MONTAGE_REPLY_2 = {
    **MONTAGE_REPLY,
    "slug": "card-promo",
    "surfaces": [
        {"slug": "card-wall", "effect": "A grid of cards", "task": "Draw cards.", "frames": [0.5],
         "criteria": SURFACE2},
    ],
}  # fmt: skip


def _second_montage(home, fake, *, have_ok, reply=MONTAGE_REPLY_2, source_slug="grid-cards"):
    """A second montage reel in a home that already learned grid-cards: the part planner is asked."""
    fake.queue("planner", {"skill": "none", "reason": "a montage, not one effect"})  # the reel as a whole
    fake.queue("criteria", reply)
    fake.queue("planner", {"skill": "grid-cards", "reason": "the same kind of screen"})  # the surface
    fake.queue("judge", _judge_of(SURFACE, have_ok, True, True))  # grid-cards by ITS OWN stored tests
    if not have_ok:
        fake.queue("forge", {"code": CODE + "  # wall", "approach": "a"})
        fake.queue("judge", _judge_of(SURFACE2, True, True, True))
    fake.queue("compose", {**CHOICE, "sources": [source_slug]}).queue("judge", _judge(True, True, True))
    creature = _creature(home, fake)
    return creature, creature.try_reel("other.mp4", "New words.")


def test_an_installed_surface_is_reused_by_the_part_planner_without_a_forge_call(montage_world, fake_model):
    home, _ = montage_world
    _learn_montage(home, fake_model)  # grid-cards v1 and the timeline grid-promo
    second = FakeModel()
    creature, report = _second_montage(home, second, have_ok=True)
    assert report.status == "BUILT" and report.skill == "design:card-promo", report.gap
    assert _steps(second) == ["planner", "criteria", "planner", "judge", "compose", "judge"]  # no forge
    labels = [label for label, _ in second.calls[2].images]
    assert labels[0] == "this screen in the reel" and labels[1].startswith("skill grid-cards")
    [part] = [e for e in ledger.read(creature.ledger.path) if e["type"] == "part"]
    assert (part["status"], part["skill"], part["version"]) == ("have", "grid-cards", 1)
    root = home / "registry"
    assert registry.get(root, "grid-cards").version == 1
    with pytest.raises(KeyError):
        registry.get(root, "card-wall")  # nothing new was installed
    assert registry.design(root, "card-promo")["timeline"]["sources"]["a"]["skill"] == "grid-cards"


def test_a_reused_surface_that_fails_its_own_tests_is_learned_anew(montage_world, fake_model):
    home, _ = montage_world
    _learn_montage(home, fake_model)
    second = FakeModel()
    creature, report = _second_montage(home, second, have_ok=False, source_slug="card-wall")
    assert report.status == "BUILT", report.gap
    assert _steps(second) == ["planner", "criteria", "planner", "judge", "forge", "judge", "compose", "judge"]
    parts = [e for e in ledger.read(creature.ledger.path) if e["type"] == "part"]
    assert [p["status"] for p in parts] == ["have-failed", "built"]
    root = home / "registry"
    assert registry.get(root, "card-wall").version == 1 and registry.get(root, "grid-cards").version == 1
    [compose_call] = [c for c in second.calls if c.step == "compose"]
    catalog = compose_call.prompt.split("Catalog")[1]
    assert catalog.index("- card-wall") < catalog.index(
        "- grid-cards"
    )  # this run's surface first, all offered


def test_a_second_montage_with_the_same_name_does_not_replace_the_first(montage_world, fake_model):
    # "what can you do" must not shrink: a later reel with the same slug is another design, not a rewrite
    home, _ = montage_world
    _learn_montage(home, fake_model)
    root = home / "registry"
    first = registry.design(root, "grid-promo")
    second = FakeModel()
    _, report = _second_montage(home, second, have_ok=True, reply={**MONTAGE_REPLY_2, "slug": "grid-promo"})
    assert report.status == "BUILT", report.gap
    assert registry.design(root, "grid-promo") == first
    assert len(registry.designs(root)) == 2, registry.designs(root)
