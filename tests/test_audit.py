"""The audit measure and page, on tiny synthetic clips made by ffmpeg lavfi. Host ffmpeg only, no Docker."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from creature import audit

PORTRAIT = "108x192"
LANDSCAPE = "192x108"


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, capture_output=True)


def lavfi(path: Path, source: str, size: str, seconds: float) -> Path:
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"{source}=size={size}:rate=10:duration={seconds}",
        "-pix_fmt",
        "yuv420p",
        str(path),
    )
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("clips")
    reel = lavfi(d / "reel.mp4", "testsrc2", PORTRAIT, 4)
    black = d / "black.mp4"
    ffmpeg(
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:size={PORTRAIT}:rate=10:duration=4",
        "-pix_fmt",
        "yuv420p",
        str(black),
    )
    half = d / "half.mp4"
    ffmpeg("-i", str(reel), "-t", "2", "-pix_fmt", "yuv420p", str(half))
    same = d / "same.mp4"
    shutil.copy(reel, same)
    return {
        "reel": reel,
        "same": same,
        "black": black,
        "half": half,
        "landscape": lavfi(d / "landscape.mp4", "testsrc2", LANDSCAPE, 4),
    }


def test_identical_clip_scores_high(clips):
    m = audit.measure(clips["reel"], clips["same"])
    assert m.frame == 100.0
    assert m.coverage == 1.0
    assert m.orientation_match
    # not 100 only because five palette colours cannot cover every pixel of a colourful test card
    assert m.score > 80


def test_black_clip_against_a_colourful_reel_scores_low(clips):
    m = audit.measure(clips["reel"], clips["black"])
    assert m.frame < 10
    assert m.colour == 0.0
    assert m.score < 40
    assert m.score < audit.measure(clips["reel"], clips["same"]).score - 40


def test_half_length_clip_has_half_coverage(clips):
    m = audit.measure(clips["reel"], clips["half"])
    assert m.coverage == pytest.approx(0.5, abs=0.03)
    assert m.frame > 90  # the first half of the reel, sampled at the same times


def test_longer_clip_caps_coverage_at_one(clips):
    assert audit.measure(clips["half"], clips["reel"]).coverage == 1.0


def test_orientation_mismatch_is_capped(clips):
    m = audit.measure(clips["reel"], clips["landscape"])
    assert not m.orientation_match
    assert (m.reel.orientation, m.clip.orientation) == ("portrait", "landscape")
    assert audit.blend(m.frame, m.colour, m.coverage) > audit.ORIENTATION_CAP  # the cap is what holds it down
    assert m.score == audit.ORIENTATION_CAP


def test_measure_is_deterministic(clips):
    assert audit.measure(clips["reel"], clips["half"]) == audit.measure(clips["reel"], clips["half"])


def test_weights_are_a_blend_and_the_cap_only_bites_on_mismatch():
    assert abs(audit.W_FRAME + audit.W_COLOUR + audit.W_COVERAGE - 1.0) < 1e-9
    assert abs(audit.overall(100, 1, 1, True) - 100.0) < 1e-9
    assert audit.overall(100, 1, 1, False) == audit.ORIENTATION_CAP
    assert audit.overall(0, 0, 0, False) == 0.0  # a low score stays low, the cap is a ceiling


def test_frame_resemblance_endpoints():
    assert audit.frame_resemblance(0) == 100.0
    assert audit.frame_resemblance(audit.FRAME_ZERO_MAD) == 0.0
    assert audit.frame_resemblance(255) == 0.0


def test_unreadable_video_is_an_audit_error(tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(audit.AuditError):
        audit.probe(bad)


# ---------------------------------------------------------------- discovery and the page


def write_run(
    home: Path, run_id: str, reel: Path | None, clip: Path | None, end: dict | None, clip_at: str = ""
):
    """A run as the creature leaves it: <run_id>.jsonl beside a <run_id>/ folder."""
    runs = home / "runs"
    folder = runs / run_id
    folder.mkdir(parents=True)
    events = [
        {"type": "run_start", "run": run_id},
        {"type": "task", "run": run_id, "source": "/somewhere/reels/Cxyz123.mp4", "text": "t"},
    ]
    if end is not None:
        events.append({"type": "run_end", "run": run_id, **end})
    (runs / f"{run_id}.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    if reel:
        shutil.copy(reel, folder / "reel.mp4")
    if clip:
        target = folder / clip_at
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(clip, target)


def tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


@pytest.fixture
def world(tmp_path, clips):
    root = tmp_path / "homes"
    home = root / "alpha"
    r, same, black = clips["reel"], clips["same"], clips["black"]
    write_run(
        home,
        "20260101T000000Z-aaaaaa",
        r,
        same,
        {"status": "HAVE", "clip": "have-x/clip.mp4"},
        "have-x/clip.mp4",
    )
    write_run(
        home,
        "20260101T000100Z-bbbbbb",
        r,
        black,
        {"status": "BUILT", "clip": "attempt-2/clip.mp4"},
        "attempt-2/clip.mp4",
    )
    write_run(
        home,
        "20260101T000200Z-cccccc",
        r,
        black,
        {"status": "FAILED", "clip": "attempt-3/clip.mp4"},
        "attempt-3/clip.mp4",
    )
    # an old ledger names no clip: the newest attempt in the folder is taken
    write_run(home, "20260101T000300Z-dddddd", r, same, {"status": "BUILT"}, "attempt-1/clip.mp4")
    write_run(home, "20260101T000400Z-eeeeee", r, None, None)  # still running: no run_end
    write_run(home, "20260101T000500Z-ffffff", r, None, {"status": "ASK", "clip": None})
    write_run(
        home, "20260101T000600Z-gggggg", None, same, {"status": "DONE", "clip": "joined.mp4"}, "joined.mp4"
    )
    (home / "runs" / "20260101T000700Z-hhhhhh.jsonl").write_text('{"type": "run_start"}\n{"type": "ta')
    return root, home


def test_page_lists_runs_worst_first_and_summarises(world):
    root, home = world
    out = root / "audit.html"
    before = tree_digest(home)
    assert audit.main([str(out), str(home)]) == 0
    assert tree_digest(home) == before  # read-only on the home

    page = out.read_text()
    table = page.split("<tbody>")[1].split("</tbody>")[0]
    at = {name: table.index(name) for name in ("bbbbbb", "cccccc", "aaaaaa", "dddddd")}
    assert max(at["bbbbbb"], at["cccccc"]) < min(at["aaaaaa"], at["dddddd"])  # black clips first
    assert "eeeeee" not in page  # no run_end
    assert "hhhhhh" not in page  # half-written ledger, no run_end
    assert "ffffff" not in table  # no clip: not in the table
    assert "Cxyz123.mp4" in page  # the source name
    assert "1 of 3</b> BUILT/HAVE" in page  # only the black BUILT run is a success that looks like nothing
    assert "gggggg" in page.split("Not measured")[1]  # a clip with no reel is listed, not dropped
    assert "http://" not in page and "https://" not in page  # no external resources

    strips = sorted(p.name for p in (root / "audit").glob("*.jpg"))
    assert len(strips) == 4
    assert all(f'src="audit/{n}"' in page for n in strips)
    assert 'href="alpha/runs/20260101T000100Z-bbbbbb/attempt-2/clip.mp4"' in page


def test_inferred_clip_is_marked(world):
    _, home = world
    found = audit.discover([home])
    inferred = [r for r in found.runs if r.clip_inferred]
    assert [r.run_id for r in inferred] == ["20260101T000300Z-dddddd"]
    assert found.in_progress == 2
    assert sorted(r.status for r in found.no_clip) == ["ASK"]
    assert [r.status for r in found.runs if r.run_id.endswith("gggggg")] == ["DONE"]


def test_status_lines_count_and_median(world):
    root, home = world
    rows, _ = audit.audit([home], root / "audit.html")
    lines = {s.status: s for s in audit.status_lines(rows)}
    assert lines["BUILT"].runs == 2
    assert lines["BUILT"].below == 1
    assert lines["HAVE"].median > 80
    assert lines["FAILED"].median < 40
    assert [r.ref.status for r in audit.low_successes(rows)] == ["BUILT"]
    scores = [r.measure.score for r in rows if r.measure]
    assert scores == sorted(scores)


def test_no_homes_argument_scans_next_to_the_page(world):
    root, _ = world
    out = root / "audit.html"
    assert audit.main([str(out)]) == 0
    assert "alpha" in out.read_text()


def test_nothing_to_audit_is_an_error(tmp_path):
    empty = tmp_path / "empty"  # tmp_path itself holds the autouse `home` fixture's creature home
    empty.mkdir()
    assert audit.main([str(empty / "audit.html"), str(empty)]) == 2


def test_ledger_cannot_point_outside_its_run(tmp_path, clips):
    home = tmp_path / "h"
    outside = os.path.relpath(clips["same"], home / "runs" / "r1")
    write_run(home, "r1", clips["reel"], None, {"status": "BUILT", "clip": outside})
    found = audit.discover([home])
    assert found.runs == []
    assert [r.status for r in found.no_clip] == ["BUILT"]
