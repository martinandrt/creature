"""Allow/deny fixtures for the static gate, as data — for file skills in the workshop.

A skill is now `def run(input, work)`: it reads work/in/*, writes work/out/* and returns a JSON
value, inside the workshop container, which already has no network, no secrets and no mount
outside /work. The gate is a cheap first filter in front of that container, not the wall. Its
line is NETWORK: a skill that reaches for the net is refused before a container is started. The
tools of the trade (subprocess for ffmpeg, Pillow, numpy, pathlib/os for files, json) are
allowed. The old no-"__" rule is gone: it bought nothing the container does not already give.

PROPOSED policy, confirm with the main session. Not covered yet: the manifest shape
(capability.json: declared output width/height/fps/duration_s), which is a contract test.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GatePayload:
    id: str
    code: str
    verdict: str  # "allow" or "reject"
    note: str


def _skill(imports: str, body: str) -> str:
    return f"{imports}\ndef run(input, work):\n    {body}\n    return 'ok'\n"


_TOUCH = "open(work + '/out/x.txt', 'w').write('x')"

# Rejected: anything that talks to a network, reaches below Python, or has no entry point.
_REJECT = [
    GatePayload("import_socket", _skill("import socket", _TOUCH), "reject", "network import"),
    GatePayload("import_urllib", _skill("import urllib.request", _TOUCH), "reject", "network import"),
    GatePayload("from_urllib", _skill("from urllib.request import urlopen", _TOUCH), "reject", "network"),
    GatePayload("import_http", _skill("import http.client", _TOUCH), "reject", "network import"),
    GatePayload("import_ssl", _skill("import ssl", _TOUCH), "reject", "network import"),
    GatePayload("import_smtplib", _skill("import smtplib", _TOUCH), "reject", "mail is network"),
    GatePayload("import_ftplib", _skill("import ftplib", _TOUCH), "reject", "network import"),
    GatePayload("import_requests", _skill("import requests", _TOUCH), "reject", "network, not in the image"),
    GatePayload("import_ctypes", _skill("import ctypes", _TOUCH), "reject", "below Python"),
    GatePayload(
        "url_via_program",
        _skill("import subprocess", "subprocess.run(['ffmpeg', '-i', 'http://1.1.1.1/x.mp4', 'out.mp4'])"),
        "reject",
        "network through a program",
    ),
    GatePayload("no_run", "x = 1\n", "reject", "no run(input, work) entry point"),
]

# Allowed: the tools of the trade. A gate that rejects ffmpeg is useless for this creature.
_ALLOW = [
    GatePayload(
        "ffmpeg_render",
        _skill(
            "import subprocess",
            "subprocess.run(['ffmpeg', '-y', '-f', 'lavfi', '-i', 'color=c=red:s=64x64:d=1',"
            " work + '/out/clip.mp4'], check=True)",
        ),
        "allow",
        "subprocess for ffmpeg",
    ),
    GatePayload(
        "pillow_numpy",
        _skill(
            "import numpy as np\nfrom PIL import Image",
            "Image.fromarray(np.zeros((8, 8, 3), dtype=np.uint8)).save(work + '/out/a.png')",
        ),
        "allow",
        "image libraries",
    ),
    GatePayload(
        "params_and_files",
        _skill("from pathlib import Path", "Path(work, 'out', 't.txt').write_text(input['text'])"),
        "allow",
        "input in, files out",
    ),
    GatePayload(
        "os_for_files", _skill("import os", "os.makedirs(work + '/out', exist_ok=True)"), "allow", "os"
    ),
    GatePayload("math_and_text", _skill("import math, re", _TOUCH), "allow", "pure"),
    GatePayload(
        "main_guard_is_fine",
        _skill("", _TOUCH) + "if __name__ == '__main__':\n    run({}, '.')\n",
        "allow",
        "the no-__ rule is gone",
    ),
]

GATE_PAYLOADS: list[GatePayload] = [*_REJECT, *_ALLOW]
