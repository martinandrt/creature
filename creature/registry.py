"""The registry: learned skills, their versions, and designs (named recipes over skills).

registry/
  index.json                    active version per skill, and the designs
  <slug>/<version>/SKILL.md     what the effect is and when to use it (Agent Skills format)
  <slug>/<version>/capability.json   inputs, output clip format, origin, cost
  <slug>/<version>/skill.py     the script; it only ever runs in the workshop
  <slug>/<version>/tests.json   file checks and judge criteria, as data
  designs/<name>.json           a recipe: which skills, in which order, with which values

Only a creature run writes here, and only after a skill passed its checks and the judge. A skill
learned again becomes a new version; older versions stay, so a skill can evolve and roll back.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from creature.criteria import SLUG, Spec

INDEX = "index.json"
REFERENCE = "reference.png"
DESIGNS = "designs"
DESIGN_NAME = re.compile(r"[a-z][a-z0-9-]{2,40}")
INPUTS = {
    "type": "object",
    "required": ["text", "output"],
    "properties": {
        "text": {"type": "string", "minLength": 1},
        "params": {"type": "object"},
        "output": {"type": "object"},
    },
}


@dataclass(frozen=True)
class Skill:
    slug: str
    version: int
    capability: dict[str, Any]
    code: str
    tests: dict[str, Any]
    path: Path


def _index(root: Path) -> dict[str, Any]:
    path = root / INDEX
    if not path.is_file():
        return {"skills": {}, "designs": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("skills", {})
    data.setdefault("designs", {})
    return data


def _write_json(path: Path, data: Any) -> None:
    # write next to the target, then rename: a reader never sees half a file
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, suffix=".tmp", encoding="utf-8"
    ) as tmp:
        json.dump(data, tmp, indent=2, ensure_ascii=False)
        tmp.write("\n")
    os.replace(tmp.name, path)


def install(
    root: Path, spec: Spec, code: str, *, origin: dict[str, Any], cost: dict[str, Any], reference: Path
) -> Skill:
    """Install a skill that passed. A slug that exists gets the next version, which becomes active."""
    if not SLUG.fullmatch(spec.slug):
        raise ValueError(f"bad slug {spec.slug!r}")
    index = _index(root)
    entry = index["skills"].get(spec.slug, {"versions": []})
    version = max(entry["versions"], default=0) + 1
    folder = root / spec.slug / str(version)
    if folder.exists():
        raise FileExistsError(f"{folder} already exists")
    capability = {
        "name": spec.slug,
        "version": version,
        "effect": spec.effect,
        "summary": spec.task,
        "inputs": INPUTS,
        "input_files": [],
        "params": spec.params,
        "param_sources": spec.param_sources,
        "reason": spec.reason,
        "output": spec.output,
        "origin": origin,
        "cost": cost,
    }
    tests = {"checks": list(spec.checks), "criteria": list(spec.criteria), "held_out": list(spec.held_out)}
    staging = Path(tempfile.mkdtemp(dir=root, prefix=".install-"))
    (staging / "skill.py").write_text(code, encoding="utf-8")
    # the reel frames this skill was judged against; later uses are judged against them again
    (staging / REFERENCE).write_bytes(Path(reference).read_bytes())
    (staging / "SKILL.md").write_text(skill_md(spec, version, origin), encoding="utf-8")
    _write_json(staging / "capability.json", capability)
    _write_json(staging / "tests.json", tests)
    folder.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, folder)
    entry = {"versions": [*entry["versions"], version], "active": version, "effect": spec.effect}
    index["skills"][spec.slug] = entry
    _write_json(root / INDEX, index)
    return get(root, spec.slug)


def skill_md(spec: Spec, version: int, origin: dict[str, Any]) -> str:
    seconds = spec.output["duration_s"]
    return (
        f"---\nname: {spec.slug}\n"
        f"description: {spec.effect.splitlines()[0][:200]} Use it to animate a short text in this style.\n"
        "---\n\n"
        f"# {spec.slug} (v{version})\n\n{spec.effect}\n\n"
        f"- Input: `text`, optional `params` (defaults from the tutorial: {json.dumps(spec.params)}).\n"
        f"- Output: `{spec.output['file']}`, {spec.output['width']}x{spec.output['height']}, "
        f"{spec.output['fps']} fps, {seconds:.2f} s.\n"
        f"- Learned from {origin.get('reel', '?')} in run {origin.get('run', '?')}.\n"
        f"- Tests: `tests.json` ({len(spec.checks)} file checks, {len(spec.criteria) + len(spec.held_out)} "
        "criteria judged against the reel).\n"
        "- Runs only inside the creature's workshop container.\n"
    )


def get(root: Path, slug: str, version: int | None = None) -> Skill:
    entry = _index(root)["skills"].get(slug)
    if entry is None:
        raise KeyError(f"no skill {slug!r}")
    version = version or entry["active"]
    folder = root / slug / str(version)
    return Skill(
        slug=slug,
        version=version,
        capability=json.loads((folder / "capability.json").read_text(encoding="utf-8")),
        code=(folder / "skill.py").read_text(encoding="utf-8"),
        tests=json.loads((folder / "tests.json").read_text(encoding="utf-8")),
        path=folder,
    )


def spec_of(skill: Skill, text: str) -> Spec:
    """The spec a skill was installed with, on new text: its own criteria, checks and clip format."""
    cap, tests = skill.capability, skill.tests
    return Spec(
        effect=cap["effect"],
        slug=skill.slug,
        verdict="try",
        reason=cap.get("reason", ""),
        task=cap["summary"],
        text=text,
        params=cap.get("params", {}),
        output=cap["output"],
        criteria=tuple(tests["criteria"]),
        held_out=tuple(tests["held_out"]),
        checks=tuple(tests["checks"]),
        param_sources=cap.get("param_sources", {}),
    )


def skills(root: Path) -> list[Skill]:
    return [get(root, slug) for slug in sorted(_index(root)["skills"])]


def activate(root: Path, slug: str, version: int) -> None:
    """Roll a skill forward or back to a version that exists."""
    index = _index(root)
    entry = index["skills"].get(slug)
    if entry is None or version not in entry["versions"]:
        raise KeyError(f"no version {version} of {slug!r}")
    entry["active"] = version
    _write_json(root / INDEX, index)


def save_design(
    root: Path, name: str, steps: list[dict[str, Any]], *, origin: dict[str, Any]
) -> dict[str, Any]:
    """A design is data: skills in order, each with its version and values. It runs without a planner."""
    if not DESIGN_NAME.fullmatch(name):
        raise ValueError(f"bad design name {name!r}")
    index = _index(root)
    for step in steps:
        entry = index["skills"].get(step.get("skill"))
        if entry is None or step.get("version") not in entry["versions"]:
            raise KeyError(f"design step uses an unknown skill: {step}")
    design = {"name": name, "steps": steps, "origin": origin}
    _write_json(root / DESIGNS / f"{name}.json", design)
    index["designs"][name] = {"steps": len(steps), "origin": origin}
    _write_json(root / INDEX, index)
    return design


def design(root: Path, name: str) -> dict[str, Any]:
    if not DESIGN_NAME.fullmatch(name) or name not in _index(root)["designs"]:
        raise KeyError(f"no design {name!r}")
    return json.loads((root / DESIGNS / f"{name}.json").read_text(encoding="utf-8"))


def designs(root: Path) -> list[str]:
    return sorted(_index(root)["designs"])


def search(root: Path, words: str) -> list[Skill]:
    """Skills whose name or effect shares a word with `words`, best match first."""
    wanted = set(re.findall(r"[a-z0-9]+", words.lower()))
    scored = []
    for skill in skills(root):
        have = set(re.findall(r"[a-z0-9]+", f"{skill.slug} {skill.capability.get('effect', '')}".lower()))
        score = len(wanted & have)
        if score:
            scored.append((score, skill.slug, skill))
    return [skill for _, _, skill in sorted(scored, key=lambda item: (-item[0], item[1]))]
