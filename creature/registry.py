"""The registry: learned skills, their versions, and designs (named recipes over skills).

registry/
  index.json                    active version per skill, and the designs
  <slug>/<version>/SKILL.md     what the effect is and when to use it (Agent Skills format)
  <slug>/<version>/capability.json   inputs, output clip format, origin, cost
  <slug>/<version>/skill.py     the script; it only ever runs in the workshop
  <slug>/<version>/tests.json   file checks and judge criteria, as data
  designs/<name>.json           a recipe: which skills, in which order, with which values; or a
                                timeline (sources, scenes, layers, cues) over several skills

Only a creature run writes here, and only after a skill passed its checks and the judge. A skill
learned again becomes a new version; older versions stay, so a skill can evolve and roll back.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from creature.criteria import SLUG, Spec

INDEX = "index.json"
SEALED = ("skill.py", "tests.json", "reference.png")  # hashed at install, checked on every read
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
    root: Path,
    spec: Spec,
    code: str,
    *,
    origin: dict[str, Any],
    cost: dict[str, Any],
    reference: Path,
    staging: Path | None = None,
    kind: str = "effect",
) -> Skill:
    """Install a skill that passed. A slug that exists gets the next version, which becomes active.
    `kind` is "effect" (renders a clip) or "tool" (returns data about a clip)."""
    if not SLUG.fullmatch(spec.slug):
        raise ValueError(f"bad slug {spec.slug!r}")
    index = _index(root)
    entry = index["skills"].get(spec.slug, {"versions": []})
    # a folder left by a crash before the index write still takes its number
    on_disk = [int(p.name) for p in (root / spec.slug).glob("*") if p.name.isdigit()]
    version = max([*entry["versions"], *on_disk], default=0) + 1
    folder = root / spec.slug / str(version)
    if folder.exists():
        raise FileExistsError(f"{folder} already exists")
    capability = {
        "name": spec.slug,
        "kind": kind,
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
    # built outside the registry (in the run's folder), then moved in whole: a failed install leaves
    # its half-built folder with the run as evidence and nothing in the registry
    parent = staging if staging is not None else root.parent
    parent.mkdir(parents=True, exist_ok=True)
    built = Path(tempfile.mkdtemp(dir=parent, prefix="install-"))
    (built / "skill.py").write_text(code, encoding="utf-8")
    # the reel frames this skill was judged against; later uses are judged against them again
    (built / REFERENCE).write_bytes(Path(reference).read_bytes())
    (built / "SKILL.md").write_text(skill_md(spec, version, origin), encoding="utf-8")
    _write_json(built / "tests.json", tests)
    capability["sha256"] = {name: _sha256(built / name) for name in SEALED}
    _write_json(built / "capability.json", capability)
    folder.parent.mkdir(parents=True, exist_ok=True)
    os.replace(built, folder)
    entry = {"versions": [*entry["versions"], version], "active": version, "effect": spec.effect}
    index["skills"][spec.slug] = entry
    _write_json(root / INDEX, index)
    return get(root, spec.slug)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"


def skill_md(spec: Spec, version: int, origin: dict[str, Any]) -> str:
    first_line = (spec.effect.splitlines() or [""])[0][:200]
    if "duration_s" not in spec.output:  # a tool: it answers about a clip, it renders none
        return (
            f"---\nname: {spec.slug}\ndescription: {first_line}\n---\n\n"
            f"# {spec.slug} (v{version}, tool)\n\n{spec.effect}\n\n"
            f"- Input: `clip` (an H.264 MP4), optional `params`.\n"
            f"- Output: a JSON object. {spec.output.get('returns', '')}\n"
            f"- Wished for after: {origin.get('wish', '?')} (run {origin.get('run', '?')}).\n"
            f"- Tests: `tests.json` ({len(spec.checks)} cases rendered by fixed code, compared exactly).\n"
            "- Runs only inside the creature's workshop container.\n"
        )
    seconds = spec.output["duration_s"]
    return (
        f"---\nname: {spec.slug}\n"
        f"description: {first_line} Use it to animate a short text in this style.\n"
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
    capability = json.loads((folder / "capability.json").read_text(encoding="utf-8"))
    sealed = capability.get("sha256", {})
    for name in SEALED:
        if sealed.get(name) != _sha256(folder / name):
            raise ValueError(f"{slug} v{version}: {name} changed after install (tests are sealed)")
    return Skill(
        slug=slug,
        version=version,
        capability=capability,
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
    """Every skill whose seal holds. A skill with a broken seal is never offered for use."""
    found = []
    for slug in sorted(_index(root)["skills"]):
        try:
            found.append(get(root, slug))
        except ValueError:
            continue
    return found


def broken(root: Path) -> list[str]:
    """Skills whose files changed after install; listed so a human can see them, never used."""
    bad = []
    for slug in sorted(_index(root)["skills"]):
        try:
            get(root, slug)
        except ValueError as error:
            bad.append(str(error))
    return bad


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


def save_timeline(
    root: Path, name: str, t: dict[str, Any], *, origin: dict[str, Any], tests: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A montage design: the timeline is the only source of time; its tests are what it passed."""
    from creature import montage

    if not DESIGN_NAME.fullmatch(name):
        raise ValueError(f"bad design name {name!r}")
    index = _index(root)
    for source in t.get("sources", {}).values():
        entry = index["skills"].get(source.get("skill"))
        if entry is None or source.get("version") not in entry["versions"]:
            raise KeyError(f"timeline source uses an unknown skill: {source}")
    found = montage.problems(t)
    if found:
        raise ValueError("bad timeline: " + "; ".join(found))
    design = {"name": name, "timeline": {**t, "name": name}, "tests": tests or {}, "origin": origin}
    _write_json(root / DESIGNS / f"{name}.json", design)
    index["designs"][name] = {"steps": len(t["sources"]), "kind": "timeline", "origin": origin}
    _write_json(root / INDEX, index)
    return design


def design(root: Path, name: str) -> dict[str, Any]:
    if not DESIGN_NAME.fullmatch(name) or name not in _index(root)["designs"]:
        raise KeyError(f"no design {name!r}")
    return json.loads((root / DESIGNS / f"{name}.json").read_text(encoding="utf-8"))


def designs(root: Path) -> list[str]:
    return sorted(_index(root)["designs"])


def taken(root: Path, name: str) -> None:
    """A new design by hand never replaces a design, nor takes a skill's name (it would shadow it)."""
    index = _index(root)
    if name in index["designs"] or name in index["skills"]:
        raise FileExistsError(f"{name!r} is already a design or a skill; pick another name")


def compose(root: Path, name: str, parts: list[str], *, origin: dict[str, Any]) -> dict[str, Any]:
    """A new design from known parts, in order: a design adds its steps, a skill adds its active
    version with the values it was learned with. Data only, no model."""
    if len(parts) < 2:
        raise ValueError("a composed design needs at least two parts")
    taken(root, name)
    known = _index(root)["designs"]
    steps: list[dict[str, Any]] = []
    for part in parts:
        if part in known:
            steps.extend(design(root, part)["steps"])
        else:
            skill = get(root, part)  # KeyError for an unknown name, ValueError for a broken seal
            steps.append(
                {"skill": skill.slug, "version": skill.version, "params": skill.capability.get("params", {})}
            )
    return save_design(root, name, steps, origin={**origin, "parts": list(parts)})


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
