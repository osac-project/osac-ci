"""Properties of the stale selection, for any times and any verdict, including missing ones."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from osac_ci.stale import LastVerdict, StaleRules, Standing, reason, select_stale

pytestmark = pytest.mark.property
settings.register_profile("stale", max_examples=200, deadline=None, suppress_health_check=list(HealthCheck))
settings.load_profile("stale")

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
REASONS = {
    "no verdict",
    "planner error",
    "verdict without a time",
    "changed since the verdict",
    "verdict stuck in progress",
    "old",
}


def stamp(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


minutes = st.integers(0, 60 * 24 * 3)
optional_stamp = st.one_of(st.none(), minutes.map(stamp))


@st.composite
def verdicts(draw: st.DrawFn) -> LastVerdict | None:
    if draw(st.booleans()) and draw(st.booleans()):
        return None
    return LastVerdict(
        draw(st.sampled_from(["queued", "in_progress", "completed"])),
        draw(st.sampled_from([None, "success", "failure", "action_required", "timed_out", "cancelled", "neutral"])),
        draw(optional_stamp),
        draw(optional_stamp),
    )


@st.composite
def standings(draw: st.DrawFn, number: int = 1) -> Standing:
    return Standing(number, "a" * 40, stamp(draw(minutes)), draw(verdicts()))


rules = st.builds(StaleRules, st.integers(0, 7200), st.integers(0, 60 * 24 * 60))


@given(standings(), rules)
def test_the_decision_never_raises_and_gives_a_known_reason_or_none(s: Standing, r: StaleRules) -> None:
    assert reason(s, NOW, r) in REASONS | {None}


@given(
    st.lists(st.integers(1, 40), unique=True, max_size=12).flatmap(
        lambda numbers: st.tuples(*[standings(n) for n in numbers]) if numbers else st.just(())
    ),
    rules,
)
def test_the_selection_is_exactly_the_stale_ones_in_a_stable_order(rows: tuple[Standing, ...], r: StaleRules) -> None:
    chosen = select_stale(list(rows), NOW, r)
    assert {s.number for s, _ in chosen} == {s.number for s in rows if reason(s, NOW, r) is not None}
    assert [(s.number, why) for s, why in chosen] == [
        (s.number, why) for s, why in select_stale(list(reversed(rows)), NOW, r)
    ]
    assert len({s.number for s, _ in chosen}) == len(chosen)


@given(standings(), rules)
def test_a_pr_changed_after_its_completed_verdict_is_always_selected(s: Standing, r: StaleRules) -> None:
    last = s.last
    if last is None or last.status != "completed" or last.conclusion is None:
        return
    posted = last.completed_at or last.started_at
    if posted is None:
        return
    later = Standing(s.number, s.head_sha, (NOW + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"), last)
    assert reason(later, NOW + timedelta(seconds=1), r) is not None


@given(minutes, rules)
def test_a_fresh_untouched_success_is_never_selected(age: int, r: StaleRules) -> None:
    posted = stamp(age)
    s = Standing(1, "a" * 40, stamp(age + 5), LastVerdict("completed", "success", posted, posted))
    expected = "old" if r.max_age and age * 60 > r.max_age else None
    assert reason(s, NOW, r) == expected


@given(st.lists(st.integers(0, 3), min_size=2, max_size=10))
def test_reasons_come_out_in_priority_order(kinds: list[int]) -> None:
    makers = [
        lambda i: Standing(i, "a" * 40, stamp(50), None),
        lambda i: Standing(i, "a" * 40, stamp(900), LastVerdict("completed", "failure", stamp(30), stamp(30))),
        lambda i: Standing(i, "a" * 40, stamp(5), LastVerdict("completed", "success", stamp(30), stamp(30))),
        lambda i: Standing(
            i, "a" * 40, stamp(900), LastVerdict("completed", "success", stamp(60 * 10), stamp(60 * 10))
        ),
    ]
    rows = [makers[k](i + 1) for i, k in enumerate(kinds)]
    why = [w for _, w in select_stale(rows, NOW)]
    rank = {"no verdict": 0, "planner error": 1, "changed since the verdict": 2, "old": 4}
    assert [rank[w] for w in why] == sorted(rank[w] for w in why)
