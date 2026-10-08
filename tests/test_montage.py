"""Montage as data: a timeline is the single source of time, validated before anything renders, and
the fixed checks on a rendered montage (smooth motion inside scenes, the layer in the safe zone)."""

import math

import pytest

from creature import composer, criteria, montage, registry, verdict
from creature.criteria import Spec

FPS = criteria.FPS
STYLE = {
    "colors": {"night": "#1B2135", "paper": "#EBEDF2", "amber": "#D4A574", "bad": "blue"},
    "fonts": {"title": "Barlow", "label": "JetBrains Mono", "body": "Comic Sans"},
}


def _timeline(frames=150, every=40, order=("a", "b"), layers=()):
    sources = {sid: {"skill": f"skill-{sid}", "version": 1, "params": {}} for sid in order}
    return montage.timeline("promo", sources, list(order), every=every, frames=frames, layers=list(layers))


# --- the timeline ------------------------------------------------------------------


def test_timeline_cuts_every_n_frames_and_the_sources_take_turns():
    t = _timeline()
    scenes = [(s["start"], s["dur"], s["source"]) for s in t["scenes"]]
    assert scenes == [(0, 40, "a"), (40, 40, "b"), (80, 40, "a"), (120, 30, "b")]  # the last one is cut short
    assert [c["frame"] for c in t["cues"]] == [0, 40, 80, 120] and {c["kind"] for c in t["cues"]} == {"cut"}
    assert (t["fps"], t["width"], t["height"], t["frames"]) == (FPS, criteria.WIDTH, criteria.HEIGHT, 150)
    assert montage.problems(t) == []


def test_timeline_clamps_length_and_rhythm_to_the_house_range():
    longest = round(criteria.MAX_S * FPS)
    t = _timeline(frames=10_000, every=1)
    assert t["frames"] == longest and t["scenes"][0]["dur"] == montage.MIN_EVERY
    assert _timeline(frames=1)["frames"] == FPS and _timeline(every=10_000)["scenes"][0]["dur"] == 150
    assert montage.frames_for(math.nan) == round(criteria.MIN_S * FPS)
    assert montage.frames_for(99) == longest and montage.frames_for(2.5) == 75


def test_problems_names_what_cannot_render():
    good = _timeline()
    assert any("sources" in p for p in montage.problems({**good, "sources": {}}))
    wrong = [{**good["scenes"][0], "source": "zz"}, *good["scenes"][1:]]
    assert any("unknown source" in p for p in montage.problems({**good, "scenes": wrong}))
    gap = [good["scenes"][0], {**good["scenes"][1], "start": 41}, *good["scenes"][2:]]
    assert any("follow" in p for p in montage.problems({**good, "scenes": gap}))
    assert any("cover" in p for p in montage.problems({**good, "frames": 151}))
    many = [{"id": f"l{i}", "height": 0.1} for i in range(montage.MAX_LAYERS + 1)]
    assert any("layers" in p for p in montage.problems({**good, "layers": many}))
    assert any("height" in p for p in montage.problems({**good, "layers": [{"id": "x", "height": 0.5}]}))


# --- the user's style ----------------------------------------------------------------


def test_style_gives_a_palette_and_known_fonts_only():
    params, notes = montage.style_params(STYLE)
    assert params["palette"] == ["#1B2135", "#EBEDF2", "#D4A574"]  # "blue" is not a colour value
    assert params["fonts"] == {
        "title": montage.FONT_FILES["Barlow"],
        "label": montage.FONT_FILES["JetBrains Mono"],
    }
    assert notes == ["font 'Comic Sans' is not in the workshop; skills keep their own"]
    assert montage.inks(STYLE) == {"dark": "#1B2135", "light": "#EBEDF2"}  # darkest and lightest
    assert montage.inks(None) == {"dark": "#111111", "light": "#f4f4f4"}
    assert montage.style_params(None) == ({}, [])


# --- the fixed checks ----------------------------------------------------------------


def test_checks_cover_the_total_the_cuts_and_the_layer_boxes():
    t = _timeline()
    checks = montage.checks(t, [[440, 1500, 200, 108]])
    by_kind = {c["kind"]: c for c in checks}
    assert {
        "exists",
        "video_stream",
        "resolution",
        "fps",
        "duration",
        "frames",
        "not_black",
    } <= by_kind.keys()
    assert by_kind["frames"]["expect"] == 150 and by_kind["duration"]["expect"] == 5.0
    assert by_kind["smooth"]["cuts"] == [0, 40, 80, 120] and by_kind["smooth"]["max_ratio"] == 1.5
    [box] = by_kind["safe_zone"]["boxes"]
    assert box == pytest.approx([440 / 1080, 1500 / 1920, 640 / 1080, 1608 / 1920])
    assert {c["file"] for c in checks} == {montage.MONTAGE}
    assert "safe_zone" not in {c["kind"] for c in montage.checks(t, [])}  # no layer, nothing to keep clear


def test_smooth_fails_a_dropped_frame_and_skips_cuts_and_stills():
    linear = [6.0] * 20
    assert verdict._smooth(linear, {0}, 1.5) is None
    dropped = [*linear[:10], 12.0, *linear[11:]]  # one frame missing: the step doubles once
    problem = verdict._smooth(dropped, {0}, 1.5)
    assert problem and problem.startswith("smooth:") and "2.0x" in problem
    assert verdict._smooth(dropped, {0, 10, 11}, 1.5) is None  # the same jump on a cut is a cut
    assert verdict._smooth([6.0, 0.2, 6.0], {0}, 1.5) is None  # a still frame is not motion
    assert verdict._smooth([], {0}, 1.5) == "smooth: the clip's motion could not be measured"


def test_safe_zone_keeps_layers_off_the_margins():
    safe = montage.SAFE
    inside = {"kind": "safe_zone", "file": "m", "boxes": [[0.3, 0.6, 0.7, 0.75]], "safe": safe}
    assert verdict._evaluate(inside, b"x", {}) is None
    low = {**inside, "boxes": [[0.3, 0.6, 0.7, 0.85]]}  # into the bottom 20 %
    assert (verdict._evaluate(low, b"x", {}) or "").startswith("safe_zone:")
    left = {**inside, "boxes": [[0.02, 0.6, 0.3, 0.7]]}
    assert (verdict._evaluate(left, b"x", {}) or "").startswith("safe_zone:")


# --- the composer's choice as a timeline -----------------------------------------------


def _skill(slug, version=1):
    return registry.Skill(slug, version, {"effect": slug}, "# code", {}, None)


def test_composer_choice_becomes_a_timeline_with_unknown_names_dropped_and_the_mark_clamped():
    catalog = [_skill("grid-cards"), _skill("type-wall", 2)]
    choice = {
        "sources": ["type-wall", "ghost", "grid-cards"],
        "every_frames": 4,
        "seconds": 6,
        "mark": {"use": True, "x": 0.99, "y": 0.01, "height": 0.9},
        "reason": "takes turns",
    }
    t = composer.timeline("promo", choice, catalog)
    assert t["sources"] == {
        "a": {"skill": "type-wall", "version": 2, "params": {}},
        "b": {"skill": "grid-cards", "version": 1, "params": {}},
    }
    assert [s["source"] for s in t["scenes"][:3]] == ["a", "b", "a"] and t["frames"] == 180
    assert len(t["scenes"]) == 45 and montage.problems(t) == []
    [mark] = t["layers"]
    assert (mark["x"], mark["y"], mark["height"]) == (0.9, 0.1, montage.MARK_HEIGHT[1])
    plain = composer.timeline("promo", {"sources": ["grid-cards"], "mark": {"use": False}}, catalog)
    assert plain["layers"] == [] and plain["scenes"][0]["dur"] == 4 and plain["frames"] == 180  # defaults


# --- saving a timeline -----------------------------------------------------------------


def _install(root, slug, ref):
    output = criteria.clip_format(3)
    spec = Spec(
        slug,
        slug,
        "try",
        "",
        "Draw it.",
        "Hi",
        {},
        output,
        ("a", "b"),
        ("c",),
        tuple(criteria.checks_for(output)),
    )
    return registry.install(root, spec, "# code", origin={"reel": "r", "run": "x"}, cost={}, reference=ref)


@pytest.fixture
def root(home, tmp_path):
    ref = tmp_path / "ref.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n")
    _install(home / "registry", "grid-cards", ref)
    return home / "registry"


def test_save_timeline_keeps_only_a_valid_timeline_over_known_skills(root):
    sources = {"a": {"skill": "grid-cards", "version": 1, "params": {}}}
    t = montage.timeline("promo", sources, ["a"], every=4, frames=180, layers=[])
    saved = registry.save_timeline(root, "promo", t, origin={"by": "test"}, tests={"criteria": ["x"]})
    assert saved["timeline"]["name"] == "promo" and saved["tests"] == {"criteria": ["x"]}
    assert registry.design(root, "promo")["timeline"]["frames"] == 180 and registry.designs(root) == ["promo"]
    assert registry._index(root)["designs"]["promo"]["kind"] == "timeline"
    with pytest.raises(KeyError):
        registry.save_timeline(
            root, "ghostly", {**t, "sources": {"a": {"skill": "ghost", "version": 1}}}, origin={}
        )
    with pytest.raises(ValueError, match="bad timeline"):
        registry.save_timeline(root, "short", {**t, "frames": 181}, origin={})
    with pytest.raises(FileExistsError):
        registry.taken(root, "promo")
    with pytest.raises(FileExistsError):
        registry.taken(root, "grid-cards")
    registry.taken(root, "fresh-name")
