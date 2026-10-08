"""One run of the creature: "try on my input what this reel shows".

perceive → criteria (before any attempt) → existing skill, or a gap → ask/refuse/skip, or forge until
the checks and the judge pass → install, save the design → the same fingerprint at the end.
Every step and every dollar goes to the run's ledger.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from creature import (
    authority,
    composer,
    criteria,
    forge,
    ledger,
    montage,
    page,
    perceive,
    planner,
    registry,
    tools,
    verdict,
    workshop,
)
from creature import wishes as wishes_mod
from creature.forge import Outcome
from creature.ledger import Ledger
from creature.llm import ClaudeCLI, Model, ModelError, Transport


@dataclass
class Report:
    run_id: str
    status: str  # HAVE · BUILT · FAILED · ASK · REFUSE · SKIP
    folder: Path
    spec: criteria.Spec | None = None
    skill: str | None = None
    attempts: int = 0
    gap: str = ""
    spent_usd: float = 0.0
    fingerprint_same: bool = False
    clip: Path | None = None
    notes: list[str] = field(default_factory=list)


class Creature:
    """The spine for one run: authority, image, ledger and model, all fixed at the start."""

    def __init__(self, home: Path, transport: Transport | None = None) -> None:
        self.home = home
        self.authority = authority.load(home)
        self.image = workshop.ensure_image(home / "workshop", self.authority.image)
        self.start = authority.fingerprint(home, image_id=self.image)
        self.ledger = Ledger.start(home)
        self.folder = home / "runs" / self.ledger.run_id
        self.folder.mkdir(parents=True, exist_ok=True)
        caps = self.authority.caps
        self.model = Model(
            transport or ClaudeCLI(), self.ledger, budget_usd=caps.run_budget_usd,
            reserve_usd=caps.call_reserve_usd, model=self.authority.models["default"],
        )  # fmt: skip
        self.registry = home / "registry"
        self.reel: perceive.Reel | None = None
        self.installed = 0
        self.ledger.record("run_start", fingerprint=self.start.digest, image=self.image)
        for problem in registry.broken(self.registry):
            self.ledger.record("seal_broken", problem=problem)

    def cap(self, step: str) -> float:
        return self.authority.caps.step_cap_usd[step]

    def model_for(self, step: str) -> str:
        return self.authority.models.get(step, self.authority.models["default"])

    def finish(self, report: Report) -> Report:
        end = authority.fingerprint(self.home, image_id=workshop.image_id(self.authority.image) or "missing")
        report.fingerprint_same = end.digest == self.start.digest
        report.spent_usd = self.ledger.spent_usd
        self.ledger.record(
            "run_end", status=report.status, skill=report.skill, attempts=report.attempts,
            spent_usd=round(report.spent_usd, 6), fingerprint=end.digest,
            fingerprint_same=report.fingerprint_same, changed=authority.changed(self.start, end),
            gap=report.gap[:1000], clip=_inside(report.clip, self.folder),
        )  # fmt: skip
        events = ledger.read(self.ledger.path)
        page.render(self.folder, report, events)
        page.overview(self.home)
        return report

    def run_skill(
        self, code: str, spec: criteria.Spec, attempt: Path, reference: Path
    ) -> tuple[Outcome, Path | None]:
        """Render with `code`, then check the file and ask the judge. Returns the outcome and the clip."""
        attempt.mkdir(parents=True, exist_ok=True)
        (attempt / "skill.py").write_text(code, encoding="utf-8")
        ran = workshop.run(code, forge.skill_input(spec), limits=self.authority.workshop, image=self.image)
        if not ran.ok:
            detail = {"stage": "workshop", "error": (ran.error or "")[:300], "killed": ran.killed}
            return Outcome(
                False, f"The script failed: {ran.error}\nContainer log: {ran.log[-800:]}", detail
            ), None
        checked = verdict.check(
            ran.outputs, spec.checks, limits=self.authority.workshop, image=self.image, folder=attempt
        )
        clip = attempt / spec.output["file"]
        if checked.problems or not checked.strip:
            detail = {"stage": "checks", "problems": list(checked.problems)}
            return Outcome(False, verdict.feedback(checked, None), detail), clip if clip.exists() else None
        (attempt / "strip.png").write_bytes(checked.strip)
        more = _sheets(attempt, checked.more)
        try:
            judged = verdict.judge(
                self.model, spec, reference, attempt / "strip.png", cap_usd=self.cap("judge"), more=more
            )
        except ModelError as error:  # no answer is not a pass; a refused budget stops the next forge call
            return Outcome(False, "", {"stage": "judge", "error": str(error)[:300]}), clip
        (attempt / "judge.json").write_text(
            json.dumps(judged.results, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        detail = {
            "stage": "judge", "passed": sum(r["pass"] for r in judged.results), "of": len(judged.results),
            "model_verdict": judged.model_verdict,
        }  # fmt: skip
        return Outcome(judged.ok, "" if judged.ok else verdict.feedback(checked, judged), detail), clip

    def try_reel(self, source: str, text: str, *, mark: Path | None = None) -> Report:
        report = Report(self.ledger.run_id, "FAILED", self.folder)
        try:
            return self._try_reel(report, source, text, mark)
        except ModelError as error:
            # a paid call that gave no usable answer (over its cap, refused, malformed): the run ends
            # cleanly with what it has, its page and its ledger, instead of a traceback
            report.status, report.gap = "FAILED", f"a model call failed: {error}"
            return self.finish(report)

    def _try_reel(self, report: Report, source: str, text: str, mark: Path | None) -> Report:
        self.ledger.record("task", source=source, text=text)
        self.reel = perceive.perceive(
            source, self.folder, self.ledger, limits=self.authority.workshop, image=self.image
        )
        # does an installed skill do this already? Then it is judged by its own stored tests
        skill, why = planner.match(
            self.model, self.reel, registry.skills(self.registry), cap_usd=self.cap("planner")
        )
        self.ledger.record("plan", skill=skill.slug if skill else None, reason=why)
        if skill:
            spec = registry.spec_of(skill, text)
            reference = skill.path / registry.REFERENCE
            report.spec = spec
            outcome, clip = self.run_skill(skill.code, spec, self.folder / f"have-{skill.slug}", reference)
            self.ledger.record(
                "have_try", skill=skill.slug, version=skill.version, ok=outcome.ok, detail=outcome.detail
            )
            if outcome.ok:
                report.status, report.skill, report.clip = "HAVE", f"{skill.slug}@v{skill.version}", clip
                return self.finish(report)
            # it fails its own tests on this input: learn the next version against the same tests
            self.ledger.record(
                "evolve", skill=skill.slug, from_version=skill.version, why=outcome.feedback[:400]
            )
            report.notes.append(
                f"{skill.slug} v{skill.version} failed its own tests here: learning v{skill.version + 1}"
            )
            return self.learn(report, spec, source, reference)

        spec = criteria.write(
            self.model, self.reel, text, cap_usd=self.cap("criteria"), seed=self.ledger.run_id,
            refuse=self.authority.refuse, ask=self.authority.ask, model_name=self.model_for("criteria"),
        )  # fmt: skip
        report.spec = spec
        self.ledger.record(
            "spec", verdict=spec.verdict, slug=spec.slug, effect=spec.effect[:300], reason=spec.reason[:300],
            visible=len(spec.criteria), held_out=len(spec.held_out), duration_s=spec.output["duration_s"],
            params=len(spec.params), kind=spec.kind, parts=[p.slug for p in spec.parts], gaps=len(spec.gaps),
        )  # fmt: skip
        (self.folder / "spec.json").write_text(
            json.dumps(_spec_dict(spec), indent=1, ensure_ascii=False), encoding="utf-8"
        )
        if spec.verdict in ("ask", "refuse", "skip"):
            report.status = spec.verdict.upper()
            report.gap = spec.reason
            if spec.verdict == "ask":
                _queue_ask(self.home, self.ledger.run_id, source, spec)
            return self.finish(report)
        if spec.kind == "montage" and spec.parts:
            return self.learn_montage(report, spec, source, text, mark=mark)
        if spec.kind == "montage":  # nothing a script can draw: what it would need is a request, not a try
            report.status = "ASK" if spec.gaps else "FAILED"
            report.gap = (
                "; ".join(f"{g['what']} (needs {g['needs']})" for g in spec.gaps) or "no usable surface"
            )
            if spec.gaps:
                _queue_ask(
                    self.home, self.ledger.run_id, source, dataclasses.replace(spec, reason=report.gap)
                )
            return self.finish(report)
        return self.learn(report, spec, source, self.reel.strip)

    def learn(self, report: Report, spec: criteria.Spec, source: str, reference: Path) -> Report:
        """Forge until the checks and the judge pass, then install (a new skill or its next version)."""
        skill, built, clip = self.learn_one(spec, source, reference)
        report.attempts = built.attempts if built else 0
        report.clip = clip  # the last attempt's clip, kept on a failure too
        if skill is None:
            report.status, report.gap = (
                "FAILED",
                built.gap if built else "cap: no more new skills in this run",
            )
            return self.finish(report)
        steps = [{"skill": skill.slug, "version": skill.version, "params": spec.params}]
        registry.save_design(self.registry, spec.slug, steps, origin=skill.capability["origin"])
        report.status, report.skill = "BUILT", f"{skill.slug}@v{skill.version}"
        return self.finish(report)

    def learn_one(
        self, spec: criteria.Spec, source: str, reference: Path, prefix: str = ""
    ) -> tuple[registry.Skill | None, forge.Build | None, Path | None]:
        """One skill: forge until its checks and its judge pass, then install. No skill on a failure."""
        if self.installed >= self.authority.caps.new_skills_per_run:
            self.ledger.record("cap_reached", cap="new_skills_per_run", skill=spec.slug)
            return None, None, None
        clips: dict[int, Path | None] = {}

        def try_code(code: str, number: int) -> Outcome:
            folder = self.folder / f"{prefix}attempt-{number}"
            outcome, clips[number] = self.run_skill(code, spec, folder, reference)
            return outcome

        before = self.ledger.spent_usd
        built = forge.build(
            self.model, spec, self.ledger, attempts=self.authority.caps.forge_attempts,
            cap_usd=self.cap("forge"), try_code=try_code,
        )  # fmt: skip
        clip = clips.get(built.attempts)
        if not built.ok:
            return None, built, clip
        # authority, image and enforcing code must be what they were at the start, right before install
        now = authority.fingerprint(self.home, image_id=workshop.image_id(self.authority.image) or "missing")
        if now.digest != self.start.digest:
            changed = authority.changed(self.start, now)
            self.ledger.record("install_refused", changed=changed)
            gap = f"not installed: the authority fingerprint changed mid-run ({', '.join(changed)})"
            return None, dataclasses.replace(built, ok=False, gap=gap), clip
        origin = {"reel": source, "run": self.ledger.run_id, "author": self.reel.author if self.reel else ""}
        cost = {"learn_usd": round(self.ledger.spent_usd - before, 6), "forge_attempts": built.attempts}
        skill = registry.install(
            self.registry,
            spec,
            built.code,
            origin=origin,
            cost=cost,
            reference=reference,
            staging=self.folder,
        )
        self.installed += 1
        self.ledger.record("installed", skill=skill.slug, version=skill.version, design=spec.slug, **cost)
        return skill, built, clip

    def learn_montage(
        self, report: Report, spec: criteria.Spec, source: str, text: str, *, mark: Path | None
    ) -> Report:
        """A montage reel: learn each surface on its own (a stuck one is skipped), then the creature
        composes them; the spine renders, measures and the judge compares the montage with the reel."""
        assert self.reel is not None
        learned: list[str] = []
        for part in spec.parts:
            have = next((s for s in registry.skills(self.registry) if s.slug == part.slug), None)
            if have:
                self.ledger.record("part", slug=part.slug, status="have", version=have.version)
                learned.append(part.slug)
                continue
            reference = perceive.frames_at(
                self.reel, part.frames, self.folder / f"part-{part.slug}.png",
                limits=self.authority.workshop, image=self.image,
            )  # fmt: skip
            skill, built, _ = self.learn_one(part, source, reference, prefix=f"{part.slug}-")
            report.attempts += built.attempts if built else 0
            self.ledger.record(
                "part", slug=part.slug, status="built" if skill else "skipped",
                attempts=built.attempts if built else 0, gap=(built.gap if built and not skill else "")[:300],
            )  # fmt: skip
            if skill:
                learned.append(skill.slug)
            else:
                report.notes.append(f"surface {part.slug} skipped: {(built.gap if built else 'cap')[:160]}")
        for gap in spec.gaps:
            report.notes.append(f"gap: {gap['what']} (needs {gap['needs']})")
        if spec.gaps:
            self.ledger.record("gaps", gaps=list(spec.gaps))
        effects = [s for s in registry.skills(self.registry) if s.capability.get("kind") != "tool"]
        catalog = [s for s in effects if s.slug in learned]
        if not catalog:
            report.status, report.gap = "FAILED", "no surface could be learned"
            return self.finish(report)
        feedback = ""
        for attempt in range(1, 3):
            choice, unknown = composer.choose(
                self.model, self.reel, spec, catalog, cap_usd=self.cap("planner"), feedback=feedback
            )
            t = composer.timeline(spec.slug, choice, catalog)
            wrong = montage.problems(t)
            if wrong:
                self.ledger.record("composed", attempt=attempt, ok=False, unknown=unknown, problems=wrong)
                feedback = "; ".join(wrong) + f". Use only these names: {', '.join(s.slug for s in catalog)}"
                continue
            self.ledger.record(
                "composed", attempt=attempt, sources=[v["skill"] for v in t["sources"].values()],
                every=t["scenes"][0]["dur"] if t["scenes"] else 0, frames=t["frames"],
                layers=len(t["layers"]), unknown=unknown, reason=str(choice.get("reason", ""))[:300],
            )  # fmt: skip
            folder = self.folder / f"montage-{attempt}"
            clip, problems, checked = self.render_timeline(t, text, folder, mark=mark, style=None)
            report.clip = clip or report.clip
            if problems or checked is None or not checked.strip:
                feedback = "\n".join(f"Check failed: {p}" for p in problems)
                continue
            judged = verdict.judge(
                self.model, spec, self.reel.strip, folder / "strip.png", cap_usd=self.cap("judge"),
                more=tuple(sorted(folder.glob("strip-*.png"))),
            )  # fmt: skip
            (folder / "judge.json").write_text(json.dumps(judged.results, indent=1, ensure_ascii=False))
            self.ledger.record(
                "montage_judged", attempt=attempt, ok=judged.ok,
                passed=sum(r["pass"] for r in judged.results), of=len(judged.results),
            )  # fmt: skip
            if judged.ok:
                origin = {"reel": source, "run": self.ledger.run_id, "author": self.reel.author}
                tests = {"criteria": list(spec.criteria), "held_out": list(spec.held_out)}
                registry.save_timeline(self.registry, spec.slug, t, origin=origin, tests=tests)
                report.status, report.skill = "BUILT", f"design:{spec.slug}"
                return self.finish(report)
            feedback = verdict.feedback(checked, judged)
        report.status, report.gap = "FAILED", feedback or "the montage did not pass"
        return self.finish(report)

    def wishes(self) -> Report:
        """Read every run's ledger, name what is missing and how many reels each would unlock."""
        report = Report(self.ledger.run_id, "FAILED", self.folder, skill="wishes")
        self.ledger.record("wishes_task")
        try:
            found = wishes_mod.wish(self.model, self.home, cap_usd=self.cap("planner"))
        except ModelError as error:
            report.gap = f"a model call failed: {error}"
            return self.finish(report)
        self.ledger.record("wishes", runs=found["runs"], wishes=found["wishes"])
        for w in found["wishes"]:
            report.notes.append(
                f"[{w['kind']}] {w['capability']}: unlocks {w['unlocks']} ({', '.join(w['reels'])})"
            )
        report.status = "DONE"
        return self.finish(report)

    def build_tool(self, wish: str | None = None) -> Report:
        """Build a tool the creature wished for: spec with test cases (a model call), clips for the cases
        (fixed code), forge, exact comparison of the answers, install as a skill of kind "tool"."""
        report = Report(self.ledger.run_id, "FAILED", self.folder, skill="tool")
        if wish is None:
            found = json.loads((self.home / wishes_mod.WISHES).read_text(encoding="utf-8"))
            first = next((w for w in found.get("wishes", []) if w.get("kind") == "tool"), None)
            if first is None:
                report.gap = "no tool among the wishes"
                return self.finish(report)
            wish = f"{first['capability']}. Why: {first.get('why', '')}"
        self.ledger.record("tool_task", wish=wish[:600])
        try:
            spec, cases = tools.write_spec(
                self.model, wish, cap_usd=self.cap("criteria"), seed=self.ledger.run_id,
                model_name=self.model_for("criteria"),
            )  # fmt: skip
        except (ModelError, ValueError) as error:
            report.gap = f"no usable tool spec: {error}"
            return self.finish(report)
        report.spec = spec
        self.ledger.record(
            "tool_spec", slug=spec.slug, purpose=spec.effect[:300], returns=spec.output["returns"][:300],
            cases=len(cases), visible=len(spec.criteria), held_out=len(spec.held_out),
        )  # fmt: skip
        (self.folder / "spec.json").write_text(json.dumps(_spec_dict(spec), indent=1, ensure_ascii=False))
        place = self.folder / "cases"
        place.mkdir(exist_ok=True)
        clips = {}
        for number, case in enumerate(cases):
            clips[case["name"]] = place / f"case-{number}.mp4"
            clips[case["name"]].write_bytes(
                tools.fixture(case, limits=self.authority.workshop, image=self.image)
            )

        def try_code(code: str, number: int) -> Outcome:
            folder = self.folder / f"attempt-{number}"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "skill.py").write_text(code, encoding="utf-8")
            results, seen, hidden = {}, [], 0
            for case in cases:
                ran = workshop.run(
                    code, {"clip": "/work/in/clip.mp4", "params": {}}, {"clip.mp4": clips[case["name"]]},
                    limits=self.authority.workshop, image=self.image,
                )  # fmt: skip
                wrong = (
                    [f"the script failed: {ran.error}"] if not ran.ok
                    else tools.compare(case["expect"], ran.value, float(case["tolerance"]))
                )  # fmt: skip
                results[case["name"]] = {
                    "ok": not wrong,
                    "got": ran.value if ran.ok else None,
                    "wrong": wrong,
                }
                if wrong and case["name"] in spec.held_out:
                    hidden += 1
                elif wrong:
                    seen += [f"Case {case['name']}: {w}" for w in wrong[:4]]
            (folder / "results.json").write_text(
                json.dumps(results, indent=1, ensure_ascii=False, default=str)
            )
            passed = sum(r["ok"] for r in results.values())
            feedback = "\n".join(
                seen + ([f"{hidden} more cases you were not shown also failed."] if hidden else [])
            )
            detail = {"stage": "cases", "passed": passed, "of": len(cases)}
            return Outcome(passed == len(cases), feedback, detail)

        before = self.ledger.spent_usd
        built = forge.build(
            self.model, spec, self.ledger, attempts=self.authority.caps.forge_attempts,
            cap_usd=self.cap("forge"), try_code=try_code, system=tools.TOOL_SYSTEM, write=tools.prompt,
        )  # fmt: skip
        report.attempts = built.attempts
        if not built.ok:
            report.gap = built.gap
            return self.finish(report)
        now = authority.fingerprint(self.home, image_id=workshop.image_id(self.authority.image) or "missing")
        if now.digest != self.start.digest:
            report.gap = "not installed: the authority fingerprint changed mid-run"
            self.ledger.record("install_refused", changed=authority.changed(self.start, now))
            return self.finish(report)
        first = clips[cases[0]["name"]]
        sheet = workshop.run(
            perceive.FRAMES_AT_CODE, {"times": [0.0]}, {"reel.mp4": first}, limits=self.authority.workshop,
            image=self.image,
        )  # fmt: skip
        reference = self.folder / "case-frame.png"
        reference.write_bytes(sheet.outputs.get("strip.png", b""))
        origin = {"wish": wish[:300], "run": self.ledger.run_id}
        cost = {"learn_usd": round(self.ledger.spent_usd - before, 6), "forge_attempts": built.attempts}
        skill = registry.install(
            self.registry, spec, built.code, origin=origin, cost=cost, reference=reference,
            staging=self.folder, kind="tool",
        )  # fmt: skip
        self.ledger.record("installed", skill=skill.slug, version=skill.version, kind="tool", **cost)
        report.status, report.skill = "BUILT", f"{skill.slug}@v{skill.version}"
        return self.finish(report)

    def use_tool(self, slug: str, clip: Path) -> dict[str, Any]:
        """Run an installed tool on a clip: no model, the workshop only."""
        skill = registry.get(self.registry, slug)
        if skill.capability.get("kind") != "tool":
            raise ValueError(f"{slug} is not a tool")
        ran = workshop.run(
            skill.code, {"clip": "/work/in/clip.mp4", "params": {}}, {"clip.mp4": clip},
            limits=self.authority.workshop, image=self.image,
        )  # fmt: skip
        self.ledger.record(
            "tool_used", skill=slug, version=skill.version, ok=ran.ok, error=(ran.error or "")[:300]
        )
        return {"ok": ran.ok, "result": ran.value, "error": ran.error}

    def render_timeline(
        self, t: dict[str, Any], text: str, folder: Path, *, mark: Path | None, style: dict[str, Any] | None
    ) -> tuple[Path | None, list[str], verdict.Checked | None]:
        """Each source once at the timeline's length (with the user's style over its own values), then
        the montage, then the montage's fixed checks. No model. Returns the clip and what failed."""
        extra, notes = montage.style_params(style)
        for note in notes:
            self.ledger.record("style_note", note=note)
        clips = {}
        for sid, source in sorted(t["sources"].items()):
            skill = registry.get(self.registry, source["skill"], source["version"])  # seals checked here
            base = registry.spec_of(skill, text)
            output = {**base.output, "duration_s": t["frames"] / t["fps"]}
            spec = dataclasses.replace(
                base, params={**base.params, **source.get("params", {}), **extra}, output=output,
                checks=tuple(criteria.checks_for(output)),
            )  # fmt: skip
            ran = workshop.run(
                skill.code, forge.skill_input(spec), limits=self.authority.workshop, image=self.image
            )
            place = folder / f"source-{sid}"
            place.mkdir(parents=True, exist_ok=True)
            checked = (
                verdict.check(ran.outputs, spec.checks, limits=self.authority.workshop, image=self.image,
                              folder=place)
                if ran.ok else None
            )  # fmt: skip
            problems = [f"the script failed: {ran.error}"] if not ran.ok else list(checked.problems)
            self.ledger.record(
                "montage_source", source=sid, skill=skill.slug, version=skill.version, ok=not problems,
                checks=problems or "all passed", seconds=ran.duration_s,
            )  # fmt: skip
            if problems:
                return None, [f"source {sid} ({skill.slug}): " + "; ".join(problems)], None
            clips[sid] = place / output["file"]
        ran = montage.render(
            t, clips, text=text, mark=mark, style=style, limits=self.authority.workshop, image=self.image
        )
        if not ran.ok or montage.MONTAGE not in ran.outputs:
            self.ledger.record("montage", ok=False, error=(ran.error or "")[:300])
            return None, [f"the montage failed: {ran.error}"], None
        checked = verdict.check(
            ran.outputs, tuple(montage.checks(t, ran.value.get("layers", []))),
            limits=self.authority.workshop, image=self.image, folder=folder,
        )  # fmt: skip
        if checked.strip:
            (folder / "strip.png").write_bytes(checked.strip)
            _sheets(folder, checked.more)
        self.ledger.record(
            "montage", ok=not checked.problems, frames=t["frames"], scenes=len(t["scenes"]),
            sources=len(clips), inks=ran.value.get("inks", [])[:60], seconds=ran.duration_s,
            checks=list(checked.problems) or "all passed",
        )  # fmt: skip
        return folder / montage.MONTAGE, list(checked.problems), checked

    def run_design(
        self,
        name: str,
        text: str | list[str],
        *,
        judge: bool = False,
        mark: Path | None = None,
        style: dict[str, Any] | None = None,
    ) -> Report:
        """Run a saved design on new text: its skills in order, no planner, no forge. With judge=False
        (the default) no model is called at all: only the workshop and the fixed file checks run.
        One text goes to every step; a list gives each step its own. A timeline design is a montage:
        `mark` is the user's image for its layer (else the text's first word), `style` the user's own
        colours and fonts, which win over the values the sources were learned with."""
        report = Report(self.ledger.run_id, "FAILED", self.folder, skill=f"design:{name}")
        recipe = registry.design(self.registry, name)
        if "timeline" in recipe:
            t = recipe["timeline"]
            if judge:
                report.notes.append("--judge is not used for a montage design yet: its fixed checks run")
            words = text if isinstance(text, str) else " ".join(text)
            self.ledger.record(
                "design_task", design=name, text=words, kind="timeline", sources=len(t["sources"]),
                frames=t["frames"], mark=bool(mark), style=bool(style),
            )  # fmt: skip
            clip, problems, _ = self.render_timeline(
                t, words, self.folder / "montage", mark=mark, style=style
            )
            report.clip = clip
            if problems:
                report.gap = "; ".join(problems)
                return self.finish(report)
            report.status = "DONE"
            return self.finish(report)
        texts = [text] if isinstance(text, str) else list(text)
        self.ledger.record(
            "design_task", design=name, text=" | ".join(texts), texts=texts, steps=len(recipe["steps"]),
            judge=judge,
        )  # fmt: skip
        if len(texts) not in (1, len(recipe["steps"])):
            report.gap = f"design {name} has {len(recipe['steps'])} steps: give one text or one per step"
            return self.finish(report)
        clips: list[Path] = []
        frames = 0
        for number, step in enumerate(recipe["steps"], start=1):
            skill = registry.get(self.registry, step["skill"], step["version"])  # sealed tests checked here
            step_text = texts[0] if len(texts) == 1 else texts[number - 1]
            spec = dataclasses.replace(registry.spec_of(skill, step_text), params=step.get("params", {}))
            folder = self.folder / f"step-{number}-{skill.slug}"
            folder.mkdir(parents=True, exist_ok=True)
            ran = workshop.run(
                skill.code, forge.skill_input(spec), limits=self.authority.workshop, image=self.image
            )
            if not ran.ok:
                self.ledger.record(
                    "design_step", step=number, skill=skill.slug, version=skill.version, ok=False,
                    error=(ran.error or "")[:300],
                )  # fmt: skip
                report.gap = f"step {number} ({skill.slug}) failed: {ran.error}"
                return self.finish(report)
            checked = verdict.check(
                ran.outputs, spec.checks, limits=self.authority.workshop, image=self.image, folder=folder
            )
            ok = not checked.problems
            judged = None
            if ok and judge and checked.strip:
                (folder / "strip.png").write_bytes(checked.strip)
                reference = skill.path / registry.REFERENCE
                judged = verdict.judge(
                    self.model, spec, reference, folder / "strip.png", cap_usd=self.cap("judge"),
                    more=_sheets(folder, checked.more),
                )  # fmt: skip
                ok = judged.ok
            self.ledger.record(
                "design_step", step=number, skill=skill.slug, version=skill.version, ok=ok,
                checks=list(checked.problems) or "all passed", judged=judged.ok if judged else None,
                seconds=ran.duration_s,
            )  # fmt: skip
            if not ok:
                report.gap = f"step {number} ({skill.slug}): " + "; ".join(
                    checked.problems or ["judge said no"]
                )
                return self.finish(report)
            clips.append(folder / spec.output["file"])
            frames += round(spec.output["duration_s"] * spec.output["fps"])
        if len(clips) == 1:
            report.clip = clips[0]
        else:
            joined, problems = self.join(clips, frames)
            self.ledger.record(
                "design_join", parts=len(clips), frames=frames, checks=problems or "all passed"
            )
            if problems:
                report.gap = "joining the steps: " + "; ".join(problems)
                return self.finish(report)
            report.clip = joined
        report.status = "DONE"
        return self.finish(report)

    def join(self, clips: list[Path], frames: int) -> tuple[Path | None, list[str]]:
        """Clips of a design share one format, so they are joined without re-encoding. The result
        must hold exactly the steps' frames together, checked by the same fixed file checks."""
        files = {f"part{i:02d}.mp4": clip for i, clip in enumerate(clips)}
        ran = workshop.run(
            JOIN_CODE, {"parts": sorted(files)}, files, limits=self.authority.workshop, image=self.image
        )
        if not ran.ok or JOINED not in ran.outputs:
            return None, [f"the join failed: {ran.error}"]
        fps = criteria.FPS
        output = {"file": JOINED, "width": criteria.WIDTH, "height": criteria.HEIGHT, "fps": fps,
                  "duration_s": frames / fps}  # fmt: skip
        checked = verdict.check(  # also writes the joined clip into the run's folder
            ran.outputs, tuple(criteria.checks_for(output)), limits=self.authority.workshop,
            image=self.image, folder=self.folder,
        )  # fmt: skip
        return self.folder / JOINED, list(checked.problems)


# Fixed code (ours) that runs in the workshop: concatenates same-format clips.
JOINED = "joined.mp4"
JOIN_CODE = r"""
import subprocess

def run(input, work):
    with open(f"{work}/list.txt", "w") as f:
        for name in input["parts"]:
            f.write(f"file '{work}/in/{name}'\n")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", f"{work}/list.txt",
                    "-c", "copy", f"{work}/out/joined.mp4"], check=True)
    return {"parts": len(input["parts"])}
"""


def _sheets(folder: Path, sheets: tuple[bytes, ...]) -> tuple[Path, ...]:
    """Further sheets of a long clip's frames, next to its strip.png."""
    paths = []
    for number, data in enumerate(sheets, start=2):
        path = folder / f"strip-{number}.png"
        path.write_bytes(data)
        paths.append(path)
    return tuple(paths)


def _inside(path: Path | None, folder: Path) -> str | None:
    """A path inside the run's folder, written relative so the ledger never holds a home path."""
    if path is None:
        return None
    try:
        return str(Path(path).resolve().relative_to(folder.resolve()))
    except ValueError:
        return None


def _spec_dict(spec: criteria.Spec) -> dict[str, Any]:
    return dataclasses.asdict(spec)  # a montage's surfaces are specs too


def _queue_ask(home: Path, run_id: str, source: str, spec: criteria.Spec) -> None:
    """An ASK waits in queue/ for a human; the creature never grants itself anything."""
    (home / "queue").mkdir(exist_ok=True)
    request = {
        "run": run_id,
        "reel": source,
        "effect": spec.effect,
        "needs": spec.reason,
        "status": "waiting",
    }
    (home / "queue" / f"{run_id}.json").write_text(
        json.dumps(request, indent=1, ensure_ascii=False), encoding="utf-8"
    )
