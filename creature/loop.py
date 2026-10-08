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

from creature import authority, criteria, forge, ledger, page, perceive, planner, registry, verdict, workshop
from creature.forge import Outcome
from creature.ledger import Ledger
from creature.llm import ClaudeCLI, Model, Transport


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
        judged = verdict.judge(self.model, spec, reference, attempt / "strip.png", cap_usd=self.cap("judge"))
        (attempt / "judge.json").write_text(
            json.dumps(judged.results, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        detail = {
            "stage": "judge", "passed": sum(r["pass"] for r in judged.results), "of": len(judged.results),
            "model_verdict": judged.model_verdict,
        }  # fmt: skip
        return Outcome(judged.ok, "" if judged.ok else verdict.feedback(checked, judged), detail), clip

    def try_reel(self, source: str, text: str) -> Report:
        report = Report(self.ledger.run_id, "FAILED", self.folder)
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
            params=len(spec.params),
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
        return self.learn(report, spec, source, self.reel.strip)

    def learn(self, report: Report, spec: criteria.Spec, source: str, reference: Path) -> Report:
        """Forge until the checks and the judge pass, then install (a new skill or its next version)."""
        if self.installed >= self.authority.caps.new_skills_per_run:
            report.status, report.gap = "FAILED", "cap: no more new skills in this run"
            return self.finish(report)
        clips: dict[int, Path | None] = {}

        def try_code(code: str, number: int) -> Outcome:
            outcome, clips[number] = self.run_skill(code, spec, self.folder / f"attempt-{number}", reference)
            return outcome

        before = self.ledger.spent_usd
        built = forge.build(
            self.model, spec, self.ledger, attempts=self.authority.caps.forge_attempts,
            cap_usd=self.cap("forge"), try_code=try_code,
        )  # fmt: skip
        report.attempts = built.attempts
        report.clip = clips.get(built.attempts)  # the last attempt's clip, kept on a failure too
        if not built.ok:
            report.status, report.gap = "FAILED", built.gap
            return self.finish(report)
        # authority, image and enforcing code must be what they were at the start, right before install
        now = authority.fingerprint(self.home, image_id=workshop.image_id(self.authority.image) or "missing")
        if now.digest != self.start.digest:
            changed = authority.changed(self.start, now)
            self.ledger.record("install_refused", changed=changed)
            report.status = "FAILED"
            report.gap = f"not installed: the authority fingerprint changed mid-run ({', '.join(changed)})"
            return self.finish(report)
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
        steps = [{"skill": skill.slug, "version": skill.version, "params": spec.params}]
        registry.save_design(self.registry, spec.slug, steps, origin=origin)
        self.ledger.record("installed", skill=skill.slug, version=skill.version, design=spec.slug, **cost)
        report.status, report.skill = "BUILT", f"{skill.slug}@v{skill.version}"
        return self.finish(report)

    def run_design(self, name: str, text: str | list[str], *, judge: bool = False) -> Report:
        """Run a saved design on new text: its skills in order, no planner, no forge. With judge=False
        (the default) no model is called at all: only the workshop and the fixed file checks run.
        One text goes to every step; a list gives each step its own."""
        report = Report(self.ledger.run_id, "FAILED", self.folder, skill=f"design:{name}")
        recipe = registry.design(self.registry, name)
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
                    self.model, spec, reference, folder / "strip.png", cap_usd=self.cap("judge")
                )
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


def _inside(path: Path | None, folder: Path) -> str | None:
    """A path inside the run's folder, written relative so the ledger never holds a home path."""
    if path is None:
        return None
    try:
        return str(Path(path).resolve().relative_to(folder.resolve()))
    except ValueError:
        return None


def _spec_dict(spec: criteria.Spec) -> dict[str, Any]:
    return {key: value for key, value in spec.__dict__.items()}


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
