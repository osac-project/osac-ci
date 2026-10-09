"""Rebuild how a pull request stood at an earlier moment, from the timestamps its data already carries.

A replay that reads a merged PR's final state can only say the planner would not block it *now*. To judge the decision
that was actually made (enqueuing, or merging directly) it needs the PR as it was then:

* labels: replay the ``labeled`` / ``unlabeled`` events up to the moment;
* reviews: only those submitted by then (a dismissal is a review of its own, so it counts from its own time);
* check runs: one that started after the moment did not exist yet, and one that finished after it was still running.

The head commit needs no reconstruction for a queue-merged PR: a push removes a PR from the queue, so the head when it
was enqueued is its final head. The changed files are the final ones for the same reason. Timestamps are ISO 8601 in UTC
with a trailing ``Z`` as GitHub returns them, so comparing them as strings orders them.
"""

from __future__ import annotations

from dataclasses import replace

from osac_ci.github.snapshot import authorization_external_id
from osac_ci.model import CheckRun, LabelEvent, Review, Snapshot


def _labels_at(events: tuple[LabelEvent, ...]) -> frozenset[str]:
    labels: set[str] = set()
    for event in events:
        if event.event == "labeled":
            labels.add(event.label)
        elif event.event == "unlabeled":
            labels.discard(event.label)
    return frozenset(labels)


def _check_at(run: CheckRun, moment: str) -> CheckRun | None:
    if run.started_at and run.started_at > moment:
        return None
    if run.status == "completed" and run.completed_at and run.completed_at > moment:
        return replace(run, status="in_progress", conclusion=None, completed_at=None)
    return run


def _review_known(review: Review, moment: str) -> bool:
    return not review.submitted_at or review.submitted_at <= moment


def _authorized_at(snapshot: Snapshot, runs: tuple[CheckRun, ...]) -> str:
    """An authorization is a check run, so it exists only from the moment that run completed. ``authorized_by`` was
    derived from the final check runs: keep it only while the run it came from is still a completed success among
    the ones that stood at the moment. (Whether the authorizer was an org member then is not rebuilt: it needs the
    organization's history, which the API does not give.)"""
    if not snapshot.authorized_by:
        return ""
    wanted = authorization_external_id(snapshot.authorized_by, snapshot.head_sha)
    done = any(r.external_id == wanted and r.status == "completed" and r.conclusion == "success" for r in runs)
    return snapshot.authorized_by if done else ""


def state_at(snapshot: Snapshot, moment: str) -> Snapshot:
    """The snapshot as it stood at ``moment``. Items without a timestamp are kept: they cannot be placed in time."""
    events = tuple(sorted((e for e in snapshot.label_events if not e.at or e.at <= moment), key=lambda e: e.at))
    runs = tuple(r for run in snapshot.check_runs if (r := _check_at(run, moment)) is not None)
    return replace(
        snapshot,
        labels=_labels_at(events),
        label_events=events,
        reviews=tuple(r for r in snapshot.reviews if _review_known(r, moment)),
        check_runs=runs,
        authorized_by=_authorized_at(snapshot, runs),
        in_merge_queue=False,  # the question is whether it could be let in, so it is not in yet
    )
