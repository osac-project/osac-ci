"""Which open pull requests need a new verdict, judged from what is already posted, without re-reading each PR.

A sweep that re-evaluates every PR costs about 7 to 11 requests each, almost all of it spent confirming a verdict that
is still right. The events (a push, a label, a finished workflow) say when a verdict changes; the sweep only exists to
repair the cases where an event was missed or its run failed. Those leave a recognizable trace on the PR, visible in a
single listing of all open PRs with their latest ``OSAC CI`` check run:

* there is no verdict on the head commit at all;
* the verdict is a failure, which for a pull request means the planner could not decide (``planner-error``);
* the PR changed after the verdict was posted (a label, a review, a comment, a push);
* the verdict still says "in progress" long after it was posted, so the event that ends it may have been missed;
* the verdict is simply old, a safety net for what no event reports (a team membership change, a CODEOWNERS change).

Everything else is skipped. This module is pure: the listing is fetched by ``osac_ci.github.standing``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class LastVerdict:
    """The newest ``OSAC CI`` check run on a PR's head commit."""

    status: str  # queued | in_progress | completed
    conclusion: str | None
    started_at: str | None  # a queued run has not started
    completed_at: str | None


@dataclass(frozen=True)
class Standing:
    number: int
    head_sha: str
    updated_at: str  # when the PR last changed (not when a check run was posted)
    last: LastVerdict | None


@dataclass(frozen=True)
class StaleRules:
    stale_after: int = 1800  # seconds an "in progress" verdict may stand before it is looked at again
    max_age: int = 21600  # seconds after which any verdict is refreshed (0 turns this off)


# Lower number = looked at first when the sweep cannot cover everyone.
_PRIORITY = {
    "no verdict": 0,
    "planner error": 1,
    "verdict without a time": 1,
    "changed since the verdict": 2,
    "verdict stuck in progress": 3,
    "old": 4,
}


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)


def _posted(last: LastVerdict) -> datetime | None:
    """When the newest verdict was posted, or ``None`` when the check run carries no time at all."""
    stamp = last.completed_at if last.status == "completed" and last.completed_at else last.started_at
    return _parse(stamp) if stamp else None


def reason(standing: Standing, now: datetime, rules: StaleRules = StaleRules()) -> str | None:  # noqa: B008
    """Why this PR needs a new verdict, or ``None`` when the posted one can stand."""
    last = standing.last
    if last is None:
        return "no verdict"
    posted = _posted(last)
    if posted is None:
        return "verdict without a time"  # cannot be compared with anything, so it cannot be trusted to be current
    if last.status != "completed":
        # An unfinished verdict still describes a PR that has changed since: that comes first, whatever its age.
        if _parse(standing.updated_at) > posted:
            return "changed since the verdict"
        return "verdict stuck in progress" if (now - posted).total_seconds() > rules.stale_after else None
    if last.conclusion in ("failure", "timed_out", "cancelled"):
        return "planner error"
    if _parse(standing.updated_at) > posted:
        return "changed since the verdict"
    if rules.max_age and (now - posted).total_seconds() > rules.max_age:
        return "old"
    return None


def stale_since(standing: Standing, why: str, rules: StaleRules = StaleRules()) -> datetime:  # noqa: B008
    """When the verdict became out of date: the order among stale PRs is how long they have been waiting."""
    updated = _parse(standing.updated_at)
    posted = _posted(standing.last) if standing.last else None
    if posted is None or why == "no verdict":
        return updated
    if why == "changed since the verdict":
        return updated
    if why == "verdict stuck in progress":
        return posted + timedelta(seconds=rules.stale_after)
    if why == "old":
        return posted + timedelta(seconds=rules.max_age)
    return posted  # planner error: it has been wrong since it was posted


def select_stale(
    standings: Sequence[Standing],
    now: datetime,
    rules: StaleRules = StaleRules(),  # noqa: B008
) -> list[tuple[Standing, str]]:
    """The PRs that need a new verdict with the reason, the most urgent first.

    Within a reason the order serves people first. PRs with no verdict yet (every PR, the first time a repository is
    covered) start with the most recently active ones. The others start with the one whose verdict went out of date
    longest ago, so a busy sweep cannot starve the PR that has been stale the longest."""

    def key(item: tuple[Standing, str]) -> tuple[int, float, int]:
        standing, why = item
        since = stale_since(standing, why, rules).timestamp()
        return (_PRIORITY[why], -since if why == "no verdict" else since, standing.number)

    return sorted(((s, why) for s in standings if (why := reason(s, now, rules))), key=key)
