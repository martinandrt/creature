import dataclasses
import io
import json

import pytest

from creature import authority, keys, ledger, perceive, workshop
from creature.ledger import Ledger
from tests.conftest import REPO

CLIP_CODE = (
    "import subprocess\n"
    "def run(input, work):\n"
    "    source = 'testsrc2=s=360x640:r=30:d=2'\n"
    "    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', source, '-pix_fmt', 'yuv420p',\n"
    "                    f'{work}/out/clip.mp4'], check=True)\n"
    "    return 'ok'"
)


@pytest.fixture
def secrets_file(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text("# keys\nexport APIFY_TOKEN='tok-123'\nOTHER=x\nEMPTY=\n")
    monkeypatch.setenv(keys.ENV, str(path))
    return path


def test_keys_reads_export_and_quotes(secrets_file):
    assert keys.get("APIFY_TOKEN") == "tok-123" and keys.get("OTHER") == "x"


def test_missing_key_never_shows_the_file(secrets_file):
    with pytest.raises(RuntimeError) as caught:
        keys.get("EMPTY")
    assert "tok-123" not in str(caught.value)


def test_keys_need_the_env_var(monkeypatch):
    monkeypatch.delenv(keys.ENV, raising=False)
    with pytest.raises(RuntimeError, match="CREATURE_SECRETS"):
        keys.get("APIFY_TOKEN")


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _apify(monkeypatch, items):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"], seen["auth"] = request.full_url, request.get_header("Authorization")
        seen["body"] = json.loads(request.data)
        return _Response(json.dumps(items).encode())

    monkeypatch.setattr(perceive.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_fetch_reel_asks_for_transcript_and_caps_cost(monkeypatch, secrets_file):
    item = {"type": "Video", "videoUrl": "https://x.cdninstagram.com/v.mp4", "caption": "Typewriter"}
    seen = _apify(monkeypatch, [item])
    assert perceive.fetch_reel("https://www.instagram.com/p/X/") == item
    assert seen["body"]["includeTranscript"] is True and seen["body"]["username"] == [
        "https://www.instagram.com/p/X/"
    ]
    assert "maxTotalChargeUsd" in seen["url"] and "timeout" in seen["url"]
    assert seen["auth"] == "Bearer tok-123" and "tok-123" not in seen["url"]


@pytest.mark.parametrize(
    "items", [[], {"error": "x"}, [{"type": "Image", "videoUrl": "u"}], [{"type": "Video"}]]
)
def test_fetch_reel_refuses_what_is_not_a_video_reel(monkeypatch, secrets_file, items):
    _apify(monkeypatch, items)
    with pytest.raises(perceive.PerceiveError):
        perceive.fetch_reel("https://www.instagram.com/p/X/")


@pytest.mark.parametrize(
    "url",
    [
        "http://x.cdninstagram.com/v.mp4",
        "https://evil.example/v.mp4",
        "https://cdninstagram.com.evil.io/v.mp4",
        "",
    ],
)
def test_download_only_from_instagram_cdn(tmp_path, url):
    with pytest.raises(perceive.PerceiveError, match="not allowed"):
        perceive.download(url, tmp_path / "v.mp4")


def test_transcript_from_segments_or_text():
    assert perceive._transcript({"transcript": [{"text": "type"}, {"text": "on text"}]}) == "type on text"
    assert perceive._transcript({"transcript": " plain "}) == "plain"
    assert perceive._transcript({}) == ""


@pytest.mark.docker
def test_local_reel_becomes_a_strip(home, tmp_path):
    limits = dataclasses.replace(authority.load(REPO).workshop, timeout_s=60)
    image = workshop.ensure_image(REPO / "workshop")
    clip = workshop.run(CLIP_CODE, {}, limits=limits, image=image)
    source = tmp_path / "reel.mp4"
    source.write_bytes(clip.outputs["clip.mp4"])
    source.with_suffix(".json").write_text(
        json.dumps({"caption": "Type on text", "transcript": "set the caret"})
    )
    log = Ledger.start(home, run_id="perceive")
    reel = perceive.perceive(str(source), tmp_path / "attempt", log, limits=limits, image=image)
    assert reel.caption == "Type on text" and reel.transcript == "set the caret"
    assert (reel.width, reel.height, reel.fps) == (360, 640, 30.0) and 1.9 < reel.duration_s < 2.1
    assert reel.strip.read_bytes().startswith(b"\x89PNG") and len(reel.times) == perceive.FRAMES
    assert [e["type"] for e in ledger.read(log.path)] == ["perceive_local", "perceived"]
