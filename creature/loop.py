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

from creature import authority, criteria, forge, perceive, registry, verdict, workshop
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
        )  # fmt: skip
        return report

    def run_skill(self, code: str, spec: criteria.Spec, attempt: Path) -> tuple[Outcome, Path | None]:
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
        judged = verdict.judge(
            self.model, spec, self.reel.strip, attempt / "strip.png", cap_usd=self.cap("judge")
        )
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
        spec = criteria.write(
            self.model, self.reel, text, cap_usd=self.cap("criteria"), seed=self.ledger.run_id,
            model_name=self.model_for("criteria"),
        )  # fmt: skip
        report.spec = spec
        self.ledger.record(
            "spec", verdict=spec.verdict, slug=spec.slug, effect=spec.effect[:300], reason=spec.reason[:300],
            visible=len(spec.criteria), held_out=len(spec.held_out), duration_s=spec.output["duration_s"],
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

        # an installed skill for this effect? Try it first: using is cheaper than learning
        for skill in registry.search(self.registry, f"{spec.slug} {spec.effect}")[:1]:
            outcome, clip = self.run_skill(skill.code, spec, self.folder / f"have-{skill.slug}")
            self.ledger.record(
                "have_try", skill=skill.slug, version=skill.version, ok=outcome.ok, detail=outcome.detail
            )
            if outcome.ok:
                report.status, report.skill, report.clip = "HAVE", f"{skill.slug}@v{skill.version}", clip
                return self.finish(report)
            # it falls short on this input: learn the next version of the same skill (evolve)
            spec = dataclasses.replace(spec, slug=skill.slug)
            report.spec = spec
            self.ledger.record(
                "evolve", skill=skill.slug, from_version=skill.version, why=outcome.feedback[:400]
            )
            report.notes.append(
                f"{skill.slug} v{skill.version} fell short on this input: learning v{skill.version + 1}"
            )

        if self.installed >= self.authority.caps.new_skills_per_run:
            report.status, report.gap = "FAILED", "cap: no more new skills in this run"
            return self.finish(report)

        clips: dict[int, Path | None] = {}

        def try_code(code: str, number: int) -> Outcome:
            outcome, clips[number] = self.run_skill(code, spec, self.folder / f"attempt-{number}")
            return outcome

        before = self.ledger.spent_usd
        built = forge.build(
            self.model, spec, self.ledger, attempts=self.authority.caps.forge_attempts,
            cap_usd=self.cap("forge"), try_code=try_code,
        )  # fmt: skip
        report.attempts = built.attempts
        if not built.ok:
            report.status, report.gap = "FAILED", built.gap
            return self.finish(report)
        origin = {"reel": source, "run": self.ledger.run_id, "author": self.reel.author}
        cost = {"learn_usd": round(self.ledger.spent_usd - before, 6), "forge_attempts": built.attempts}
        skill = registry.install(self.registry, spec, built.code, origin=origin, cost=cost)
        self.installed += 1
        steps = [{"skill": skill.slug, "version": skill.version, "params": spec.params}]
        registry.save_design(self.registry, spec.slug, steps, origin=origin)
        self.ledger.record("installed", skill=skill.slug, version=skill.version, design=spec.slug, **cost)
        report.status, report.skill, report.clip = (
            "BUILT",
            f"{skill.slug}@v{skill.version}",
            clips.get(built.attempts),
        )
        return self.finish(report)


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
