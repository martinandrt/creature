"""Practice: on command, the creature returns to its own failures, within the human's budget."""

import pytest

from creature import ledger, practice
from tests.fakes import FakeModel
from tests.test_loop import CODE, REPLY, _authority, _creature, _judge, world  # noqa: F401  (world: fixture)


def _fail(home, fake, reel):
    # criteria, then every forge attempt fails the judge: the run ends FAILED
    fake.queue("criteria", REPLY)
    for _ in range(3):
        fake.queue("forge", {"code": CODE, "approach": "a"}).queue("judge", _judge(False, True, True))
    report = _creature(home, fake).try_reel(str(reel), "Stay curious.")
    assert report.status == "FAILED", report.gap
    return report


def _returns(home):
    return [
        e for p in (home / "runs").glob("*.jsonl") for e in ledger.read(p) if e["type"] == "practice_return"
    ]


def _with_budget(home, usd):
    _authority(home, lambda raw: raw["caps"].__setitem__("practice_budget_usd", usd))


@pytest.mark.usefixtures("world")
def test_without_a_practice_budget_the_creature_does_not_return(home, tmp_path):
    reel = tmp_path / "reel.mp4"
    reel.write_bytes(b"reel")
    _fail(home, FakeModel(), reel)
    _authority(home, lambda raw: raw["caps"].pop("practice_budget_usd", None))  # a home without the key
    done = practice.practice(home, transport=FakeModel())
    assert done.returns == [] and "no practice budget" in done.stopped and _returns(home) == []


@pytest.mark.usefixtures("world")
def test_practice_returns_to_a_failure_records_it_and_stops_after_three_returns(home, tmp_path):
    reel = tmp_path / "reel.mp4"
    reel.write_bytes(b"reel")
    _fail(home, FakeModel(), reel)
    _with_budget(home, 4.0)
    for attempt in (1, 2, 3):
        fake = FakeModel()  # each return fails again: criteria, three forge attempts, each judged
        fake.queue("criteria", REPLY)
        for _ in range(3):
            fake.queue("forge", {"code": CODE, "approach": "a"}).queue("judge", _judge(False, True, True))
        done = practice.practice(home, transport=fake)
        [back] = done.returns
        assert back.attempt == attempt and back.status == "FAILED" and back.reel == "reel.mp4"
    events = _returns(home)
    assert sorted(e["attempt"] for e in events) == [1, 2, 3]
    assert all(e["reel"] == str(reel) and e["result"] == "FAILED" and e["took"] == [] for e in events)
    # a fourth practice leaves it alone: three returns, then it is a human's turn
    assert practice.practice(home, transport=FakeModel()).returns == []


@pytest.mark.usefixtures("world")
def test_a_return_that_passes_ends_the_failure_and_says_what_it_took_from_the_registry(home, tmp_path):
    reel = tmp_path / "reel.mp4"
    reel.write_bytes(b"reel")
    _fail(home, FakeModel(), reel)
    _with_budget(home, 4.0)
    fake = FakeModel()
    fake.queue("criteria", REPLY).queue("forge", {"code": CODE, "approach": "a"})
    fake.queue("judge", _judge(True, True, True))
    [back] = practice.practice(home, transport=fake).returns
    assert back.status == "BUILT"
    assert practice.failures(home) == []  # its latest try passed: nothing left to return to
    assert practice.practice(home, transport=FakeModel()).returns == []


@pytest.mark.usefixtures("world")
def test_the_practice_budget_caps_every_return(home, tmp_path):
    reel = tmp_path / "reel.mp4"
    reel.write_bytes(b"reel")
    _fail(home, FakeModel(), reel)
    _with_budget(home, 0.01)  # below one call's reserve: no return is started
    done = practice.practice(home, transport=FakeModel())
    assert done.returns == [] and done.stopped == "budget"
