from datetime import datetime, timedelta, timezone

import pytest

from mrstat.effort import CommitEvent, EffortParams, attribute_hours

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
P = EffortParams(max_gap_minutes=120, first_commit_minutes=60, max_hours_per_day=10, timezone="UTC")


def ev(sha, minutes, author="a"):
    return CommitEvent(sha, author, T0 + timedelta(minutes=minutes))


def test_session_gaps_and_first_commit_bonus():
    h = attribute_hours([ev("1", 0), ev("2", 30), ev("3", 90), ev("4", 400)], P)
    assert h["1"] == pytest.approx(1.0)  # first of session
    assert h["2"] == pytest.approx(0.5)
    assert h["3"] == pytest.approx(1.0)
    assert h["4"] == pytest.approx(1.0)  # gap > 2h -> new session


def test_authors_are_independent():
    h = attribute_hours([ev("a1", 0, "a"), ev("b1", 10, "b"), ev("a2", 20, "a")], P)
    assert h["a2"] == pytest.approx(20 / 60)
    assert h["b1"] == pytest.approx(1.0)


def test_daily_cap_scales_down():
    # 12 commits every 60 minutes = 1 + 11 = 12h on one day -> capped to 10h
    events = [ev(str(i), 60 * i) for i in range(12)]
    h = attribute_hours(events, P)
    assert sum(h.values()) == pytest.approx(10.0)


def test_duplicate_sha_counted_once_and_cherry_pick_is_free():
    h = attribute_hours([ev("x", 0), ev("x", 0), ev("y", 0)], P)
    assert len(h) == 2
    assert sorted(h.values()) == pytest.approx([0.0, 1.0])
