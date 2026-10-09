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


# ---- verdicts with missing times ---------------------------------------------------------------------------------


def test_a_queued_verdict_that_never_started_is_looked_at_not_parsed() -> None:
    queued = LastVerdict("queued", None, None, None)
    assert reason(pr(updated=600, last=queued), NOW) == "verdict without a time"


def test_a_completed_verdict_with_no_time_at_all_cannot_be_trusted() -> None:
    assert (
        reason(pr(updated=600, last=LastVerdict("completed", "success", None, None)), NOW) == "verdict without a time"
    )


def test_a_completed_verdict_without_a_completion_time_uses_its_start() -> None:
    last = LastVerdict("completed", "success", at(10), None)
    assert reason(pr(updated=15, last=last), NOW) is None
    assert reason(pr(updated=5, last=last), NOW) == "changed since the verdict"


# ---- an unfinished verdict of a PR that has changed ----------------------------------------------------------------


def test_a_change_after_an_unfinished_verdict_comes_before_waiting_for_it() -> None:
    waiting = LastVerdict("in_progress", None, at(10), None)
    assert reason(pr(updated=5, last=waiting), NOW) == "changed since the verdict"  # young, but already out of date
    assert reason(pr(updated=15, last=waiting), NOW) is None  # young and current: left alone
    stuck = LastVerdict("in_progress", None, at(90), None)
    assert reason(pr(updated=60, last=stuck), NOW) == "changed since the verdict"
    assert reason(pr(updated=120, last=stuck), NOW) == "verdict stuck in progress"


# ---- the order among stale PRs: since when has the verdict been out of date -------------------------------------


def order(rows: list[Standing], rules: StaleRules | None = None) -> list[int]:
    rules = rules or StaleRules()
    return [s.number for s, _ in select_stale(rows, NOW, rules)]


def test_changed_verdicts_go_by_when_the_pr_changed_not_when_it_was_last_active_in_general() -> None:
    rows = [pr(1, updated=5, last=done(60)), pr(2, updated=20, last=done(60))]  # both changed after their verdict
    assert order(rows) == [2, 1]  # #2 changed longer ago


def test_old_verdicts_go_by_when_they_were_posted() -> None:
    rows = [pr(1, updated=60 * 20, last=done(60 * 8)), pr(2, updated=60 * 20, last=done(60 * 12))]
    assert order(rows) == [2, 1]


def test_planner_errors_go_by_when_they_were_posted() -> None:
    rows = [pr(1, updated=600, last=done(10, "failure")), pr(2, updated=600, last=done(40, "failure"))]
    assert order(rows) == [2, 1]


def test_stuck_verdicts_go_by_when_they_became_stuck() -> None:
    stuck_a = LastVerdict("in_progress", None, at(50), None)
    stuck_b = LastVerdict("in_progress", None, at(120), None)
    assert order([pr(1, updated=600, last=stuck_a), pr(2, updated=600, last=stuck_b)]) == [2, 1]


def test_equal_times_fall_back_to_the_pr_number() -> None:
    rows = [pr(7, updated=600, last=done(10, "failure")), pr(3, updated=600, last=done(10, "failure"))]
    assert order(rows) == [3, 7]


def test_stale_since_names_the_moment_for_each_reason() -> None:
    from osac_ci.stale import stale_since

    rules = StaleRules(stale_after=1800, max_age=3600)
    assert stale_since(pr(updated=5, last=done(60)), "changed since the verdict", rules) == NOW - timedelta(minutes=5)
    assert stale_since(pr(updated=900, last=done(60)), "old", rules) == NOW - timedelta(minutes=60) + timedelta(hours=1)
    stuck = LastVerdict("in_progress", None, at(50), None)
    assert stale_since(pr(updated=900, last=stuck), "verdict stuck in progress", rules) == NOW - timedelta(minutes=20)
    assert stale_since(pr(updated=900, last=done(30, "failure")), "planner error", rules) == NOW - timedelta(minutes=30)
    assert stale_since(pr(updated=3, last=None), "no verdict", rules) == NOW - timedelta(minutes=3)


def test_a_completed_verdict_counts_from_when_it_finished_not_when_it_started() -> None:
    long_run = LastVerdict("completed", "success", at(30), at(5))
    assert reason(pr(updated=10, last=long_run), NOW) is None  # the PR changed while the verdict was being computed
    assert reason(pr(updated=2, last=long_run), NOW) == "changed since the verdict"
