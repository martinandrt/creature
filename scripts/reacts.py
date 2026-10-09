"""Does each installed skill react to its input? Render it with two different texts and compare.

Read only: every effect skill in every home renders twice in the workshop (the style card's two probe
sentences) and fixed code measures how much of the frame differs between the renders. 0 % means the
text is drawn in no pixel the user's input reaches: the skill keeps its text fixed. Nothing is written
into a home; renders go to a scratch folder. A skill copied into several homes (same code, same values)
is rendered once.

    uv run python scripts/reacts.py ~/Desktop/creature-homes docs/reacts.json [workers]
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from creature import authority, card, registry, workshop


def main(homes: Path, out: Path, workers: int = 3) -> None:
    found = {}  # (code, values) digest -> (skill, limits, image, where)
    where: dict[str, list[str]] = {}
    for home in sorted(p for p in homes.iterdir() if (p / "registry" / registry.INDEX).is_file()):
        try:
            rules = authority.load(home)
        except Exception:
            continue
        image = workshop.image_id(rules.image)
        if not image:
            continue
        for skill in registry.skills(home / "registry"):
            if skill.capability.get("kind") == "tool":
                continue
            values = json.dumps(skill.capability.get("params", {}), sort_keys=True)
            key = hashlib.sha256((skill.code + values).encode()).hexdigest()[:16]
            where.setdefault(key, []).append(f"{home.name}/{skill.slug}@v{skill.version}")
            if key not in found and not (skill.capability.get("assets") and not rules.workshop.assets):
                found[key] = (skill, rules.workshop, image)
    scratch = Path(tempfile.mkdtemp(prefix="reacts-"))

    def measure(item):
        key, (skill, limits, image) = item
        return key, card.measure(skill, scratch / key, limits=limits, image=image)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        measured = dict(pool.map(measure, sorted(found.items())))
    rows = []
    for key, result in sorted(measured.items(), key=lambda kv: where[kv[0]][0]):
        share = result.get("text_share")
        rows.append({"skills": where[key], "text_share": share, "error": result.get("error")})
    ok = [r for r in rows if r["error"] is None]
    summary = {
        "skills_installed": sum(len(v) for v in where.values()),
        "distinct": len(rows),
        "measured": len(ok),
        "react": sum(1 for r in ok if r["text_share"] > 0),
        "fixed_text": sum(1 for r in ok if r["text_share"] == 0),
        "failed_to_render": len(rows) - len(ok),
        "probe_texts": list(card.TEXTS),
    }
    out.write_text(
        json.dumps({"summary": summary, "skills": rows}, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main(Path(sys.argv[1]).expanduser(), Path(sys.argv[2]), int(sys.argv[3]) if len(sys.argv) > 3 else 3)
