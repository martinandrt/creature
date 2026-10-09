from types import SimpleNamespace

from creature import page


def test_a_make_runs_page_says_what_it_was_asked_and_what_it_cost(tmp_path):
    folder = tmp_path / "runs" / "20261009T030617Z-880ab4"
    (folder / "make-2").mkdir(parents=True)
    (folder / "make-2" / "joined.mp4").write_bytes(b"\x00")
    ts = "2026-10-09T03:06:17+00:00"
    lines = ["Night shift.", "Seventeen reels.", "One creature.", "Zero new authority."]
    skills = ["glow-orb-title", "bold-title-card", "glow-orb-title", "bold-title-card"]
    call = {"type": "model_call", "ts": ts, "model": "m", "ok": True, "input_tokens": 1, "output_tokens": 1}
    events = [
        {"type": "run_start", "ts": ts, "image": "sha256:x"},
        {"type": "make_task", "ts": ts, "lines": lines, "style": False},
        {**call, "step": "compose", "cost_usd": 0.0013},
        {"type": "make_choice", "ts": ts, "attempt": 1, "reason": "alternate dark and light",
         "shots": [{"line": i + 1, "skill": s, "why": ""} for i, s in enumerate(skills)]},
        {"type": "make_judged", "ts": ts, "ok": True, "lines": [], "feedback": "fine"},
        {**call, "step": "judge", "cost_usd": 0.0041},
        {"type": "run_end", "ts": ts, "status": "DONE", "skill": "make", "attempts": 0,
         "clip": "make-2/joined.mp4", "spent_usd": 0.0054, "fingerprint_same": True, "gap": "",
         "changed": []},
    ]  # fmt: skip
    report = SimpleNamespace(
        run_id=folder.name, status="DONE", spec=None, skill="make", attempts=0, gap="", spent_usd=0.0054,
        fingerprint_same=True, clip=folder / "make-2" / "joined.mp4", folder=folder, notes=[],
    )  # fmt: skip
    text = page.render(folder, report, events).read_text(encoding="utf-8")
    for line in lines:
        assert line in text  # the script it was asked for, where a learning run shows its input text
    for skill in set(skills):
        assert skill in text  # and which learned screen each line got
    assert "(none)" not in text and "Input text: “”" not in text
    assert "no model call" not in text and "2 model calls" in text  # it composed and judged with the model
    assert "Source: design" not in text
    assert "creature design make " not in text  # no design named "make" exists; the saved one has a free name


def test_a_make_runs_page_can_be_rebuilt_from_its_ledger(tmp_path):
    import json

    run = "20261009T030617Z-880ab4"
    (tmp_path / "runs" / run / "make-1").mkdir(parents=True)
    (tmp_path / "runs" / run / "make-1" / "joined.mp4").write_bytes(b"\x00")
    ts = "2026-10-09T03:06:17+00:00"
    events = [
        {"type": "run_start", "ts": ts, "image": "sha256:x"},
        {"type": "make_task", "ts": ts, "lines": ["One.", "Two."], "style": False},
        {"type": "model_call", "ts": ts, "step": "compose", "model": "m", "ok": True, "cost_usd": 0.001},
        {"type": "make_choice", "ts": ts, "attempt": 1,
         "shots": [{"line": 1, "skill": "a"}, {"line": 2, "skill": "b"}]},
        {"type": "make_judged", "ts": ts, "ok": True},
        {"type": "make_saved", "ts": ts, "design": "make-7"},
        {"type": "run_end", "ts": ts, "status": "DONE", "skill": "make", "clip": "make-1/joined.mp4",
         "spent_usd": 0.001, "fingerprint_same": True},
    ]  # fmt: skip
    (tmp_path / "runs" / f"{run}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    text = page.rebuild(tmp_path, run).read_text(encoding="utf-8")
    assert "One." in text and "Two." in text
    assert "creature design make-7 --text=One. --text=Two." in text  # the design it saved, by the name it got
