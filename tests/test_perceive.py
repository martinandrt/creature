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


def test_failed_apify_call_still_records_its_cost(monkeypatch, secrets_file, home):
    # Apify charges the actor start even when nothing usable comes back: paid means recorded
    _apify(monkeypatch, [])
    log = Ledger.start(home, run_id="apify-fail")
    limits = authority.load(REPO).workshop
    with pytest.raises(perceive.PerceiveError):
        perceive.perceive("https://www.instagram.com/p/X/", home / "attempt", log, limits=limits, image="img")
    [event] = [e for e in ledger.read(log.path) if e["type"] == "perceive_apify"]
    assert event["cost_usd"] == perceive.APIFY_COST_USD
    assert log.spent_usd > 0


def test_redirect_off_the_cdn_is_refused():
    # the host allowlist must survive a 302: re-validate the Location, scheme included
    import urllib.request

    handler = perceive.RedirectWithinCdn()
    request = urllib.request.Request("https://a.cdninstagram.com/v.mp4")
    for target in ("https://evil.example/v.mp4", "http://a.cdninstagram.com/v.mp4"):
        with pytest.raises(perceive.PerceiveError, match="not allowed"):
            handler.redirect_request(request, None, 302, "Found", {}, target)
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://b.fbcdn.net/v.mp4") is not None


def test_download_stops_at_the_size_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(perceive, "MAX_VIDEO_BYTES", 1000)
    monkeypatch.setattr(perceive, "_open", lambda url: _Response(b"\0" * 2000))
    with pytest.raises(perceive.PerceiveError, match="too large"):
        perceive.download("https://a.cdninstagram.com/v.mp4", tmp_path / "v.mp4")


def test_download_writes_the_file(monkeypatch, tmp_path):
    monkeypatch.setattr(perceive, "_open", lambda url: _Response(b"video"))
    perceive.download("https://a.cdninstagram.com/v.mp4", tmp_path / "v.mp4")
    assert (tmp_path / "v.mp4").read_bytes() == b"video"


def test_perceive_via_apify_offline(monkeypatch, secrets_file, home, tmp_path):
    # the whole Apify path with the network and the workshop faked: events, cost, and no token anywhere
    item = {
        "type": "Video",
        "videoUrl": "https://x.cdninstagram.com/v.mp4",
        "caption": " Typewriter ",
        "transcript": [{"text": "hi"}],
        "ownerUsername": "anet",
    }
    _apify(monkeypatch, [item])
    monkeypatch.setattr(perceive, "download", lambda url, target: target.write_bytes(b"video"))
    strip = workshop.WorkshopResult(
        ok=True,
        value={"duration_s": 2.0, "width": 360, "height": 640, "fps": 30.0, "times": [0.5, 1.5]},
        outputs={"strip.png": b"\x89PNG\r\n"},
        error=None,
        killed=None,
        duration_s=0.3,
        log="",
    )
    monkeypatch.setattr(perceive.workshop, "run", lambda *args, **kwargs: strip)
    log = Ledger.start(home, run_id="apify-ok")
    limits = authority.load(REPO).workshop
    reel = perceive.perceive(
        "https://www.instagram.com/p/X/", tmp_path / "attempt", log, limits=limits, image="img"
    )
    assert (reel.caption, reel.transcript, reel.author) == ("Typewriter", "hi", "anet")
    assert reel.strip.read_bytes().startswith(b"\x89PNG") and reel.times == (0.5, 1.5)
    assert [e["type"] for e in ledger.read(log.path)] == ["perceive_apify", "perceived"]
    assert log.spent_usd == perceive.APIFY_COST_USD
    assert "tok-123" not in log.path.read_text()


def test_video_cap_fits_the_work_dir():
    # a reel at the download cap plus 12 frames and a strip must fit the workshop's /work tmpfs
    work_bytes = authority.load(REPO).workshop.work_mb * 1_000_000
    needed = perceive.MAX_VIDEO_BYTES * 1.5
    assert needed < work_bytes


def test_recorded_apify_reel_replays_offline(monkeypatch, secrets_file):
    # recorded from the real actor (reel 1, transcript add-on); the CDN URL is replaced
    items = json.loads((REPO / "tests" / "fixtures" / "apify_reel01.json").read_text())
    _apify(monkeypatch, items)
    item = perceive.fetch_reel(items[0]["url"])
    assert "typewriter" in perceive._transcript(item).lower() and item["type"] == "Video"
    assert "TYPEWRITER" in perceive._text(item["caption"])


def test_whisper_missing_or_hearing_music_gives_no_transcript(monkeypatch, tmp_path):
    wav = tmp_path / "audio.wav"
    wav.write_bytes(b"RIFF")
    monkeypatch.setattr(perceive.shutil, "which", lambda name: None)
    assert perceive.transcribe(wav) == ""
    model = tmp_path / "model.bin"
    model.write_bytes(b"x")
    monkeypatch.setenv("CREATURE_WHISPER_MODEL", str(model))
    monkeypatch.setattr(perceive.shutil, "which", lambda name: "/bin/whisper-cli")

    def heard(text):
        return lambda *a, **k: perceive.subprocess.CompletedProcess(a, 0, stdout=text, stderr="")

    monkeypatch.setattr(perceive.subprocess, "run", heard(" you\n"))
    assert perceive.transcribe(wav) == ""  # a word heard in music is not speech
    monkeypatch.setattr(perceive.subprocess, "run", heard(" Apply the typewriter\n transition here.\n"))
    assert perceive.transcribe(wav) == "Apply the typewriter transition here."


@pytest.mark.docker
def test_palette_measures_the_picture_not_the_letterbox(home, tmp_path):
    # a square reel is padded into the 9:16 tile with black bars (210 of 480 rows, 44 % of the tile).
    # The palette describes the reel's colours: a pure red reel must come out red, with no black entry
    limits = dataclasses.replace(authority.load(REPO).workshop, timeout_s=60)
    image = workshop.ensure_image(REPO / "workshop")
    code = CLIP_CODE.replace("testsrc2=s=360x640:r=30:d=2", "color=c=red:s=1080x1080:r=30:d=2")
    clip = workshop.run(code, {}, limits=limits, image=image)
    source = tmp_path / "square.mp4"
    source.write_bytes(clip.outputs["clip.mp4"])
    log = Ledger.start(home, run_id="palette")
    reel = perceive.perceive(str(source), tmp_path / "attempt", log, limits=limits, image=image)
    assert (reel.width, reel.height) == (1080, 1080) and reel.palette
    red = reel.palette[0]["hex"]
    assert int(red[1:3], 16) > 240 and int(red[3:5], 16) < 16 and int(red[5:7], 16) < 16, reel.palette
    assert reel.palette[0]["share"] > 0.9, reel.palette  # one colour, not 56 % red and 44 % padding
    assert not any(c["hex"] == "#000000" for c in reel.palette), reel.palette
