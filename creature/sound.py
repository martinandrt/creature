"""Sound for a finished clip: offline, no model, from the read-only asset library.

Every clip the creature makes is silent. This module lays sound under it with fixed code that runs in the
workshop (the library is mounted at /assets there): short sounds on the moments the picture changes and a
quiet bed under the whole clip. Nothing is generated and nothing is learned; the same clip, cues, library
and run id always give the same mix. The silent original is never touched: the result is written next to
it as `<name>-sound.mp4` (`clip-sound.mp4`, `montage-sound.mp4`), and only when its measurement passes.

Cues are frame numbers. A montage hands in its timeline's cuts; a clip with no timeline is searched in the
workshop (see Detection). The sounds are chosen from the library's manifest by category:

  hit      "úder" (uder-*)        on a hard cut, alternating with a whoosh
  whoosh   "švih" (svih-*)        on a hard cut, alternating with a hit
  key      "klávesa", "klik"      on cues that come 6 or more to a second (any 1 s window holding 6 cues)
  riser    "náběh" (nabeh-*)      from the first frame, when one is short enough to end before the first cue
  bed      a loop ("smycka")      the pad (tags pad/klid) when there is one; under the whole clip

Choice is seeded by the run id. Each category is drawn like a shuffled bag (every sound once before any
repeats), and a sound is never the same file as the one just before it, nor as the last of its own category.
Placement: a sound's audible onset lands on its cue frame. The part of a slow swell more than 8 dB under
the sound's own peak level is cut off (a whoosh then arrives instead of creeping in), and the sound is laid
over the bed in a trial: the onset detector the measurement uses says how long after its first sample it
becomes audible, and the sound starts that much earlier. A sound is faded out over 30 ms to be silent when
the next one starts, so fast cuts stay distinct. The bed sits 20 dB under the sounds (every sound is first
brought to -3 dBFS), with fades, looped to the clip. The mix is stereo at 48 kHz, exactly the clip's length,
peak-normalised to -1 dBFS (a boost is capped at 6 dB, so a clip with no cues keeps a quiet bed instead of
a loud one), muxed with `-c:v copy` and AAC 192k. AAC can overshoot a sharp transient, so the encoded file
is decoded again: if its peak is over -0.5 dBFS the mix is lowered by the overshoot and encoded once more.

Detection (a clip with no timeline), thresholds in PARAMS:

  cuts      ffmpeg's scene score of at least 0.3; flagged frames within 2 frames of each other are one cut.
  onsets    mean absolute difference between consecutive frames of a 72 px wide grey picture. A change is
            strong when it reaches 25% of the clip's own strongest change outside the cuts and at least 0.15
            (on 0-255). An onset is the first strong frame after 3 calm ones whose change is at least 3 times
            their average (a blip on steady motion is not one); a cut counts as strong, so the frames
            just after a cut are never an onset.

Measurement, on the OUTPUT file in a second workshop run (MEASURE_CODE): it decodes the audio and reports
whether there is an audio stream, the RMS over the whole file (dBFS), the peak (dBFS), how long the audio is
against the picture, and the share of the cue frames that have an audio onset within 2 frames. The audio
onset detector: 20 ms windows every 5 ms give a level in dB, once of the signal itself (thumps) and once of
the pre-emphasised signal x[n] - 0.97 x[n-1] (clicks, swishes; a pad's slow wobble stays under it). A window
is an onset when either level is 6 dB above the lowest level in the 30 ms before it and above -75 dB; the
first window of a run of such windows counts, and two onsets are at least 50 ms apart. The mixer uses the same
detector to place a sound, so the measurement is not blind to it: as a check that the share is not chance,
the same share is taken with every cue moved half a gap off (`chance_share`, near zero when the sound sits
on the cuts; not taken when the cues are closer than 6 frames). Cue frame 0 is not counted: a riser has no
onset.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from creature import workshop

SUFFIX = "-sound"
TOLERANCE_FRAMES = 2  # an audio onset within this many frames of a cue is on it
MIN_ALIGNED = 0.8  # the share of cues on an onset below which the sound is not kept
RMS_MIN_DB = -40.0  # quieter than this over the whole file is as good as silent
PEAK_MAX_DB = 0.0  # at or below full scale
LENGTH_TOLERANCE_S = 2 / 30  # the audio may differ from the picture by two frames
MAX_LISTED = 200  # cues and placements kept in a result

PARAMS: dict[str, Any] = {
    "rate": 48000,
    # detection
    "scene_score": 0.3,
    "cut_merge_frames": 2,
    "motion_width": 72,
    "motion_floor": 0.15,
    "motion_share": 0.25,
    "motion_rest_frames": 3,
    "motion_rise": 3.0,
    # choice
    "dense_per_s": 6,
    # the audio onset detector
    "tolerance_frames": TOLERANCE_FRAMES,
    "win": 960,  # 20 ms
    "hop": 240,  # 5 ms
    "emphasis": 0.97,
    "rise_db": 6.0,
    "rise_span": 6,  # 30 ms
    "floor_db": -75.0,
    "min_gap": 10,  # 50 ms
    # the mix
    "sample_peak": 0.7079,  # -3 dBFS, whatever the library says
    "bed_db": -20.0,
    "final_peak_db": -1.0,
    "max_boost_db": 6.0,
    "encoded_ceiling_db": -0.5,
    "fade_in_s": 0.3,
    "fade_out_s": 0.5,
    "release_s": 0.03,
    "max_lead_s": 0.25,
    "trim_db": 8.0,
    "max_trim_s": 0.6,
    "gain_db": {"hit": 0.0, "whoosh": -2.0, "key": -6.0, "riser": -6.0},
}

# Fixed code (ours) shared by both workshop runs: the audio onset detector, probing and decoding.
ONSET_CODE = r"""
import json
import math
import os
import random
import re
import subprocess
import unicodedata

import numpy as np

P = PARAMS
RATE = P["rate"]


def energy_db(x, emphasis=True):
    # level of 20 ms windows every 5 ms of the (pre-emphasised) signal, in dB of mean square
    win, hop = P["win"], P["hop"]
    x = np.asarray(x, dtype=np.float64)
    if emphasis and len(x) > 1:
        x = np.concatenate([[0.0], x[1:] - P["emphasis"] * x[:-1]])
    if len(x) < win:
        x = np.pad(x, (0, win - len(x)))
    count = 1 + (len(x) - win) // hop
    total = np.concatenate([[0.0], np.cumsum(x * x)])
    starts = np.arange(count) * hop
    return 10.0 * np.log10(np.maximum((total[starts + win] - total[starts]) / win, 1e-10))


def audio_onsets(x):
    # seconds of the windows (their centres) that rise rise_db over the 30 ms before, in the pre-emphasised
    # signal (clicks, swishes) or in the plain one (thumps); the first window of each run counts
    levels = [energy_db(x, True), energy_db(x, False)]
    found, running, last = [], False, -10**9
    for k in range(1, len(levels[0])):
        hit = any(
            level[k] - level[max(0, k - P["rise_span"]):k].min() >= P["rise_db"] and level[k] > P["floor_db"]
            for level in levels
        )
        if hit and not running and k - last >= P["min_gap"]:
            found.append((k * P["hop"] + P["win"] / 2) / RATE)
            last = k
        running = hit
    return found


def sh(args, text=False):
    return subprocess.run(args, capture_output=True, text=text)


def probe(path):
    entries = ("stream=codec_type,codec_name,width,height,avg_frame_rate,nb_read_packets,duration,channels"
               ":format=duration")
    out = sh(["ffprobe", "-v", "error", "-count_packets", "-show_entries", entries, "-of", "json", path],
             True)
    if out.returncode != 0:
        raise RuntimeError("cannot read the file: " + out.stderr[-300:])
    info = json.loads(out.stdout)
    video = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    audio = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    result = {"audio": bool(audio), "audio_s": None, "audio_channels": None, "video": bool(video)}
    if audio:
        result["audio_channels"] = audio[0].get("channels")
        try:
            result["audio_s"] = float(audio[0].get("duration") or info["format"]["duration"])
        except (KeyError, TypeError, ValueError):
            result["audio_s"] = None
    if video:
        v = video[0]
        num, den = (v.get("avg_frame_rate") or "0/1").split("/")
        fps = float(num) / float(den) if float(den) else 0.0
        seconds = float(v.get("duration") or info.get("format", {}).get("duration") or 0)
        frames = int(v.get("nb_read_packets") or 0) or round(seconds * fps)
        result.update(width=int(v.get("width") or 0), height=int(v.get("height") or 0), fps=fps,
                      frames=frames, video_s=frames / fps if fps and frames else seconds)
    return result


def decode(path, channels):
    # float samples at the mix rate: shape (n, channels)
    out = sh(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vn", "-f", "f32le", "-ar", str(RATE),
              "-ac", str(channels), "-"])
    if out.returncode != 0:
        raise RuntimeError("cannot decode audio: " + out.stderr.decode(errors="replace")[-300:])
    return np.frombuffer(out.stdout, dtype=np.float32).reshape(-1, channels)
"""

# Fixed code (ours) that runs in the workshop on the silent clip: find cues, choose, place, mix, mux.
SOUND_BODY = r"""
KINDS = {"uder": "hit", "svih": "whoosh", "nabeh": "riser", "klavesa": "key", "klik": "key"}


def scene_cuts(path, fps):
    select = "select='gt(scene," + str(P["scene_score"]) + ")',showinfo"
    found = sh(["ffmpeg", "-v", "info", "-nostdin", "-i", path, "-vf", select, "-an", "-f", "null", "-"],
               True)
    frames = sorted({round(float(t) * fps) for t in re.findall(r"pts_time:([0-9.]+)", found.stderr)})
    cuts = []
    for f in frames:
        if f > 0 and (not cuts or f - cuts[-1] > P["cut_merge_frames"]):
            cuts.append(f)
    return cuts


def motion_onsets(path, info, cuts):
    width = P["motion_width"]
    height = max(2, 2 * round(width * info["height"] / info["width"] / 2))
    scale = "scale=" + str(width) + ":" + str(height) + ":flags=area,format=gray"
    raw = sh(["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vf", scale, "-f", "rawvideo", "-"]).stdout
    size = width * height
    count = len(raw) // size
    if count < 3:
        return []
    grey = np.frombuffer(raw[:count * size], dtype=np.uint8).reshape(count, size).astype(np.float32)
    change = np.concatenate([[0.0], np.abs(np.diff(grey, axis=0)).mean(axis=1)])  # change[f]: into frame f
    is_cut = np.zeros(count, dtype=bool)
    for c in cuts:
        is_cut[max(0, c):min(count, c + P["cut_merge_frames"] + 1)] = True
    outside = change[~is_cut]
    peak = float(outside.max()) if len(outside) else 0.0
    limit = max(P["motion_floor"], P["motion_share"] * peak)
    strong = (change >= limit) | is_cut
    rest, rise = P["motion_rest_frames"], P["motion_rise"]
    return [
        f for f in range(rest, count)
        if strong[f] and not is_cut[f] and not strong[f - rest:f].any()
        and change[f] >= rise * max(float(change[f - rest:f].mean()), 0.05)
    ]


def detect(path, info):
    cuts = scene_cuts(path, info["fps"])
    cues = [{"frame": f, "kind": "cut"} for f in cuts]
    cues += [{"frame": f, "kind": "onset"} for f in motion_onsets(path, info, cuts)]
    return sorted(cues, key=lambda c: c["frame"])


def unaccent(text):
    plain = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in plain if not unicodedata.combining(c)).lower()


def library_sounds(root):
    # the manifest's sounds by category; a file that is not there is left out
    with open(root + "/manifest.json", encoding="utf-8") as file:
        manifest = json.load(file)
    kinds = {"hit": [], "whoosh": [], "riser": [], "key": [], "loop": [], "pad": []}
    sounds = [e for e in manifest.get("prvky", []) if e.get("kategorie") == "zvuk"]
    for e in sorted(sounds, key=lambda e: str(e.get("jmeno"))):
        rel = str(e.get("soubor") or "")
        if not rel or rel.startswith("/") or ".." in rel.split("/") or not os.path.isfile(root + "/" + rel):
            continue
        entry = {"path": rel, "name": str(e.get("jmeno")), "length": e.get("delka_s")}
        if e.get("smycka") is True:
            kinds["loop"].append(entry)
            if {"pad", "klid"} & {unaccent(t) for t in e.get("stitky", [])}:
                kinds["pad"].append(entry)
            continue
        word = unaccent(e.get("podkategorie") or str(e.get("jmeno")).split("-")[0])
        if word in KINDS:
            kinds[KINDS[word]].append(entry)
    return kinds


def make_plan(sounds, cues, frames, fps, seed):
    # which sound goes on which cue; pure choice, no audio
    rng = random.Random(str(seed) + ":sound")
    seen = {}
    for c in cues:
        f = int(c["frame"])
        if 0 < f < frames:
            seen[f] = "cut" if "cut" in (seen.get(f), c.get("kind", "cut")) else "onset"
    events = sorted(seen.items())
    at = [f for f, _ in events]
    dense, need = set(), P["dense_per_s"]
    for j in range(len(at) - need + 1):
        if at[j + need - 1] - at[j] < fps:  # `need` cues inside one second
            dense.update(at[j:j + need])
    bags, last_of, previous = {k: [] for k in sounds}, {}, None

    def draw(kind):
        if not bags[kind]:
            bags[kind] = list(sounds[kind])
            rng.shuffle(bags[kind])
        avoid = {previous, last_of.get(kind)}
        pick = next((i for i, e in enumerate(bags[kind]) if e["path"] not in avoid), 0)
        return bags[kind].pop(pick)

    placements, turn = [], 0
    if events:
        fits = [
            e for e in sounds["riser"] if isinstance(e["length"], int | float) and e["length"] <= at[0] / fps
        ]
        if fits:
            riser = rng.choice(fits)
            placements.append({"kind": "riser", "file": riser["path"], "frame": 0, "anchor": False})
    for f, how in events:
        if f in dense:
            kind = "key"
        else:
            kind = ("hit", "whoosh")[turn % 2]
            turn += 1
            if not sounds[kind]:
                kind = "whoosh" if kind == "hit" else "hit"
        if not sounds[kind]:
            continue
        pick = draw(kind)
        previous = last_of[kind] = pick["path"]
        placements.append({"kind": kind, "file": pick["path"], "frame": f, "anchor": True, "cue": how})
    bed = None
    if sounds["loop"]:
        pool = sounds["pad"] or sounds["loop"]
        bed = pool[rng.randrange(len(pool))]["path"]
    return {"placements": placements, "bed": bed, "cues": at, "dense": sorted(dense)}


def load_sound(root, rel):
    x = decode(root + "/" + rel, 2).astype(np.float32)
    peak = float(np.abs(x).max()) if len(x) else 0.0
    return x * (P["sample_peak"] / peak) if peak > 0 else x


def trim_lead(x):
    # a slow swell has no onset: its leading part, more than trim_db under the sound's own peak level, is cut
    # off (at most max_trim_s) and the rest comes in over 5 ms
    level = energy_db(x.mean(axis=1), emphasis=False)
    first = int(np.argmax(level >= level.max() - P["trim_db"]))
    cut = min(first * P["hop"], int(P["max_trim_s"] * RATE))
    if cut <= 0 or cut >= len(x):
        return x
    y = x[cut:].copy()
    ramp = min(len(y), int(0.005 * RATE))
    y[:ramp] *= np.linspace(0, 1, ramp, dtype=np.float32)[:, None]
    return y


def lead_of(bed, x, at):
    # how long after its first sample the sound becomes audible over the bed, by the measurement's detector
    pad = int(0.1 * RATE)
    lo = max(0, at - pad)
    head = x[:int((P["max_lead_s"] + 0.1) * RATE)]
    hi = min(len(bed), at + len(head) + pad)
    trial = bed[lo:hi].copy()
    end = min(len(trial), at - lo + len(head))
    trial[at - lo:end] += head[:end - (at - lo)]
    base = audio_onsets(bed[lo:hi].mean(axis=1))
    start = (at - lo) / RATE
    found = audio_onsets(trial.mean(axis=1))
    own = [t for t in found if t >= start - 0.02 and all(abs(t - b) > 0.02 for b in base)]
    return min(max(own[0] - start, 0.0), P["max_lead_s"]) if own else 0.0


def render(n, placements, audio, bed_audio, fps):
    # the mix as float32 (n, 2), and its parts: the bed and the gain as mixed, and each placed sound
    bed = np.zeros((n, 2), dtype=np.float32)
    if bed_audio is not None and len(bed_audio):
        bed = np.tile(bed_audio, (-(-n // len(bed_audio)), 1))[:n] * np.float32(10 ** (P["bed_db"] / 20))
        up, down = min(int(P["fade_in_s"] * RATE), n // 4), min(int(P["fade_out_s"] * RATE), n // 4)
        if up:
            bed[:up] *= np.linspace(0, 1, up, dtype=np.float32)[:, None]
        if down:
            bed[n - down:] *= np.linspace(1, 0, down, dtype=np.float32)[:, None]
    items = []
    for p in placements:
        x = audio[p["file"]]
        if p["anchor"]:
            x = trim_lead(x)
        x = x * np.float32(10 ** (P["gain_db"][p["kind"]] / 20))
        at = int(round(p["frame"] * RATE / fps))
        hits_at_once = p["kind"] == "key"  # a click has no swell to wait for
        lead = lead_of(bed, x, at) if p["anchor"] and not hits_at_once and at < n else 0.0
        items.append({**p, "x": x, "start": max(0, at - int(round(lead * RATE))), "lead_s": round(lead, 4)})
    items.sort(key=lambda i: i["start"])
    mix, release = bed.copy(), int(P["release_s"] * RATE)
    for pos, item in enumerate(items):
        start, x = item["start"], item["x"]
        if start >= n:
            continue
        end, cut = min(n, start + len(x)), start + len(x) > n
        if pos + 1 < len(items) and items[pos + 1]["start"] < end:
            end, cut = max(start + 1, items[pos + 1]["start"]), True  # silent when the next one starts
        piece = x[:end - start].copy()
        if cut:
            fade = min(release, len(piece))
            piece[len(piece) - fade:] *= np.linspace(1, 0, fade, dtype=np.float32)[:, None]
        mix[start:end] += piece
        item["end"] = end
    peak = float(np.abs(mix).max()) if n else 0.0
    gain = 1.0
    if peak > 0:
        gain = min(10 ** (P["final_peak_db"] / 20) / peak, 10 ** (P["max_boost_db"] / 20))
    parts = {"bed": bed * np.float32(gain), "gain": gain, "items": items}
    return (mix * np.float32(gain)).astype(np.float32), parts


def run(input, work):
    root, clip = input.get("assets", "/assets"), work + "/in/clip.mp4"
    info = probe(clip)
    if not info["video"] or not info["frames"] or not info["fps"]:
        raise RuntimeError("the clip has no picture to put sound under")
    fps, frames = info["fps"], info["frames"]
    detected = input.get("cues") is None
    cues = detect(clip, info) if detected else input["cues"]
    plan = make_plan(library_sounds(root), cues, frames, fps, input["seed"])
    n = round(frames / fps * RATE)
    audio = {}
    for p in plan["placements"]:
        if p["file"] not in audio:
            audio[p["file"]] = load_sound(root, p["file"])
    bed_audio = load_sound(root, plan["bed"]) if plan["bed"] else None
    mix, parts = render(n, plan["placements"], audio, bed_audio, fps)
    ceiling, scale = 10 ** (P["encoded_ceiling_db"] / 20), 1.0
    for _ in range(3):  # AAC can overshoot a sharp transient: lower the mix until the file stays under
        with open(work + "/mix.raw", "wb") as file:
            file.write((mix * np.float32(scale)).tobytes())
        made = sh(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", clip,
                   "-f", "f32le", "-ar", str(RATE), "-ac", "2", "-i", work + "/mix.raw",
                   "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                   "-t", f"{n / RATE:.6f}", "-movflags", "+faststart", work + "/out/sound.mp4"], True)
        if made.returncode != 0:
            raise RuntimeError("muxing failed: " + made.stderr[-300:])
        peak = float(np.abs(decode(work + "/out/sound.mp4", 2)).max())
        if peak <= ceiling:
            break
        scale *= ceiling / peak * 0.98
    used = [p["file"] for p in plan["placements"]] + ([plan["bed"]] if plan["bed"] else [])
    notes = []
    if not plan["bed"]:
        notes.append("the library has no loop: no bed")
    if not any(p["kind"] == "riser" for p in plan["placements"]):
        notes.append("no riser fits before the first cue")
    listed = [
        {"kind": i["kind"], "file": i["file"], "frame": i["frame"], "start_s": round(i["start"] / RATE, 4),
         "lead_s": i["lead_s"]}
        for i in parts["items"]
    ]
    return {"frames": frames, "fps": fps, "video_s": info["video_s"], "detected": detected,
            "cues": plan["cues"], "dense": plan["dense"], "placements": listed, "bed": plan["bed"],
            "gain_db": round(20 * math.log10(parts["gain"] * scale), 2), "sounds": list(dict.fromkeys(used)),
            "notes": notes, "out": "sound.mp4"}
"""

# Fixed code (ours) that runs in the workshop on the OUTPUT file: what the file really holds.
MEASURE_BODY = r"""
def run(input, work):
    path = work + "/in/" + input["file"]
    info = probe(path)
    if not info["audio"]:
        return {"audio": False, "video_s": info.get("video_s"), "fps": info.get("fps")}
    x = decode(path, 2)
    fps, tolerance = float(input["fps"]), P["tolerance_frames"]
    onsets = audio_onsets(x.mean(axis=1))

    def share(frames):
        if not frames:
            return None
        return sum(any(abs(t * fps - f) <= tolerance for t in onsets) for f in frames) / len(frames)

    cues = [int(f) for f in input["cues"] if int(f) > 0]
    gaps = sorted(b - a for a, b in zip(cues, cues[1:]))
    gap = gaps[len(gaps) // 2] if gaps else 0
    shift = round(gap / 2) if gap >= 6 else 0  # room for a shifted cue to sit clear of the real ones
    square = float((x.astype(np.float64) ** 2).mean()) if len(x) else 0.0
    return {
        "audio": True, "channels": info["audio_channels"], "audio_s": info["audio_s"],
        "video_s": info.get("video_s"), "fps": fps, "samples": int(len(x)),
        "rms_db": 10 * math.log10(max(square, 1e-12)),
        "peak_db": 20 * math.log10(max(float(np.abs(x).max()), 1e-6)) if len(x) else -120.0,
        "cues": len(cues), "onsets": len(onsets), "aligned_share": share(cues),
        "chance_share": share([f + shift for f in cues]) if shift else None,
    }
"""

SOUND_CODE = "PARAMS = " + json.dumps(PARAMS) + "\n" + ONSET_CODE + SOUND_BODY
MEASURE_CODE = "PARAMS = " + json.dumps(PARAMS) + "\n" + ONSET_CODE + MEASURE_BODY


@dataclass
class Scored:
    """What scoring a clip came to. `out` is the sounded file when `ok`, else None."""

    ok: bool
    out: Path | None = None
    sounds: list[str] = field(default_factory=list)
    cues: list[int] = field(default_factory=list)
    aligned_share: float | None = None
    chance_share: float | None = None
    rms_db: float | None = None
    peak_db: float | None = None
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    placements: list[dict[str, Any]] = field(default_factory=list)
    detected: bool = False
    audio_s: float | None = None
    video_s: float | None = None
    seconds: float = 0.0

    @classmethod
    def failed(cls, *problems: str) -> Scored:
        return cls(False, problems=[p for p in problems if p])

    def event(self) -> dict[str, Any]:
        """The fields of the ledger's `scored` event."""
        return {
            "sounds": self.sounds, "cues": self.cues[:MAX_LISTED], "cue_count": len(self.cues),
            "aligned_share": self.aligned_share, "chance_share": self.chance_share,
            "rms_db": self.rms_db, "peak_db": self.peak_db, "ok": self.ok, "problems": self.problems[:6],
            "detected": self.detected, "seconds": self.seconds,
        }  # fmt: skip


def output_path(clip: Path) -> Path:
    """Next to the clip: clip.mp4 → clip-sound.mp4, montage.mp4 → montage-sound.mp4."""
    return clip.with_name(f"{clip.stem}{SUFFIX}.mp4")


def timeline_cues(t: dict[str, Any]) -> list[dict[str, Any]]:
    """The cuts of a montage's timeline (its cues, else where its scenes start)."""
    cues = [c for c in t.get("cues") or [] if isinstance(c, dict) and "frame" in c]
    if not cues:
        cues = [{"frame": s["start"], "kind": "cut"} for s in t.get("scenes") or [] if "start" in s]
    return [{"frame": int(c["frame"]), "kind": str(c.get("kind", "cut"))} for c in cues]


def _cues(cues: Any) -> list[dict[str, Any]] | None:
    if cues is None:
        return None
    return [
        {"frame": int(c["frame"]), "kind": str(c.get("kind", "cut"))}
        if isinstance(c, dict)
        else {"frame": int(c), "kind": "cut"}
        for c in cues
    ]


def score(
    clip: Path,
    *,
    seed: str,
    limits: workshop.Limits,
    image: str,
    cues: Any = None,
    out: Path | None = None,
) -> Scored:
    """Put sound under `clip`, measure the result, and write it next to the clip only if it passes.

    `cues` are cut frames (ints, or dicts with "frame" and "kind"); None looks for them in the clip. The
    mix is made in the workshop, then the very bytes that would be written are measured in a second run;
    the file appears (as `output_path(clip)`, or `out`) only when that measurement passes, so a sounded
    file next to a clip always means a sound that passed. A clip the workshop cannot handle is a failed
    Scored with the reason, not an exception."""
    clip = Path(clip)
    target = Path(out) if out is not None else output_path(clip)
    if target.resolve() == clip.resolve():
        raise ValueError("the sounded clip must not overwrite the silent one")
    if not limits.assets:
        return Scored.failed("no asset library is mounted")
    job = {"seed": str(seed), "cues": _cues(cues), "assets": workshop.ASSETS}
    made = workshop.run(SOUND_CODE, job, {"clip.mp4": clip}, limits=limits, image=image)
    value = made.value if isinstance(made.value, dict) else {}
    data = made.outputs.get(value.get("out", "sound.mp4")) if made.ok else None
    if not made.ok or not data:
        return Scored.failed(f"the sound could not be mixed: {made.error or 'no output'}"[:300])
    with tempfile.TemporaryDirectory() as folder:
        candidate = Path(folder) / "sound.mp4"
        candidate.write_bytes(data)
        measured = workshop.run(
            MEASURE_CODE, {"file": "sound.mp4", "cues": value.get("cues", []), "fps": value.get("fps", 30.0)},
            {"sound.mp4": candidate}, limits=limits, image=image,
        )  # fmt: skip
    result = assess(value, measured.value if measured.ok else None, measured.error)
    result.seconds = round(made.duration_s + measured.duration_s, 2)
    if result.ok:
        target.write_bytes(data)
        result.out = target
    return result


def assess(made: dict[str, Any], measured: dict[str, Any] | None, error: str | None = None) -> Scored:
    """The verdict on a measurement: audio stream, RMS, peak, length and the share of cues on an onset."""
    result = Scored(
        False, sounds=list(made.get("sounds", [])), cues=[int(f) for f in made.get("cues", [])],
        placements=list(made.get("placements", []))[:MAX_LISTED], notes=list(made.get("notes", [])),
        detected=bool(made.get("detected")),
    )  # fmt: skip
    if not isinstance(measured, dict):
        result.problems.append(f"the written file could not be measured: {error or 'no result'}"[:300])
        return result
    if not measured.get("audio"):
        result.problems.append("the written file has no audio stream")
        return result
    result.rms_db = round(float(measured["rms_db"]), 1)
    result.peak_db = round(float(measured["peak_db"]), 1)
    result.audio_s, result.video_s = measured.get("audio_s"), measured.get("video_s")
    share, chance = measured.get("aligned_share"), measured.get("chance_share")
    result.aligned_share = None if share is None else round(float(share), 3)
    result.chance_share = None if chance is None else round(float(chance), 3)
    if result.rms_db <= RMS_MIN_DB:
        result.problems.append(f"the sound is too quiet: RMS {result.rms_db} dBFS (needs above {RMS_MIN_DB})")
    if result.peak_db > PEAK_MAX_DB:
        result.problems.append(f"the sound clips: peak {result.peak_db} dBFS (needs at most {PEAK_MAX_DB})")
    if result.audio_s is not None and result.video_s:
        gap = abs(float(result.audio_s) - float(result.video_s))
        if gap > LENGTH_TOLERANCE_S:
            result.problems.append(
                f"the audio lasts {float(result.audio_s):.2f} s, the picture {float(result.video_s):.2f} s"
            )
    if result.aligned_share is not None and result.aligned_share < MIN_ALIGNED:
        result.problems.append(
            f"only {result.aligned_share:.0%} of {measured.get('cues')} cues have an audio onset "
            f"within {TOLERANCE_FRAMES} frames (needs {MIN_ALIGNED:.0%})"
        )
    result.ok = not result.problems
    return result
