import dataclasses
import json
import subprocess

import pytest

from creature import authority, workshop
from tests.conftest import REPO

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def image() -> str:
    return workshop.ensure_image(REPO / "workshop")


@pytest.fixture(scope="module")
def limits() -> workshop.Limits:
    return dataclasses.replace(authority.load(REPO).workshop, timeout_s=60)


def _run(image, limits, code, input=None, files=None, **change):
    return workshop.run(code, input or {}, files, limits=dataclasses.replace(limits, **change), image=image)


def test_value_comes_back(image, limits):
    result = _run(
        image, limits, "def run(input, work):\n    return {'n': len(input['text'])}", {"text": "abc"}
    )
    assert result.ok and result.value == {"n": 3} and result.outputs == {}


def test_files_go_in_and_come_out(image, limits, tmp_path):
    source = tmp_path / "words.txt"
    source.write_text("one two three")
    code = (
        "from pathlib import Path\n"
        "def run(input, work):\n"
        "    text = Path(work, 'in', 'words.txt').read_text()\n"
        "    Path(work, 'out', 'frames').mkdir()\n"
        "    Path(work, 'out', 'frames', 'count.txt').write_text(str(len(text.split())))\n"
        "    return 'done'"
    )
    result = _run(image, limits, code, files={"words.txt": source})
    assert result.ok and result.outputs == {"frames/count.txt": b"3"}


def test_prints_do_not_break_the_protocol(image, limits):
    result = _run(image, limits, "def run(input, work):\n    print('noise' * 1000)\n    return 7")
    assert result.ok and result.value == 7


def test_error_names_the_skill_line(image, limits):
    result = _run(image, limits, "def run(input, work):\n    x = 1\n    raise ValueError('bad frame')")
    assert not result.ok and "ValueError: bad frame" in result.error and "skill.py line 3" in result.error


def test_missing_run_function(image, limits):
    result = _run(image, limits, "x = 1")
    assert not result.ok and "run(input, workdir)" in result.error


def test_ffmpeg_renders_a_clip(image, limits):
    code = (
        "import subprocess\n"
        "def run(input, work):\n"
        "    source = 'color=c=red:s=320x240:d=1:r=30'\n"
        "    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', source,\n"
        "                    '-pix_fmt', 'yuv420p', f'{work}/out/clip.mp4'], check=True)\n"
        "    return 'ok'"
    )
    result = _run(image, limits, code)
    assert result.ok and result.outputs["clip.mp4"][4:8] == b"ftyp"


def test_pillow_and_numpy_are_there(image, limits):
    code = (
        "import numpy as np\n"
        "from PIL import Image\n"
        "def run(input, work):\n"
        "    Image.fromarray(np.zeros((4, 4, 3), dtype=np.uint8)).save(f'{work}/out/black.png')\n"
        "    return 'ok'"
    )
    result = _run(image, limits, code)
    assert result.ok and result.outputs["black.png"].startswith(b"\x89PNG")


def test_pinned_fonts_are_in_the_image(image, limits):
    # a font swap would silently change every render the judge compares
    code = (
        "from pathlib import Path\n"
        "def run(input, work):\n"
        "    fonts = Path('/usr/share/fonts').rglob('*')\n"
        "    return sorted(p.name for p in fonts if p.suffix in ('.ttf', '.otf'))"
    )
    result = _run(image, limits, code)
    assert result.ok
    assert "DejaVuSans-Bold.ttf" in result.value and any(name.startswith("Inter") for name in result.value)


def test_work_folder_cap_holds_while_the_skill_runs(image, limits):
    code = (
        "def run(input, work):\n"
        "    with open(f'{work}/out/big.bin', 'wb') as f:\n"
        "        for _ in range(64):\n"
        "            f.write(b'0' * 1_000_000)\n"
        "    return 'wrote it all'"
    )
    result = _run(image, limits, code, work_mb=16)
    assert not result.ok and "No space left" in result.error


def test_input_file_names_must_be_plain(image, limits, tmp_path):
    source = tmp_path / "a.txt"
    source.write_text("x")
    result = _run(image, limits, "def run(input, work):\n    return 1", files={"../a.txt": source})
    assert not result.ok and "plain file name" in result.error


def test_input_must_be_json(image, limits):
    result = _run(image, limits, "def run(input, work):\n    return 1", {1, 2})
    assert not result.ok and result.error.startswith("bad input")


def test_symlink_in_output_is_refused(image, limits):
    code = (
        "import os\ndef run(input, work):\n    os.symlink('/etc/passwd', f'{work}/out/passwd')\n    return 1"
    )
    result = _run(image, limits, code)
    assert not result.ok and "not a regular file" in result.error


def test_image_id_is_immutable_id(image):
    assert image.startswith("sha256:") and workshop.image_id(workshop.IMAGE) == image


def test_no_container_left_behind(image, limits):
    _run(image, limits, "def run(input, work):\n    while True:\n        pass", timeout_s=2)
    names = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=creature-", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert names == []


def test_result_json_from_the_skill_cannot_fake_success(image, limits):
    # the runner writes result.json outside out/, so a skill's own file is just an output
    code = (
        "import json\n"
        "def run(input, work):\n"
        "    open(f'{work}/out/result.json', 'w').write(json.dumps({'value': 'forged'}))\n"
        "    raise RuntimeError('real failure')"
    )
    result = _run(image, limits, code)
    assert not result.ok and "real failure" in result.error
    assert json.loads(result.outputs["result.json"]) == {"value": "forged"}
