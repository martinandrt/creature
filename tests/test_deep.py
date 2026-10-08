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
