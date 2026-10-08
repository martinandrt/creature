"""The asset library: a host folder the workshop reads at /assets, read-only, part of the authority
fingerprint. The rule under test: whatever the workshop can read is fingerprinted, hidden entries are
neither mounted nor shown, and the forge is told about the library by tags, never the whole manifest."""

import base64
import dataclasses
import json
import os
import struct
import zlib

import pytest

from creature import authority, criteria, deep, workshop
from creature.criteria import Spec
from tests.conftest import REPO

IMAGE = "sha256:" + "a" * 64
KRUH = "<svg viewBox='0 0 100 100'><circle cx='50' cy='50' r='40'/></svg>"
ITEMS = [
    {"jmeno": "kruh", "kategorie": "tvary", "stitky": ["kruh", "circle", "plny"], "soubor": "tvary/kruh.svg",
     "viewBox": "0 0 100 100"},
    {"jmeno": "sipka", "kategorie": "sipky", "stitky": ["sipka", "arrow"], "soubor": "sipky/sipka.svg",
     "viewBox": "0 0 100 40"},
    {"jmeno": "font", "kategorie": "pisma", "stitky": ["bold"], "soubor": "pisma/font.ttf"},
]  # fmt: skip


def library(root):
    """A small library shaped like the real one: README, manifest, files, and hidden parts at two levels."""
    for item in ITEMS:
        path = root / item["soubor"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(KRUH if path.suffix == ".svg" else "font bytes", encoding="utf-8")
    (root / "README.md").write_text("Where things are: shapes in tvary/, arrows in sipky/.\n" + "x" * 3000)
    (root / "manifest.json").write_text(json.dumps({"knihovna": "test", "prvky": ITEMS}, ensure_ascii=False))
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text("{}")
    (root / ".DS_Store").write_bytes(b"\0")
    (root / "tvary" / ".cache").mkdir()
    (root / "tvary" / ".cache" / "x.txt").write_text("cached")
    return root


@pytest.fixture
def lib(tmp_path):
    return library(tmp_path / "lib")


def _spec(*said):
    out = criteria.clip_format(2)
    said = said or ("The circle fills the frame.",)
    return Spec("A circle grows", "circle-grow", "try", "", "Draw a circle.", "Hi", {}, out,
                said, ("SECRET",), tuple(criteria.checks_for(out)))  # fmt: skip


# --- authority -----------------------------------------------------------------------


def _authority(home, **shop):
    raw = json.loads((REPO / authority.AUTHORITY_FILE).read_text(encoding="utf-8"))
    raw["workshop"].update(shop)
    (home / authority.AUTHORITY_FILE).write_text(json.dumps(raw), encoding="utf-8")


def test_the_library_must_be_an_existing_folder(home, lib, tmp_path):
    _authority(home, assets=str(lib))
    assert authority.load(home).workshop.assets == str(lib)
    for bad in (str(tmp_path / "nowhere"), 3, str(lib / "README.md")):
        _authority(home, assets=bad)
        with pytest.raises(authority.AuthorityError, match="assets"):
            authority.load(home)


def test_the_library_is_a_part_of_the_fingerprint_only_when_set(home, lib):
    _authority(home)
    without = authority.fingerprint(home, image_id=IMAGE)
    assert authority.ASSETS_PART not in without.parts
    _authority(home, assets=str(lib))
    with_lib = authority.fingerprint(home, image_id=IMAGE)
    assert with_lib.parts[authority.ASSETS_PART] == workshop.assets_digest(str(lib))
    (lib / "tvary" / "kruh.svg").write_text(KRUH.replace("40", "41"))
    after = authority.fingerprint(home, image_id=IMAGE)
    assert authority.changed(with_lib, after) == [authority.ASSETS_PART]


# --- the digest ----------------------------------------------------------------------


def test_digest_follows_content_not_time_and_ignores_hidden_top_level_entries(lib):
    assert workshop.assets_digest(None) == "none"
    before = workshop.assets_digest(str(lib))
    os.utime(lib / "tvary" / "kruh.svg", (1, 1))
    assert workshop.assets_digest(str(lib)) == before  # mtime is not content
    (lib / ".claude" / "settings.json").write_text('{"changed": true}')
    (lib / ".DS_Store").write_bytes(b"\0\1")
    assert workshop.assets_digest(str(lib)) == before  # hidden at the top: not mounted, not counted
    (lib / "tvary" / "kruh.svg").write_text(KRUH.replace("40", "41"))
    assert workshop.assets_digest(str(lib)) != before
    (lib / "tvary" / "novy.svg").write_text(KRUH)
    assert workshop.assets_digest(str(lib)) not in (before, workshop.assets_digest(str(lib / "tvary")))


def test_a_file_the_workshop_can_read_is_in_the_digest(lib):
    # tvary/ is bind-mounted whole, so tvary/.cache/x.txt is readable at /assets/tvary/.cache/x.txt
    # (see the docker test below). What the workshop can read must be in the fingerprint: a change to
    # that file must change the digest. (Hidden entries at the TOP level are not mounted and stay out.)
    before = workshop.assets_digest(str(lib))
    (lib / "tvary" / ".cache" / "x.txt").write_text("changed under the mount")
    assert workshop.assets_digest(str(lib)) != before


# --- the mounts ----------------------------------------------------------------------


def test_mounts_are_the_visible_top_level_entries_read_only(lib):
    assert workshop.asset_mounts(None) == []
    (lib / "link").symlink_to(lib / "tvary")
    mounts = workshop.asset_mounts(str(lib))
    specs = [m for m in mounts if m != "--mount"]
    assert mounts.count("--mount") == len(specs) == 5
    assert specs == [
        f"type=bind,src={lib / name},dst={workshop.ASSETS}/{name},readonly"
        for name in ("README.md", "manifest.json", "pisma", "sipky", "tvary")
    ]  # no .claude, no .DS_Store, no symlink


# --- what the forge is told ----------------------------------------------------------


def test_the_forge_sees_the_readme_and_the_fonts_but_a_circle_task_picks_no_geometry(lib):
    shelf = deep.library(str(lib), _spec())
    assert shelf.startswith("Asset library, read-only at /assets")
    assert "Where things are" in shelf and len(shelf) < 2500 + 1500  # the README is cut at 2500
    assert "/assets/pisma/font.ttf" in shelf  # the clip has text, so it draws type
    assert "kruh" not in shelf and "sipka" not in shelf  # shapes and arrows: the code draws them
    assert deep.library(None, _spec()) == ""


def test_never_the_whole_manifest(tmp_path):
    root = library(tmp_path / "big")
    many = [
        {"jmeno": f"grain {i}", "kategorie": "textury", "stitky": ["grain"], "soubor": f"textury/g{i}.png"}
        for i in range(deep.LIBRARY_SHOWN + 10)
    ]
    (root / "manifest.json").write_text(json.dumps({"prvky": many + ITEMS}))
    shelf = deep.library(str(root), _spec("Film grain flickers over the frame."))
    assert shelf.count("/assets/textury/") == deep.LIBRARY_SHOWN  # 40 match the criteria; the cap holds
    assert shelf.count("/assets/") == deep.LIBRARY_SHOWN + 1  # plus the font, for the clip's text


def test_used_assets_are_the_paths_the_code_names_plus_what_the_forge_said():
    files = {
        "frame.py": "FONT = '/assets/pisma/font.ttf'\nIMG = \"/assets/tvary/kruh.svg\".\n",
        "layout.json": '{"icon": "/assets/ikony/hvezda.svg"}',
    }
    said = ["/assets/zare/zare-mekka.png", "not a path", "/etc/passwd", 3]
    assert deep.used_assets(files, said) == [
        "/assets/ikony/hvezda.svg",
        "/assets/pisma/font.ttf",
        "/assets/tvary/kruh.svg",
        "/assets/zare/zare-mekka.png",
    ]
    assert deep.used_assets({"frame.py": "no assets here"}, None) == []


# --- inside the workshop -------------------------------------------------------------

LOOK_CODE = r"""
import os
def run(input, work):
    out = {"top": sorted(os.listdir("/assets")), "kruh": open("/assets/tvary/kruh.svg").read(),
           "nested_hidden_readable": os.path.exists("/assets/tvary/.cache/x.txt")}
    for path in ("/assets/tvary/kruh.svg", "/assets/tvary/new.svg", "/assets/README.md"):
        try:
            with open(path, "a") as f:
                f.write("x")
            out[path] = "written"
        except OSError as error:
            out[path] = error.errno
    return out
"""


@pytest.mark.docker
def test_the_workshop_reads_the_library_and_cannot_write_it(lib):
    limits = dataclasses.replace(authority.load(REPO).workshop, timeout_s=60, assets=str(lib))
    image = workshop.ensure_image(REPO / "workshop")
    ran = workshop.run(LOOK_CODE, {}, limits=limits, image=image)
    assert ran.ok, ran.error
    seen = ran.value
    assert seen["top"] == ["README.md", "manifest.json", "pisma", "sipky", "tvary"]  # no .claude
    assert seen["kruh"] == KRUH
    assert seen["/assets/tvary/kruh.svg"] == 30 and seen["/assets/tvary/new.svg"] == 30  # EROFS
    assert seen["/assets/README.md"] == 30
    assert (lib / "tvary" / "kruh.svg").read_text() == KRUH and not (lib / "tvary" / "new.svg").exists()
    assert seen["nested_hidden_readable"] is True  # hence the digest test above


def test_a_declared_asset_path_never_leaves_the_library():
    # capability.json records what the skill reads under /assets; a path with ".." points elsewhere
    # (on the host, _existing would resolve it outside the library) and must be dropped, never recorded
    code = "A = '/assets/../etc/passwd'\nB = '/assets/tvary/../../x.svg'\nC = '/assets/tvary/kruh.svg'\n"
    files = {"frame.py": code}
    assert deep.used_assets(files, ["/assets/./pisma/../font.ttf"]) == ["/assets/tvary/kruh.svg"]


# --- SVG in the workshop -------------------------------------------------------------

# a verbatim copy of the library's tvary/kruh.svg: the workshop reads it at /assets like the real one
REAL_KRUH = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100" fill="none">'
    '<circle cx="50" cy="50" r="50" fill="currentColor"/></svg>'
)
SVG_CODE = r"""
import subprocess
from PIL import Image, UnidentifiedImageError

def run(input, work):
    path = "/assets/tvary/kruh.svg"
    try:
        Image.open(path).load()
        pillow = "opened"
    except UnidentifiedImageError:
        pillow = "UnidentifiedImageError"
    target = f"{work}/out/kruh.png"
    done = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-frames:v", "1", target],
                          capture_output=True, text=True)
    out = {"pillow": pillow, "ffmpeg": done.returncode, "stderr": done.stderr[-300:]}
    if done.returncode == 0:
        with Image.open(target) as raster:
            rgba = raster.convert("RGBA")
            out.update(size=list(raster.size), centre=list(rgba.getpixel((50, 50))),
                       corner=list(rgba.getpixel((1, 1))))
    return out
"""


@pytest.mark.docker
def test_the_workshop_rasterises_an_svg_from_the_library_with_ffmpeg_not_pillow(tmp_path):
    """Verdict: an SVG at /assets CAN be rendered in the workshop, by ffmpeg (its librsvg decoder). Pillow
    cannot open one, and ffmpeg draws it at the size the file declares: a 24 px icon is a 24 px raster."""
    lib = tmp_path / "lib"
    (lib / "tvary").mkdir(parents=True)
    (lib / "tvary" / "kruh.svg").write_text(REAL_KRUH)
    limits = dataclasses.replace(authority.load(REPO).workshop, timeout_s=60, assets=str(lib))
    ran = workshop.run(SVG_CODE, {}, limits=limits, image=workshop.ensure_image(REPO / "workshop"))
    assert ran.ok, ran.error
    seen = ran.value
    assert seen["pillow"] == "UnidentifiedImageError"  # hence the sheet's SVG tiles go through ffmpeg
    assert seen["ffmpeg"] == 0, seen["stderr"]
    assert seen["size"] == [100, 100]  # the declared width and height
    assert seen["centre"] == [0, 0, 0, 255] and seen["corner"][3] == 0  # currentColor is black; outside clear
    assert ran.outputs["kruh.png"].startswith(b"\x89PNG")


# --- the contact sheet, made by the workshop's own code ------------------------------


def _png(width, height, rgba):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + bytes(rgba) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    body = chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    return b"\x89PNG\r\n\x1a\n" + body


JPEG_16 = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcUFhYaHSUfGhsjHBYWICwg"
    "IyYnKSopGR8tMC0oMCUoKSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgo"
    "KCgoKCgoKCgoKCgoKCj/wAARCAAQABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
    "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6"
    "Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
    "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
    "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
    "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPE"
    "xcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDyrSfDv3fk/Suz0nw70+T9K7HSfDv3fk/S"
    "uz0nw7935P0rXHZ5vqLhXiP4dT//2Q=="
)
ICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">'
    '<path d="M3 3h18v18H3z"/></svg>'
)
PEEP = (
    "<svg viewBox='0 0 240 324'><rect x='20' y='20' width='200' height='280' fill='#000'/></svg>"  # no xmlns
)
SHEET_FILES = {
    "textury/grain.png": _png(54, 96, (200, 200, 200, 255)),
    "zare/glow.png": _png(64, 64, (255, 255, 255, 90)),
    "fotky/photo.jpg": JPEG_16,
    "ikony/box.svg": ICON.encode(),
    "postavy/peep.svg": PEEP.encode(),
    "textury/broken.png": b"not a picture",
    "textury/oops.svg": b"<svg",
}
PIXELS = r"""
from PIL import Image

def run(input, work):
    with Image.open(f"{work}/in/library.png") as sheet:
        return {"size": list(sheet.size), "box": list(sheet.convert("RGB").getpixel((632 + 40, 8 + 40))),
                "gutter": list(sheet.convert("RGB").getpixel((2, 2)))}
"""


@pytest.mark.docker
def test_the_sheet_code_tiles_png_jpeg_and_svg_skips_what_cannot_be_read_and_keeps_the_cap(tmp_path):
    lib = tmp_path / "lib"
    for name, data in SHEET_FILES.items():
        (lib / name).parent.mkdir(parents=True, exist_ok=True)
        (lib / name).write_bytes(data)
    limits = dataclasses.replace(authority.load(REPO).workshop, timeout_s=60, assets=str(lib))
    image = workshop.ensure_image(REPO / "workshop")
    items = [{"path": f"/assets/{name}", "label": name.split("/")[-1]} for name in SHEET_FILES]

    def sheet(cap, **more):
        ran = workshop.run(
            deep.SHEET_CODE, {"items": items, "max_bytes": cap}, limits=limits, image=image, **more
        )
        assert ran.ok, ran.error
        return ran

    ran = sheet(5_000_000)
    skipped = {path for path, _ in ran.value["skipped"]}
    assert skipped == {"/assets/textury/broken.png", "/assets/textury/oops.svg"}
    assert ran.value["tiles"] == 5 and ran.value["file"] == "library.png"
    png = ran.outputs["library.png"]
    assert png.startswith(b"\x89PNG") and len(png) == ran.value["bytes"]
    (tmp_path / "library.png").write_bytes(png)
    seen = workshop.run(PIXELS, {}, {"library.png": tmp_path / "library.png"}, limits=limits, image=image)
    assert seen.ok, seen.error
    assert seen.value["size"] == [8 + 5 * 208, 8 + 226]  # five tiles in one row, labels under them
    assert seen.value["box"] == [0, 0, 0] and seen.value["gutter"] == [
        127,
        127,
        127,
    ]  # the 24 px icon is drawn big

    tight = sheet(len(png) - 1)  # a smaller cap: JPEG or a smaller sheet, never over it
    assert len(tight.outputs[tight.value["file"]]) <= len(png) - 1
    hopeless = sheet(100)
    assert "error" in hopeless.value and hopeless.outputs == {}
