"""Which pull requests need a new verdict, from what is already posted."""

from datetime import UTC, datetime, timedelta

import pytest

from osac_ci.stale import LastVerdict, StaleRules, Standing, reason, select_stale

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)


def at(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pr(number: int = 1, *, updated: float = 600, last: LastVerdict | None = None) -> Standing:
    return Standing(number, "a" * 40, at(updated), last)


def done(posted: float, conclusion: str = "success") -> LastVerdict:
    return LastVerdict("completed", conclusion, at(posted), at(posted))


def test_a_pr_without_any_verdict_needs_one() -> None:
    assert reason(pr(), NOW) == "no verdict"


def test_a_fresh_verdict_stands() -> None:
    assert reason(pr(updated=60, last=done(30)), NOW) is None


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "cancelled"])
def test_a_failed_verdict_is_a_planner_error_to_retry(conclusion: str) -> None:
    assert reason(pr(updated=600, last=done(10, conclusion)), NOW) == "planner error"


@pytest.mark.parametrize("conclusion", ["success", "action_required", "neutral"])
def test_a_deliberate_verdict_is_not_retried(conclusion: str) -> None:
    assert reason(pr(updated=600, last=done(10, conclusion)), NOW) is None


def test_a_change_after_the_verdict_makes_it_stale_and_one_before_does_not() -> None:
    assert reason(pr(updated=15, last=done(10)), NOW) is None  # the PR changed 15 minutes ago, the verdict 10
    assert reason(pr(updated=10, last=done(10)), NOW) is None  # the same second is not a change
    assert reason(pr(updated=5, last=done(10)), NOW) == "changed since the verdict"


def test_an_in_progress_verdict_is_left_alone_until_it_is_old() -> None:
    waiting = LastVerdict("in_progress", None, at(10), None)
    assert reason(pr(updated=600, last=waiting), NOW) is None
    stuck = LastVerdict("in_progress", None, at(31), None)
    assert reason(pr(updated=600, last=stuck), NOW) == "verdict stuck in progress"
    assert reason(pr(updated=600, last=stuck), NOW, StaleRules(stale_after=7200)) is None


def test_an_old_verdict_is_refreshed_unless_that_is_turned_off() -> None:
    old = done(60 * 7)
    assert reason(pr(updated=60 * 20, last=old), NOW) == "old"
    assert reason(pr(updated=60 * 20, last=old), NOW, StaleRules(max_age=0)) is None
    assert reason(pr(updated=60 * 20, last=done(60 * 5)), NOW) is None


def test_the_most_urgent_come_first_then_the_most_active_or_the_longest_waiting() -> None:
    rows = [
        pr(1, updated=60 * 8, last=done(60 * 7)),  # old
        pr(2, updated=5, last=done(10)),  # changed
        pr(3, updated=400, last=None),  # no verdict, active less recently than #4
        pr(4, updated=100, last=None),
        pr(5, updated=300, last=done(10, "failure")),  # planner error
        pr(6, updated=600, last=done(10)),  # fine
    ]
    order = [(s.number, why) for s, why in select_stale(rows, NOW)]
    assert order == [
        (4, "no verdict"),  # the more recently active first
        (3, "no verdict"),
        (5, "planner error"),
        (2, "changed since the verdict"),
        (1, "old"),
    ]


def test_timestamps_with_an_offset_compare_correctly() -> None:
    standing = Standing(
        1,
        "a" * 40,
        "2026-10-09T14:00:00+02:00",
        LastVerdict("completed", "success", "2026-10-09T11:00:00Z", "2026-10-09T11:00:00Z"),
    )
    assert reason(standing, NOW) == "changed since the verdict"  # 12:00Z is after 11:00Z


def test_among_stale_verdicts_the_longest_waiting_goes_first() -> None:
    rows = [pr(1, updated=5, last=done(10)), pr(2, updated=8, last=done(10))]
    assert [s.number for s, _ in select_stale(rows, NOW)] == [2, 1]
