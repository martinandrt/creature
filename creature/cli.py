"""Command line: creature try · list · show · design · queue.

The state root is --home or $CREATURE_HOME. Keys come from the file named by $CREATURE_SECRETS.
"""

from __future__ import annotations

import argparse
import json
import sys

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
    designed.add_argument("--text", required=True, help="my input text")
    designed.add_argument("--judge", action="store_true", help="also ask the judge (a paid model call)")
    sub.add_parser("list", help="learned skills and designs")
    shown = sub.add_parser("show", help="one skill: manifest and tests")
    shown.add_argument("slug")
    sub.add_parser("queue", help="ASK requests waiting for a human")
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
        return 0 if report.status in ("HAVE", "BUILT") else 1
    if args.command == "design":
        from creature.loop import Creature

        report = Creature(root).run_design(args.name, args.text, judge=args.judge)
        print(f"{report.status}  {report.skill}")
        if report.gap:
            print(f"gap: {report.gap[:400]}")
        print(f"spent: ${report.spent_usd:.4f}  authority unchanged: {report.fingerprint_same}")
        if report.clip:
            print(f"clip: {report.clip}")
        print(f"run: {report.folder}")
        return 0 if report.status == "DONE" else 1
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
    if args.command == "queue":
        for path in sorted((root / "queue").glob("*.json")):
            request = json.loads(path.read_text(encoding="utf-8"))
            print(f"{request['run']}  {request['status']}  {request['effect'][:60]}")
            print(f"    needs: {request['needs'][:200]}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
