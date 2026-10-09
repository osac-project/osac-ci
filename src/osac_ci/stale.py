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
from datetime import UTC, datetime


@dataclass(frozen=True)
class LastVerdict:
    """The newest ``OSAC CI`` check run on a PR's head commit."""

    status: str  # queued | in_progress | completed
    conclusion: str | None
    started_at: str
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
    "changed since the verdict": 2,
    "verdict stuck in progress": 3,
    "old": 4,
}


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)


def reason(standing: Standing, now: datetime, rules: StaleRules = StaleRules()) -> str | None:  # noqa: B008
    """Why this PR needs a new verdict, or ``None`` when the posted one can stand."""
    last = standing.last
    if last is None:
        return "no verdict"
    if last.status != "completed":
        started = _parse(last.started_at)
        return "verdict stuck in progress" if (now - started).total_seconds() > rules.stale_after else None
    if last.conclusion in ("failure", "timed_out", "cancelled"):
        return "planner error"
    posted = _parse(last.completed_at or last.started_at)
    if _parse(standing.updated_at) > posted:
        return "changed since the verdict"
    if rules.max_age and (now - posted).total_seconds() > rules.max_age:
        return "old"
    return None


def select_stale(
    standings: Sequence[Standing],
    now: datetime,
    rules: StaleRules = StaleRules(),  # noqa: B008
) -> list[tuple[Standing, str]]:
    """The PRs that need a new verdict with the reason, the most urgent first.

    Within a reason the order serves people first: PRs with no verdict yet (every PR, the first time a repository is
    covered) start with the most recently active ones. The others start with the one that has waited longest, so a busy
    sweep cannot starve the PR that has been stale the longest."""

    def key(item: tuple[Standing, str]) -> tuple[int, float, int]:
        standing, why = item
        updated = _parse(standing.updated_at).timestamp()
        return (_PRIORITY[why], -updated if why == "no verdict" else updated, standing.number)

    return sorted(((s, why) for s in standings if (why := reason(s, now, rules))), key=key)
