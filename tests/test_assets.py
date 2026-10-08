"""The asset library: a host folder the workshop reads at /assets, read-only, part of the authority
fingerprint. The rule under test: whatever the workshop can read is fingerprinted, hidden entries are
neither mounted nor shown, and the forge is told about the library by tags, never the whole manifest."""

import dataclasses
import json
import os

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


def _spec():
    out = criteria.clip_format(2)
    return Spec("A circle grows", "circle-grow", "try", "", "Draw a circle.", "Hi", {}, out,
                ("The circle fills the frame.",), ("SECRET",), tuple(criteria.checks_for(out)))  # fmt: skip


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


def test_the_forge_sees_the_readme_and_the_items_that_match_the_task_plus_fonts(lib):
    shelf = deep.library(str(lib), _spec())
    assert shelf.startswith("Asset library, read-only at /assets")
    assert "Where things are" in shelf and len(shelf) < 2500 + 1500  # the README is cut at 2500
    assert "/assets/tvary/kruh.svg [tvary] kruh, circle, plny 0 0 100 100" in shelf
    assert "/assets/pisma/font.ttf" in shelf  # fonts always
    assert "sipka" not in shelf  # no shared word with the task: not shown
    assert deep.library(None, _spec()) == ""


def test_never_the_whole_manifest(tmp_path):
    root = library(tmp_path / "big")
    many = [
        {"jmeno": f"circle {i}", "kategorie": "tvary", "stitky": ["circle"], "soubor": f"tvary/c{i}.svg"}
        for i in range(deep.LIBRARY_SHOWN + 10)
    ]
    (root / "manifest.json").write_text(json.dumps({"prvky": many + ITEMS}))
    shelf = deep.library(str(root), _spec())
    assert shelf.count("/assets/tvary/") == deep.LIBRARY_SHOWN  # 41 match the task; the cap holds
    assert shelf.count("/assets/") == deep.LIBRARY_SHOWN + 1  # plus the font, always


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
