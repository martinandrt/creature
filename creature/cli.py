"""Command line: creature try · design · compose · list · show · queue · page · overview.

The state root is --home or $CREATURE_HOME. Keys come from the file named by $CREATURE_SECRETS.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from creature import home as home_mod
from creature import registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="creature", description="Try on my input what a reel shows.")
    parser.add_argument("--home", help="state root (default: $CREATURE_HOME)")
    sub = parser.add_subparsers(dest="command", required=True)
    tried = sub.add_parser("try", help="try on my text what this reel shows")
    tried.add_argument("reel", help="Instagram reel URL or a local .mp4")
    tried.add_argument("--text", required=True, help="my input text")
    designed = sub.add_parser("design", help="run a learned design on new text (no model calls by default)")
    designed.add_argument("name")
    designed.add_argument(
        "--text", required=True, action="append", help="my input text; repeat it to give each step its own"
    )
    designed.add_argument("--judge", action="store_true", help="also ask the judge (a paid model call)")
    designed.add_argument("--mark", help="an image (PNG) for a montage's fixed layer, e.g. a logo")
    designed.add_argument("--style", help="a JSON file with my colours and fonts, used over the learned ones")
    composed = sub.add_parser("compose", help="a new design from designs and skills, in order (no model)")
    composed.add_argument("name")
    composed.add_argument("parts", nargs="+", help="design or skill names, in the order they play")
    composed.add_argument("--every", type=int, help="a montage of skill names instead: a cut every N frames")
    composed.add_argument("--seconds", type=float, default=7.0, help="the montage's length")
    sub.add_parser("list", help="learned skills and designs")
    shown = sub.add_parser("show", help="one skill: manifest and tests")
    shown.add_argument("slug")
    sub.add_parser("queue", help="ASK requests waiting for a human")
    sub.add_parser("wishes", help="what I lack, read from my own runs (one model call)")
    tooled = sub.add_parser("tool", help="build a tool I wished for: spec with test cases, forge, install")
    tooled.add_argument("--wish", help="the wish in words (default: the first tool in wishes.json)")
    used = sub.add_parser("use", help="run an installed tool on a clip (no model)")
    used.add_argument("slug")
    used.add_argument("clip")
    sub.add_parser("overview", help="write the page with every run of the night")
    paged = sub.add_parser("page", help="write one run's side-by-side page again, from its ledger")
    paged.add_argument("run", help="run id (the name of its folder in runs/)")
    args = parser.parse_args(argv)
    root = home_mod.resolve(args.home)

    if args.command == "try":
        from creature.loop import Creature  # imports Docker and the model only when needed

        report = Creature(root).try_reel(args.reel, args.text)
        print(f"{report.status}  {report.skill or ''}")
        if report.spec:
            print(f"effect: {report.spec.effect.splitlines()[0][:120]}")
        for note in report.notes:
            print(note)
        if report.gap:
            print(f"gap: {report.gap[:400]}")
        print(f"attempts: {report.attempts}  spent: ${report.spent_usd:.4f}")
        print(f"authority unchanged: {report.fingerprint_same}")
        if report.clip:
            print(f"clip: {report.clip}")
        print(f"run: {report.folder}")
        print(f"page: {report.folder / 'page.html'}")
        return 0 if report.status in ("HAVE", "BUILT") else 1
    if args.command == "design":
        from creature.loop import Creature

        style = json.loads(Path(args.style).read_text(encoding="utf-8")) if args.style else None
        mark = Path(args.mark) if args.mark else None
        report = Creature(root).run_design(args.name, args.text, judge=args.judge, mark=mark, style=style)
        print(f"{report.status}  {report.skill}")
        if report.gap:
            print(f"gap: {report.gap[:400]}")
        print(f"spent: ${report.spent_usd:.4f}  authority unchanged: {report.fingerprint_same}")
        if report.clip:
            print(f"clip: {report.clip}")
        print(f"run: {report.folder}")
        return 0 if report.status == "DONE" else 1
    if args.command == "compose" and args.every:
        from creature import montage

        registry.taken(root / "registry", args.name)
        sources = {}
        for number, part in enumerate(args.parts):
            skill = registry.get(root / "registry", part)
            sources[chr(97 + number)] = {"skill": skill.slug, "version": skill.version, "params": {}}
        t = montage.timeline(
            args.name, sources, list(sources), every=args.every, frames=montage.frames_for(args.seconds),
            layers=[{"id": "mark", "x": 0.5, "y": 0.5, "height": 0.06}],
        )  # fmt: skip
        registry.save_timeline(root / "registry", args.name, t, origin={"composed_by": "compose"})
        print(f"montage {args.name}: {len(t['scenes'])} cuts over {t['frames']} frames, mark on top")
        return 0
    if args.command == "compose":
        made = registry.compose(root / "registry", args.name, args.parts, origin={"composed_by": "compose"})
        for number, step in enumerate(made["steps"], start=1):
            print(f"{number}. {step['skill']} v{step['version']}")
        print(f"design: {args.name}  (run it: creature design {args.name} --text ...)")
        return 0
    if args.command == "list":
        for skill in registry.skills(root / "registry"):
            learned = skill.capability.get("cost", {}).get("learn_usd", 0)
            effect = skill.capability["effect"][:70]
            print(f"{skill.slug} v{skill.version}  learned for ${learned:.4f}  {effect}")
        for name in registry.designs(root / "registry"):
            print(f"design: {name}")
        for problem in registry.broken(root / "registry"):
            print(f"SEAL BROKEN, not used: {problem}")
        return 0
    if args.command == "show":
        skill = registry.get(root / "registry", args.slug)
        print(
            json.dumps({"capability": skill.capability, "tests": skill.tests}, indent=1, ensure_ascii=False)
        )
        return 0
    if args.command == "page":
        from creature import page

        print(page.rebuild(root, args.run))
        return 0
    if args.command == "overview":
        from creature import page

        print(page.overview(root))
        return 0
    if args.command == "wishes":
        from creature.loop import Creature

        report = Creature(root).wishes()
        for note in report.notes:
            print(note)
        print(f"{report.status}  spent: ${report.spent_usd:.4f}  page: {report.folder / 'page.html'}")
        return 0 if report.status == "DONE" else 1
    if args.command == "tool":
        from creature.loop import Creature

        report = Creature(root).build_tool(args.wish)
        print(f"{report.status}  {report.skill or ''}")
        if report.gap:
            print(f"gap: {report.gap[:400]}")
        print(f"attempts: {report.attempts}  spent: ${report.spent_usd:.4f}")
        print(f"page: {report.folder / 'page.html'}")
        return 0 if report.status == "BUILT" else 1
    if args.command == "use":
        from creature.loop import Creature, Report

        creature = Creature(root)
        result = creature.use_tool(args.slug, Path(args.clip))
        status = "DONE" if result["ok"] else "FAILED"
        creature.finish(Report(creature.ledger.run_id, status, creature.folder, skill=f"use:{args.slug}"))
        print(json.dumps(result, indent=1, ensure_ascii=False))
        return 0 if result["ok"] else 1
    if args.command == "queue":
        for path in sorted((root / "queue").glob("*.json")):
            request = json.loads(path.read_text(encoding="utf-8"))
            print(f"{request['run']}  {request['status']}  {request['effect'][:60]}")
            print(f"    needs: {request['needs'][:200]}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
