"""A small synthetic asset library for the sound tests: the real library's manifest fields (the ones the
sound code reads) and short WAV files made with numpy, so no test depends on ~/Desktop/creature-assets."""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np

RATE = 48000


def _write(path: Path, x: np.ndarray, channels: int = 1) -> None:
    pcm = (np.clip(x, -1, 1) * 32000).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as file:
        file.setnchannels(channels)
        file.setsampwidth(2)
        file.setframerate(RATE)
        file.writeframes(pcm.tobytes())


def _t(seconds: float) -> np.ndarray:
    return np.arange(int(seconds * RATE)) / RATE


def hit(number: int) -> np.ndarray:
    t = _t(0.25)
    rng = np.random.default_rng(number)
    return np.exp(-t / 0.05) * (
        0.7 * np.sin(2 * np.pi * (180 + 90 * number) * t) + 0.3 * rng.standard_normal(len(t))
    )


def whoosh(number: int) -> np.ndarray:
    t = _t(0.4)
    rng = np.random.default_rng(100 + number)
    envelope = np.minimum(t / 0.04, 1.0) * np.exp(-t / 0.15)
    return envelope * rng.standard_normal(len(t)) * 0.6


def key(number: int) -> np.ndarray:
    t = _t(0.03)
    rng = np.random.default_rng(200 + number)
    return np.exp(-t / 0.008) * rng.standard_normal(len(t))


def riser(number: int) -> np.ndarray:
    t = _t(1.0)
    rng = np.random.default_rng(300 + number)
    return (t / 1.0) ** 2 * rng.standard_normal(len(t)) * 0.5


def loop(number: int) -> np.ndarray:
    t = _t(2.0)  # whole periods only, so the seam is silent
    return 0.4 * np.sin(2 * np.pi * (110 + 55 * number) * t) + 0.2 * np.sin(
        2 * np.pi * (220 + 55 * number) * t
    )


CATEGORIES = {  # name -> (podkategorie, tags, maker, loop?)
    "uder": ("úder", ["uder", "hit"], hit),
    "svih": ("švih", ["svih", "whoosh"], whoosh),
    "klavesa": ("klávesa", ["klavesa", "psani"], key),
    "nabeh": ("náběh", ["nabeh", "riser"], riser),
}


def make_library(
    root: Path, *, hits: int = 2, whooshes: int = 0, keys: int = 0, risers: int = 0, loops: int = 1
) -> Path:
    """A library folder: manifest.json and zvuky/*.wav, with this many sounds of each category."""
    entries = []
    counts = {"uder": hits, "svih": whooshes, "klavesa": keys, "nabeh": risers}
    for word, count in counts.items():
        sub, tags, maker = CATEGORIES[word]
        for number in range(count):
            name = f"{word}-{chr(97 + number)}"
            x = maker(number)
            _write(root / "zvuky" / f"{name}.wav", x)
            entries.append(
                {
                    "jmeno": name, "kategorie": "zvuk", "podkategorie": sub, "stitky": tags,
                    "soubor": f"zvuky/{name}.wav", "delka_s": round(len(x) / RATE, 3), "smycka": False,
                }
            )  # fmt: skip
    for number in range(loops):
        name = f"smycka-{chr(97 + number)}"
        x = loop(number)
        _write(root / "zvuky" / f"{name}.wav", x)
        entries.append(
            {
                "jmeno": name, "kategorie": "zvuk", "podkategorie": "smyčka",
                "stitky": ["smycka", "pad", "klid"],
                "soubor": f"zvuky/{name}.wav", "delka_s": round(len(x) / RATE, 3), "smycka": True,
            }
        )  # fmt: skip
    # a graphic the sound code must ignore, and a sound whose file is missing
    entries.append({"jmeno": "kruh", "kategorie": "tvary", "soubor": "tvary/kruh.svg"})
    entries.append(
        {"jmeno": "uder-chybi", "kategorie": "zvuk", "podkategorie": "úder", "stitky": ["uder"],
         "soubor": "zvuky/uder-chybi.wav", "delka_s": 0.2, "smycka": False}
    )  # fmt: skip
    (root / "manifest.json").write_text(
        json.dumps({"knihovna": "test", "prvky": entries}, ensure_ascii=False), encoding="utf-8"
    )
    return root
