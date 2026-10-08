import json

from creature import deep


def test_a_round_changes_only_the_files_it_names_and_edits_apply_once():
    space = deep.Workspace({"frame.py": "A = 1\nB = 2\n", "layout.json": "{}"})
    problems = deep.apply(
        space, {"files": [], "edits": [{"path": "frame.py", "find": "B = 2", "replace": "B = 3"}]}
    )
    assert problems == [] and space.files == {"frame.py": "A = 1\nB = 3\n", "layout.json": "{}"}
    twice = deep.Workspace({"frame.py": "x\nx\n"})
    assert "2 times" in deep.apply(twice, {"edits": [{"path": "frame.py", "find": "x", "replace": "y"}]})[0]
    assert twice.files == {"frame.py": "x\nx\n"}


def test_bad_names_bad_json_and_too_many_files_change_nothing():
    space = deep.Workspace({"frame.py": "pass\n"})
    assert deep.apply(space, {"files": [{"path": "../evil.py", "content": "x"}]})
    assert deep.apply(space, {"files": [{"path": "layout.json", "content": "{nope"}]})
    many = [{"path": f"m{i}.py", "content": "x"} for i in range(deep.MAX_FILES)]
    assert deep.apply(space, {"files": many})
    assert space.files == {"frame.py": "pass\n"}


def test_the_installed_skill_carries_its_files_inside_fixed_wrapper_code():
    files = {"frame.py": "def frame(n, ctx):\n    return None\n", "layout.json": json.dumps({"frames": 30})}
    code = deep.skill_code(files)
    assert code.startswith(deep.WRAPPER.split("__FILES__")[0]) and repr(dict(sorted(files.items()))) in code
    assert "def run(input, work)" in code


def test_the_round_prompt_never_carries_held_out_criteria():
    from creature import criteria
    from creature.criteria import Spec

    out = criteria.clip_format(3)
    spec = Spec("e", "s-x", "try", "", "t", "Hi", {}, out, ("seen one",), ("SECRET held out",), ())
    text = deep.prompt(spec, deep.Workspace(), "feedback", "", 1, 30)
    assert "seen one" in text and "SECRET" not in text


# --- the round loop: best version kept, budget ends it, bad answers and failed clips feed back -----

from creature import criteria  # noqa: E402
from creature.criteria import Spec  # noqa: E402
from creature.ledger import Ledger  # noqa: E402
from creature.llm import Model, ModelError  # noqa: E402
from creature.perceive import Reel  # noqa: E402
from tests.fakes import FakeModel  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n"
V1 = {"path": "frame.py", "content": "V = 1\n"}
LAYOUT = {"path": "layout.json", "content": json.dumps({"frames": 90})}


def _reply(note, files=(), edits=(), done=False):
    return {"note": note, "files": list(files), "edits": list(edits), "done": done}


def _edit(find, replace):
    return {"path": "frame.py", "find": find, "replace": replace}


class Rounds:
    """deep.build with the preview faked (scores scripted) and finish recorded."""

    def __init__(self, home, tmp_path, monkeypatch, scores, finish_ok=(True,), budget=4.0):
        self.fake = FakeModel()
        self.ledger = Ledger.start(home, run_id="deep")
        self.model = Model(
            self.fake, self.ledger, budget_usd=budget, reserve_usd=0.25, model="claude-haiku-5-5"
        )
        strip = tmp_path / "reel-strip.png"
        strip.write_bytes(PNG)
        self.reel = Reel(
            "reel.mp4", "", "", "", tmp_path / "reel.mp4", strip, (0.5, 1.5), 12.0, 1080, 1920, 30.0
        )
        pairs = tmp_path / "pairs.png"
        pairs.write_bytes(PNG)
        scores = iter(scores)
        shown = (("pairs: reel left, yours right", pairs),)
        monkeypatch.setattr(deep, "_preview", lambda *a, **k: deep.Preview(shown, None, next(scores), "d"))
        out = criteria.clip_format(3)
        self.spec = Spec("e", "s-x", "try", "", "t", "Hi", {}, out, ("seen one",), ("SECRET held out",), ())
        self.finished = []
        oks = iter(finish_ok)

        def finish(code, number):
            self.finished.append((number, code))
            ok = next(oks)
            return deep.Outcome(ok, "" if ok else "Judge, not met: seen one (seen: nothing typed)", {})

        self.finish = finish
        self.folder = tmp_path / "rounds"

    def build(self, rounds=5):
        return deep.build(
            self.model, self.spec, self.ledger, reel=self.reel, rounds=rounds, cap_usd=0.25,
            model_name="claude-opus-5-5", limits=None, image="img", folder=self.folder, finish=self.finish,
        )  # fmt: skip

    def prompts(self):
        return [c.prompt for c in self.fake.calls]

    def events(self, kind):
        from creature import ledger

        return [e for e in ledger.read(self.ledger.path) if e["type"] == kind]


def test_a_worse_round_goes_back_to_the_best_files_and_done_renders_the_best(home, tmp_path, monkeypatch):
    r = Rounds(home, tmp_path, monkeypatch, scores=[20.0, 30.0, 10.0])
    r.fake.queue("forge", _reply("v1", files=[V1, LAYOUT]))
    r.fake.queue("forge", _reply("v2", edits=[_edit("V = 1", "V = 2")]))  # scores worse: reverted
    r.fake.queue("forge", _reply("v3", edits=[_edit("V = 1", "V = 3")], done=True))  # applies to v1
    result = r.build()
    assert result.ok and result.rounds == 3 and result.files["frame.py"] == "V = 3\n"
    [(number, code)] = r.finished
    assert number == 3 and code == deep.skill_code(result.files)
    first, second, third = r.prompts()
    assert "No files yet" in first and "your best so far" in second
    assert "worse than round 1" in third and "back to round 1's files" in third
    assert "V = 1\n" in third and "V = 2" not in third  # the forge sees the files it really has
    assert [e["best_round"] for e in r.events("round_score")] == [1, 1, 3]
    labels = [[label for label, _ in c.images] for c in r.fake.calls]
    assert labels[0] == ["reel frames"] and labels[1] == labels[2] == ["pairs: reel left, yours right"]


def test_the_budget_ends_the_rounds_and_nothing_is_finished(home, tmp_path, monkeypatch):
    r = Rounds(home, tmp_path, monkeypatch, scores=[20.0, 15.0], budget=4.0)
    r.fake.queue("forge", _reply("v1", files=[V1, LAYOUT]))
    r.fake.queue("forge", _reply("v2", edits=[_edit("V = 1", "V = 2")]), cost_usd=3.9)  # the run's money
    result = r.build(rounds=30)
    assert not result.ok and result.rounds == 2 and result.gap.startswith("budget:")
    assert r.finished == [] and result.files["frame.py"] == "V = 2\n"
    assert r.events("model_refused") and len(r.events("round")) == 2


def test_a_malformed_answer_costs_a_round_and_the_next_round_is_told(home, tmp_path, monkeypatch):
    r = Rounds(home, tmp_path, monkeypatch, scores=[20.0])
    r.fake.queue("forge", ModelError("cut off"))
    r.fake.queue("forge", _reply("v1", files=[V1, LAYOUT], done=True))
    result = r.build()
    assert result.ok and result.rounds == 2
    assert "could not be used" in r.prompts()[1]
    [bad, good] = r.events("round")
    assert bad["ok"] is False and "cut off" in bad["error"] and good["ok"] is True


def test_a_clip_that_fails_the_judge_feeds_the_next_round_without_the_hidden_criterion(
    home, tmp_path, monkeypatch
):
    r = Rounds(home, tmp_path, monkeypatch, scores=[20.0, 10.0], finish_ok=(False, True))
    r.fake.queue("forge", _reply("v1", files=[V1, LAYOUT], done=True))
    r.fake.queue("forge", _reply("v2", edits=[_edit("V = 1", "V = 2")], done=True))
    result = r.build()
    assert result.ok and result.rounds == 2 and [n for n, _ in r.finished] == [1, 2]
    second = r.prompts()[1]
    assert "Your whole clip was checked and did not pass" in second and "Judge, not met: seen one" in second
    assert "SECRET" not in second
    assert "SECRET" not in deep.SYSTEM
