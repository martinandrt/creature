import json

from creature import wishes
from creature.ledger import Ledger


def _run(home, run_id, status, gap="", source="reels/DcEZYsvtgQX.mp4", **extra):
    log = Ledger.start(home, run_id=run_id)
    log.record("task", source=source, text="Hi there.")
    log.record("spec", effect="Brand montage", verdict="try")
    for kind, data in extra.items():
        log.record(kind, **data)
    log.record("run_end", status=status, gap=gap)


class Asked:
    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def ask(self, step, system, prompt, schema, *, cap_usd):
        self.prompts.append(prompt)
        return self.reply


def test_evidence_is_every_run_without_a_skill_and_every_skipped_part(home):
    _run(home, "a", "BUILT", source="reels/AAA.mp4")
    _run(home, "b", "FAILED", "the judge said no", source="reels/BBB.mp4")
    _run(
        home,
        "c",
        "BUILT",
        source="reels/CCC.mp4",
        part={"slug": "dot-ring", "status": "skipped", "gap": "stuck"},
    )
    found = {r["reel"]: r for r in wishes.evidence(home)}
    assert sorted(found) == ["BBB", "CCC"]
    assert "the judge said no" in found["BBB"]["why"] and "dot-ring skipped" in found["CCC"]["why"]


def test_wishes_keep_only_reels_from_the_evidence_and_sort_by_reels_unlocked(home):
    _run(home, "b", "FAILED", "no 3D", source="reels/BBB.mp4")
    _run(home, "c", "ASK", "photos", source="reels/CCC.mp4")
    model = Asked(
        {
            "wishes": [
                {"capability": "photos as input", "kind": "input", "reels": ["CCC"], "why": "x"},
                {
                    "capability": "a 3D renderer",
                    "kind": "authority",
                    "reels": ["BBB", "CCC", "ZZZ"],
                    "why": "y",
                },
                {"capability": "made up", "kind": "tool", "reels": ["ZZZ"], "why": "z"},
            ]
        }
    )
    found = wishes.wish(model, home, cap_usd=0.03)
    assert [w["capability"] for w in found["wishes"]] == ["a 3D renderer", "photos as input"]
    assert found["wishes"][0]["reels"] == ["BBB", "CCC"] and found["runs"] == 2
    assert "<runs>" in model.prompts[0]
    assert json.loads((home / wishes.WISHES).read_text()) == found
