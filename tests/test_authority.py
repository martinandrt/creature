import pytest

from creature import authority, home
from creature.authority import AUTHORITY_FILE, ENFORCERS, MISSING


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
    assert authority.fingerprint(state, code_root) == authority.fingerprint(state, code_root)


def test_any_byte_of_authority_changes_it(state, code_root):
    before = authority.fingerprint(state, code_root)
    (state / AUTHORITY_FILE).write_text('{"version": 1} \n')
    after = authority.fingerprint(state, code_root)
    assert after.digest != before.digest
    assert authority.changed(before, after) == [AUTHORITY_FILE]


@pytest.mark.parametrize("name", ENFORCERS)
def test_enforcer_change_changes_it(state, code_root, name):
    before = authority.fingerprint(state, code_root)
    (code_root / name).write_text("# edited\n")
    after = authority.fingerprint(state, code_root)
    assert authority.changed(before, after) == [name]


def test_missing_files_are_marked_and_count(state, code_root):
    (code_root / "broker.py").unlink()
    without = authority.fingerprint(state, code_root)
    assert without.parts["broker.py"] == MISSING
    (code_root / "broker.py").write_text("")
    assert authority.fingerprint(state, code_root).digest != without.digest


def test_default_code_root_is_the_package(state):
    print_ = authority.fingerprint(state)
    assert print_.parts["sandbox.py"] != MISSING
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
