"""Perception: a reel becomes a local video, its words and a strip of frames.

The spine fetches the reel's caption, transcript and video URL from Apify and downloads the video.
Everything that parses the untrusted video (ffprobe, frame extraction) runs in the workshop, never on
the host. A local .mp4 works too (with an optional sidecar <name>.json holding caption and transcript),
for tests and for reels saved earlier.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from creature import keys, workshop
from creature.ledger import Ledger

ACTOR = "apify~instagram-reel-scraper"
# FREE tier list prices: actor start + one reel + transcript add-on
APIFY_COST_USD = 0.001 + 0.0026 + 0.048
APIFY_MAX_CHARGE_USD = 0.10
APIFY_TIMEOUT_S = 180
MAX_VIDEO_BYTES = 100_000_000
MAX_APIFY_BYTES = 5_000_000
FRAMES = 24  # one sheet, 6 x 4: half picked by cuts (one per shot), the rest spread evenly
CUT_SCORE = 0.3  # ffmpeg scene score above which a frame starts a new shot
PALETTE = 6
WHISPER_MODEL = "~/whisper-models/ggml-large-v3-turbo-q5_0.bin"  # or $CREATURE_WHISPER_MODEL
WHISPER_TIMEOUT_S = 120
MIN_WORDS = 4  # Whisper "hears" a word or two in music ("you"): that is not speech
AUDIO_MAX_S = 120

# Fixed code (ours, not generated) that runs in the workshop on the downloaded video.
STRIP_CODE = (
    workshop.SHEET_CODE
    + r"""
import json, re
import numpy as np

def cuts_of(video, score):
    found = subprocess.run(["ffmpeg", "-v", "info", "-i", video, "-vf",
                            f"select='gt(scene,{score})',showinfo", "-an", "-f", "null", "-"],
                           capture_output=True, text=True)
    return sorted({round(float(t), 3) for t in re.findall(r"pts_time:([0-9.]+)", found.stderr)})

def pick(duration, cuts, count):
    # half the frames: first to (nearly) last, evenly, so start and end are both seen; the other half:
    # the middle of shots none of those landed in, spread over the reel, so short shots are seen too
    last = max(0.0, duration - 0.2)
    even = [round(last * i / max(1, count // 2 - 1), 3) for i in range(count // 2)]
    bounds = [0.0, *[c for c in cuts if 0.0 < c < duration], duration]
    shots = [(a, b) for a, b in zip(bounds, bounds[1:]) if b - a > 0.05]
    unseen = [round((a + b) / 2, 3) for a, b in shots if not any(a <= t < b for t in even)]
    want = count - len(even)
    if len(unseen) > want:
        unseen = [unseen[round(i * (len(unseen) - 1) / max(1, want - 1))] for i in range(want)]
    times = sorted(set(even) | set(unseen))
    times = [t for i, t in enumerate(times) if i == 0 or t - times[i - 1] > 0.02]
    while len(times) < count and len(times) > 1:  # fill the widest gap
        i = max(range(len(times) - 1), key=lambda k: times[k + 1] - times[k])
        times.insert(i + 1, round((times[i] + times[i + 1]) / 2, 3))
    return times[:count]

def palette(paths, size):
    pixels = np.concatenate([np.asarray(Image.open(p).convert("RGB")).reshape(-1, 3)[::37] for p in paths])
    pixels = pixels.astype(np.float32)
    order = np.argsort(pixels.sum(axis=1))
    centers = pixels[order[np.linspace(0, len(order) - 1, size).astype(int)]]
    for _ in range(12):
        nearest = np.argmin(((pixels[:, None, :] - centers[None]) ** 2).sum(axis=2), axis=1)
        centers = np.array([pixels[nearest == k].mean(axis=0) if (nearest == k).any() else centers[k]
                            for k in range(size)])
    shares = np.bincount(nearest, minlength=size) / len(pixels)
    found = []
    for k in np.argsort(-shares):
        color = centers[k]
        if shares[k] < 0.02 or any(np.abs(color - np.array(c)).sum() < 40 for c, _ in found):
            continue
        found.append((color.round().astype(int).tolist(), float(shares[k])))
    return [{"hex": "#%02x%02x%02x" % tuple(c), "share": round(sh, 3)} for c, sh in found]

def run(input, work):
    video = f"{work}/in/reel.mp4"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height,avg_frame_rate,duration:format=duration", "-of", "json", video],
        capture_output=True, text=True, check=True)
    info = json.loads(probe.stdout)
    streams = info.get("streams", [])
    stream = [s for s in streams if s.get("codec_type") == "video"][0]
    duration = float(info["format"]["duration"])
    if float(stream.get("duration") or 0) > 0:  # the audio may run longer than the picture
        duration = min(duration, float(stream["duration"]))
    num, den = stream["avg_frame_rate"].split("/")
    cuts = cuts_of(video, input["cut_score"])
    times = pick(duration, cuts, input["frames"])
    tiles = []
    for i, t in enumerate(times):
        path = f"{work}/frame_{i:03d}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", video, "-frames:v", "1", "-vf", FIT,
                        path], check=True)
        tiles.append(path)
    sheets(tiles, times, f"{work}/out/strip")
    audio = any(s.get("codec_type") == "audio" for s in streams)
    if audio:
        subprocess.run(["ffmpeg", "-v", "error", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
                        "-t", str(input["audio_max_s"]), f"{work}/out/audio.wav"], check=True)
    return {"duration_s": duration, "width": stream["width"], "height": stream["height"],
            "fps": round(float(num) / float(den), 3) if float(den) else 0.0, "times": times,
            "cuts": cuts, "palette": palette(tiles, input["palette"]), "audio": audio}
"""
)


@dataclass(frozen=True)
class Reel:
    source: str  # reel URL or local file
    caption: str
    transcript: str
    author: str
    video: Path
    strip: Path  # 24 frames in time order, 6 per row, each with its time
    times: tuple[float, ...]  # seconds at which the strip's frames were taken
    duration_s: float
    width: int
    height: int
    fps: float
    cuts: tuple[float, ...] = ()  # seconds at which a new shot starts
    palette: tuple[dict[str, Any], ...] = ()  # measured colours, {hex, share}, most used first


class PerceiveError(Exception):
    pass


def perceive(source: str, folder: Path, ledger: Ledger, *, limits: workshop.Limits, image: str) -> Reel:
    """Turn a reel URL (or a local .mp4) into a Reel inside `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    local = Path(source).expanduser()
    if local.suffix.lower() == ".mp4" and local.is_file():
        video = folder / "reel.mp4"
        video.write_bytes(local.read_bytes())
        sidecar = local.with_suffix(".json")
        meta = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}
        caption, transcript, author = _text(meta.get("caption")), _text(meta.get("transcript")), ""
        ledger.record("perceive_local", source=str(local), sidecar=sidecar.is_file())
    else:
        try:
            item = fetch_reel(source)
        except PerceiveError as error:
            # Apify bills the start even when nothing usable comes back: paid means recorded
            ledger.record(
                "perceive_apify", source=source, actor=ACTOR, ok=False, error=str(error)[:300],
                cost_usd=APIFY_COST_USD,
            )  # fmt: skip
            raise
        ledger.record(
            "perceive_apify", source=source, actor=ACTOR, ok=True, cost_usd=APIFY_COST_USD,
            caption_chars=len(_text(item.get("caption"))), has_transcript=bool(_transcript(item)),
        )  # fmt: skip
        video = folder / "reel.mp4"
        download(str(item.get("videoUrl") or ""), video)
        caption, transcript = _text(item.get("caption")), _transcript(item)
        author = _text(item.get("ownerUsername"))
    result = workshop.run(
        STRIP_CODE,
        {"frames": FRAMES, "cut_score": CUT_SCORE, "palette": PALETTE, "audio_max_s": AUDIO_MAX_S},
        {"reel.mp4": video},
        limits=limits,
        image=image,
    )
    if not result.ok or "strip.png" not in result.outputs:
        raise PerceiveError(f"could not read the video: {result.error} {result.log[-300:]}")
    strip = folder / "reel-strip.png"
    strip.write_bytes(result.outputs["strip.png"])
    info = result.value
    # local Whisper first (free); the transcript from Apify or the sidecar only when it hears no speech
    heard, engine = "", "none"
    if "audio.wav" in result.outputs:
        (folder / "audio.wav").write_bytes(result.outputs["audio.wav"])
        heard = transcribe(folder / "audio.wav")
    if heard:
        transcript, engine = heard, "whisper"
    elif transcript:
        engine = "apify" if author else "sidecar"
    reel = Reel(
        source=source, caption=caption, transcript=transcript, author=author, video=video, strip=strip,
        times=tuple(info["times"]), duration_s=float(info["duration_s"]), width=int(info["width"]),
        height=int(info["height"]), fps=float(info["fps"]), cuts=tuple(info.get("cuts", ())),
        palette=tuple(info.get("palette", ())),
    )  # fmt: skip
    ledger.record(
        "perceived", source=source, duration_s=reel.duration_s, width=reel.width, height=reel.height,
        fps=reel.fps, transcript_chars=len(transcript), transcript_from=engine, cuts=len(reel.cuts),
        palette=[c.get("hex") for c in reel.palette], seconds=result.duration_s,
    )  # fmt: skip
    return reel


def transcribe(wav: Path) -> str:
    """Speech in the reel's audio, by local Whisper. Empty when Whisper is missing or hears no speech.
    The audio was cut out of the untrusted video in the workshop; here only a plain WAV is read."""
    binary = shutil.which("whisper-cli")
    model = Path(os.environ.get("CREATURE_WHISPER_MODEL", WHISPER_MODEL)).expanduser()
    if not binary or not model.is_file():
        return ""
    try:
        done = subprocess.run(
            [binary, "-m", str(model), "-f", str(wav), "-l", "auto", "-nt", "-np", "-sns"],
            capture_output=True, text=True, timeout=WHISPER_TIMEOUT_S,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = " ".join(done.stdout.split()) if done.returncode == 0 else ""
    return text if len(text.split()) >= MIN_WORDS else ""


def fetch_reel(url: str) -> dict[str, Any]:
    """One reel from Apify: caption, transcript, video URL. Capped in time and in dollars."""
    query = urllib.parse.urlencode({"timeout": APIFY_TIMEOUT_S, "maxTotalChargeUsd": APIFY_MAX_CHARGE_USD})
    request = urllib.request.Request(
        f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items?{query}",
        data=json.dumps({"username": [url], "resultsLimit": 1, "includeTranscript": True}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {keys.get('APIFY_TOKEN')}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=APIFY_TIMEOUT_S + 30) as response:
            body = response.read(MAX_APIFY_BYTES + 1)
        if len(body) > MAX_APIFY_BYTES:
            raise PerceiveError("Apify answer is too large")
        items = json.loads(body)
    except (OSError, json.JSONDecodeError) as error:
        raise PerceiveError(f"Apify: {error}") from None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise PerceiveError("Apify returned no reel")
    if items[0].get("type") not in (None, "Video") or not items[0].get("videoUrl"):
        raise PerceiveError("not a video reel")
    return items[0]


def _check_cdn(url: str) -> None:
    parts = urllib.parse.urlparse(url)
    host = parts.hostname or ""
    if parts.scheme != "https" or not (host.endswith(".cdninstagram.com") or host.endswith(".fbcdn.net")):
        raise PerceiveError(f"video host not allowed: {host or url[:60]}")


class RedirectWithinCdn(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to another allowed CDN host, so the allowlist survives a 302."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        _check_cdn(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str):
    return urllib.request.build_opener(RedirectWithinCdn()).open(url, timeout=60)


def download(url: str, target: Path) -> None:
    """The reel video, from Instagram's CDN only (redirects included), at most MAX_VIDEO_BYTES."""
    _check_cdn(url)
    try:
        with _open(url) as response, target.open("wb") as out:
            size = 0
            while chunk := response.read(1 << 16):
                size += len(chunk)
                if size > MAX_VIDEO_BYTES:
                    raise PerceiveError("video is too large")
                out.write(chunk)
    except OSError as error:
        raise PerceiveError(f"download: {error}") from None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _transcript(item: dict[str, Any]) -> str:
    # the add-on returns either plain text or a list of segments
    value = item.get("transcript")
    if isinstance(value, list):
        return " ".join(_text(seg.get("text") if isinstance(seg, dict) else seg) for seg in value).strip()
    return _text(value)
