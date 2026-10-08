"""Perception: a reel becomes a local video, its words and a strip of frames.

The spine fetches the reel's caption, transcript and video URL from Apify and downloads the video.
Everything that parses the untrusted video (ffprobe, frame extraction) runs in the workshop, never on
the host. A local .mp4 works too (with an optional sidecar <name>.json holding caption and transcript),
for tests and for reels saved earlier.
"""

from __future__ import annotations

import json
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
FRAMES = 12  # one strip: 6 columns x 2 rows, in time order

# Fixed code (ours, not generated) that runs in the workshop on the downloaded video.
STRIP_CODE = r"""
import json, subprocess

FIT = "scale=180:320:force_original_aspect_ratio=decrease,pad=180:320:(ow-iw)/2:(oh-ih)/2"

def run(input, work):
    video = f"{work}/in/reel.mp4"
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,avg_frame_rate:format=duration", "-of", "json", video],
        capture_output=True, text=True, check=True)
    info = json.loads(probe.stdout)
    stream, duration = info["streams"][0], float(info["format"]["duration"])
    num, den = stream["avg_frame_rate"].split("/")
    times = [round(duration * (i + 0.5) / input["frames"], 3) for i in range(input["frames"])]
    for i, t in enumerate(times):
        subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", video, "-frames:v", "1",
                        "-vf", FIT,
                        f"{work}/frame_{i:02d}.png"], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-i", f"{work}/frame_%02d.png",
                    "-vf", f"tile={input['columns']}x{input['rows']}", "-frames:v", "1",
                    f"{work}/out/strip.png"], check=True)
    return {"duration_s": duration, "width": stream["width"], "height": stream["height"],
            "fps": round(float(num) / float(den), 3) if float(den) else 0.0, "times": times}
"""


@dataclass(frozen=True)
class Reel:
    source: str  # reel URL or local file
    caption: str
    transcript: str
    author: str
    video: Path
    strip: Path  # 12 frames in time order, 6 per row
    times: tuple[float, ...]  # seconds at which the strip's frames were taken
    duration_s: float
    width: int
    height: int
    fps: float


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
        item = fetch_reel(source)
        ledger.record(
            "perceive_apify", source=source, actor=ACTOR, cost_usd=APIFY_COST_USD,
            caption_chars=len(_text(item.get("caption"))), has_transcript=bool(_transcript(item)),
        )  # fmt: skip
        video = folder / "reel.mp4"
        download(str(item.get("videoUrl") or ""), video)
        caption, transcript = _text(item.get("caption")), _transcript(item)
        author = _text(item.get("ownerUsername"))
    result = workshop.run(
        STRIP_CODE,
        {"frames": FRAMES, "columns": 6, "rows": 2},
        {"reel.mp4": video},
        limits=limits,
        image=image,
    )
    if not result.ok or "strip.png" not in result.outputs:
        raise PerceiveError(f"could not read the video: {result.error} {result.log[-300:]}")
    strip = folder / "reel-strip.png"
    strip.write_bytes(result.outputs["strip.png"])
    info = result.value
    reel = Reel(
        source=source, caption=caption, transcript=transcript, author=author, video=video, strip=strip,
        times=tuple(info["times"]), duration_s=float(info["duration_s"]), width=int(info["width"]),
        height=int(info["height"]), fps=float(info["fps"]),
    )  # fmt: skip
    ledger.record(
        "perceived", source=source, duration_s=reel.duration_s, width=reel.width, height=reel.height,
        fps=reel.fps, transcript_chars=len(transcript), seconds=result.duration_s,
    )  # fmt: skip
    return reel


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
            items = json.loads(response.read())
    except OSError as error:
        raise PerceiveError(f"Apify: {error}") from None
    if not isinstance(items, list) or not items or not isinstance(items[0], dict):
        raise PerceiveError("Apify returned no reel")
    if items[0].get("type") not in (None, "Video") or not items[0].get("videoUrl"):
        raise PerceiveError("not a video reel")
    return items[0]


def download(url: str, target: Path) -> None:
    """The reel video, from Instagram's CDN only, at most MAX_VIDEO_BYTES."""
    host = urllib.parse.urlparse(url).hostname or ""
    if urllib.parse.urlparse(url).scheme != "https" or not (
        host.endswith(".cdninstagram.com") or host.endswith(".fbcdn.net")
    ):
        raise PerceiveError(f"video host not allowed: {host or url[:60]}")
    try:
        with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as out:
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
