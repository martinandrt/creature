import json

import pytest

from creature import criteria, registry
from creature.criteria import Spec


def _spec(slug="typewriter-reveal", text="Stay curious.", effect="Typewriter reveal") -> Spec:
    output = criteria.clip_format(3)
    return Spec(
        effect, slug, "try", "2D text", "Type it with a caret.", text, {"blinks": 8}, output,
        ("Characters appear one at a time.",), ("The line stays at the end.",),
        tuple(criteria.checks_for(output)),
    )  # fmt: skip


ORIGIN = {"reel": "https://www.instagram.com/p/X/", "run": "run-1"}
COST = {"learn_usd": 0.0054, "forge_attempts": 1}


@pytest.fixture
def root(home):
    return home / "registry"


def test_install_writes_the_skill_folder(root):
    skill = registry.install(root, _spec(), "def run(input, work):\n    return 1", origin=ORIGIN, cost=COST)
    assert skill.slug == "typewriter-reveal" and skill.version == 1
    assert sorted(p.name for p in skill.path.iterdir()) == [
        "SKILL.md",
        "capability.json",
        "skill.py",
        "tests.json",
    ]
    assert skill.capability["output"]["fps"] == 30 and skill.capability["origin"] == ORIGIN
    assert skill.tests["held_out"] == ["The line stays at the end."] and len(skill.tests["checks"]) == 8
    md = (skill.path / "SKILL.md").read_text()
    assert md.startswith("---\nname: typewriter-reveal\ndescription: ")


def test_learning_again_adds_a_version_and_keeps_the_old_one(root):
    registry.install(root, _spec(), "# v1", origin=ORIGIN, cost=COST)
    second = registry.install(root, _spec(text="Longer text"), "# v2", origin=ORIGIN, cost=COST)
    assert second.version == 2 and registry.get(root, "typewriter-reveal").code == "# v2"
    assert registry.get(root, "typewriter-reveal", 1).code == "# v1"
    registry.activate(root, "typewriter-reveal", 1)
    assert registry.get(root, "typewriter-reveal").version == 1
    with pytest.raises(KeyError):
        registry.activate(root, "typewriter-reveal", 7)


@pytest.mark.parametrize("slug", ["../evil", "Typewriter", "a/b"])
def test_bad_slug_never_becomes_a_folder(root, slug):
    with pytest.raises(ValueError):
        registry.install(root, _spec(slug=slug), "# x", origin=ORIGIN, cost=COST)
    assert not any(root.iterdir())


def test_design_is_data_over_known_skills(root):
    registry.install(root, _spec(), "# v1", origin=ORIGIN, cost=COST)
    steps = [{"skill": "typewriter-reveal", "version": 1, "params": {"blinks": 8}}]
    registry.save_design(root, "calm-intro", steps, origin=ORIGIN)
    assert registry.design(root, "calm-intro")["steps"] == steps and registry.designs(root) == ["calm-intro"]
    with pytest.raises(KeyError):
        registry.save_design(root, "broken", [{"skill": "nope", "version": 1}], origin=ORIGIN)
    with pytest.raises(ValueError):
        registry.save_design(root, "../up", steps, origin=ORIGIN)
    json.loads((root / "designs" / "calm-intro.json").read_text())


def test_search_ranks_by_shared_words(root):
    registry.install(root, _spec("typewriter-reveal"), "# a", origin=ORIGIN, cost=COST)
    registry.install(
        root, _spec("counter-roll", effect="Digits roll up to a number"), "# b", origin=ORIGIN, cost=COST
    )
    assert registry.search(root, "typewriter caret reveal")[0].slug == "typewriter-reveal"
    assert registry.search(root, "particles") == []


def test_unknown_skill_or_design(root):
    with pytest.raises(KeyError):
        registry.get(root, "nothing")
    with pytest.raises(KeyError):
        registry.design(root, "nothing")
