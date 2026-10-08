import copy
import json

import pytest

from creature import authority, home
from creature.authority import AUTHORITY_FILE, ENFORCERS, IMAGE_PART, MISSING, AuthorityError
from tests.conftest import REPO

IMAGE = "sha256:" + "a" * 64
SHIPPED = json.loads((REPO / AUTHORITY_FILE).read_text())


@pytest.fixture
def code_root(tmp_path):
    root = tmp_path / "code"
    root.mkdir()
    for name in ENFORCERS:
        (root / name).write_text(f"# {name}\n")
    return root


@pytest.fixture
def state(home):
    (home / AUTHORITY_FILE).write_text('{"version": 1}\n')
    return home


def test_fingerprint_is_stable(state, code_root):
    assert authority.fingerprint(state, code_root, image_id=IMAGE) == authority.fingerprint(
        state, code_root, image_id=IMAGE
    )


def test_any_byte_of_authority_changes_it(state, code_root):
    before = authority.fingerprint(state, code_root, image_id=IMAGE)
    (state / AUTHORITY_FILE).write_text('{"version": 1} \n')
    after = authority.fingerprint(state, code_root, image_id=IMAGE)
    assert after.digest != before.digest
    assert authority.changed(before, after) == [AUTHORITY_FILE]


@pytest.mark.parametrize("name", ENFORCERS)
def test_enforcer_change_changes_it(state, code_root, name):
    before = authority.fingerprint(state, code_root, image_id=IMAGE)
    (code_root / name).write_text("# edited\n")
    after = authority.fingerprint(state, code_root, image_id=IMAGE)
    assert authority.changed(before, after) == [name]


def test_missing_files_are_marked_and_count(state, code_root):
    (code_root / "forge.py").unlink()
    without = authority.fingerprint(state, code_root, image_id=IMAGE)
    assert without.parts["forge.py"] == MISSING
    (code_root / "forge.py").write_text("")
    assert authority.fingerprint(state, code_root, image_id=IMAGE).digest != without.digest


def test_missing_authority_file_is_refused(home, code_root):
    # "unchanged" must never be vacuously true: no authority.json, no fingerprint
    with pytest.raises(FileNotFoundError, match=AUTHORITY_FILE):
        authority.fingerprint(home, code_root, image_id=IMAGE)


def test_default_code_root_is_the_package(state):
    print_ = authority.fingerprint(state, image_id=IMAGE)
    assert print_.parts["workshop.py"] != MISSING
    assert len(print_.short) == 12


def test_home_resolves_explicit_then_env(tmp_path, monkeypatch):
    assert home.resolve(tmp_path) == tmp_path.resolve()
    monkeypatch.setenv(home.ENV, str(tmp_path))
    assert home.resolve() == tmp_path.resolve()


def test_home_needs_a_path(monkeypatch):
    monkeypatch.delenv(home.ENV)
    with pytest.raises(RuntimeError, match="CREATURE_HOME"):
        home.resolve()


def test_home_must_exist(tmp_path):
    with pytest.raises(FileNotFoundError):
        home.resolve(tmp_path / "nope")


def test_workshop_image_is_a_part(state, code_root):
    before = authority.fingerprint(state, code_root, image_id=IMAGE)
    after = authority.fingerprint(state, code_root, image_id="sha256:" + "b" * 64)
    assert before.parts[IMAGE_PART] == IMAGE
    assert authority.changed(before, after) == [IMAGE_PART]


def test_fingerprint_needs_the_image(state, code_root):
    with pytest.raises(ValueError, match="image"):
        authority.fingerprint(state, code_root, image_id="")


def test_shipped_authority_loads():
    loaded = authority.load(REPO)
    assert loaded.caps.run_budget_usd == SHIPPED["caps"]["run_budget_usd"]
    assert loaded.caps.forge_attempts == SHIPPED["caps"]["forge_attempts"]
    assert loaded.workshop.work_mb == SHIPPED["workshop"]["work_mb"]
    assert loaded.caps.step_cap_usd["judge"] == SHIPPED["caps"]["step_cap_usd"]["judge"]
    assert loaded.refuse and loaded.ask


def _write(home, change):
    raw = copy.deepcopy(SHIPPED)
    change(raw)
    (home / AUTHORITY_FILE).write_text(json.dumps(raw))
    return home


@pytest.mark.parametrize(
    "change",
    [
        lambda a: a["caps"].update(run_budget_usd=float("nan")),
        lambda a: a["caps"].update(run_budget_usd=-1),
        lambda a: a["caps"].update(call_reserve_usd=0),
        lambda a: a["caps"].update(forge_attempts=0),
        lambda a: a["caps"].update(forge_attempts=2.5),
        lambda a: a["caps"].update(forge_attempts=True),
        lambda a: a["caps"].update(step_cap_usd={}),
        lambda a: a["caps"]["step_cap_usd"].update(judge=0),
        lambda a: a["caps"].pop("run_budget_usd"),
        lambda a: a["workshop"].update(network=True),
        lambda a: a["workshop"].pop("network"),
        lambda a: a["workshop"].update(work_mb=0),
        lambda a: a["workshop"].update(timeout_s="120"),
        lambda a: a["workshop"].update(image=""),
        lambda a: a.update(refuse="everything"),
        lambda a: a.pop("caps"),
        lambda a: a.pop("models"),
        lambda a: a["models"].pop("default"),
        lambda a: a["models"].update(criteria=""),
    ],
)
def test_bad_authority_is_refused(home, change):
    with pytest.raises(AuthorityError):
        authority.load(_write(home, change))


def test_missing_or_broken_authority_is_refused(home):
    with pytest.raises(AuthorityError, match=r"no authority\.json"):
        authority.load(home)
    (home / AUTHORITY_FILE).write_text("{not json")
    with pytest.raises(AuthorityError, match="not JSON"):
        authority.load(home)
