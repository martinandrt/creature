"""The night in numbers, from the run ledgers only. Usage: python scripts/night.py HOMES_DIR OUT.json"""

import glob
import json
import statistics
import sys
from pathlib import Path


def runs(homes: Path):
    for path in sorted(glob.glob(str(homes / "*" / "runs" / "*.jsonl"))):
        home = Path(path).parents[1]
        try:
            authority = json.loads((home / "authority.json").read_text())
        except (OSError, ValueError):
            authority = {}
        events = [json.loads(line) for line in open(path)]
        end = next((e for e in events if e["type"] == "run_end"), None)
        task = next((e for e in events if e["type"] in ("task", "design_task")), {})
        spec = next((e for e in events if e["type"] == "spec"), {})
        calls = [e for e in events if e["type"] == "model_call"]
        yield {
            "home": home.name,
            "run": Path(path).stem,
            "reel": Path(str(task.get("source") or task.get("design") or "")).name,
            "status": end["status"] if end else None,
            "spent_usd": round(sum(float(e.get("cost_usd") or 0) for e in events), 4),  # every paid event
            "task": task.get("type"),
            "opus_builder": (authority.get("models") or {}).get("forge") == "claude-opus-5-5",
            "kind": spec.get("kind"),
            "forge_calls": sum(1 for e in calls if e["step"] == "forge"),
            "installed": [f"{e['skill']}@v{e['version']}" for e in events if e["type"] == "installed"],
            "parts": [
                {"slug": e["slug"], "status": e["status"], "skill": e.get("skill")}
                for e in events
                if e["type"] == "part"
            ],
            "assets_declared": sorted({a for e in events if e["type"] == "installed" for a in e.get("assets") or []}),
        }


def main() -> None:
    homes, out = Path(sys.argv[1]).expanduser(), Path(sys.argv[2])
    rows = [r for r in runs(homes) if r["status"]]
    count = lambda s: sum(1 for r in rows if r["status"] == s)  # noqa: E731
    installed = [i for r in rows for i in r["installed"]]
    opus = [r for r in rows if r["opus_builder"]]
    effect = [r["spent_usd"] for r in opus if r["status"] == "BUILT" and r["kind"] == "effect"]
    learned = [r["spent_usd"] for r in opus if r["kind"] == "montage" and any(p["status"] == "built" for p in r["parts"])]
    reused = [
        r["spent_usd"]
        for r in opus
        if r["kind"] == "montage" and r["parts"] and all(p["status"] == "have" for p in r["parts"]) and not r["forge_calls"]
    ]
    summary = {
        "runs_finished": len(rows),
        "skills_installed": len(installed),
        "skills_distinct": len({i.split("@")[0] for i in installed}),
        "FAILED": count("FAILED"),
        "ASK": count("ASK"),
        "SKIP": count("SKIP"),
        "BUILT": count("BUILT"),
        "HAVE": count("HAVE"),
        "DONE": count("DONE"),
        "effect_learned_usd_median": round(statistics.median(effect), 2) if effect else None,
        "effect_learned_n": len(effect),
        "screens_learned_usd_range": [round(min(learned), 2), round(max(learned), 2)] if learned else None,
        "screens_learned_n": len(learned),
        "all_screens_reused_usd": sorted(round(x, 2) for x in reused),
        "replays_usd": sorted({r["spent_usd"] for r in rows if r["status"] == "DONE" and r["task"] == "design_task"}),
        "skills_declaring_assets": sorted({a for r in rows for a in r["assets_declared"]}),
    }
    out.write_text(json.dumps({"summary": summary, "runs": rows}, indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
