"""Label rules.

Mirrors two legacy sources, which intentionally differ (lifecycle caveat 6: a green `check-labels` alone does not
mean queue eligibility):

* osac `.github/scripts/auto-queue.sh` ``labels_ok``: three required labels, four blocking labels.
* osac `.github/workflows/label-gate.yml` (``check-labels``): the same three required labels, two blocking labels.
"""

from __future__ import annotations

from collections.abc import Collection

REQUIRED_LABELS: tuple[str, ...] = ("lgtm", "approved", "jira/valid-reference")
QUEUE_BLOCKING_LABELS: tuple[str, ...] = (
    "do-not-merge/hold",
    "do-not-merge/work-in-progress",
    "do-not-merge/invalid-owners-file",
    "needs-rebase",
)
GATE_BLOCKING_LABELS: tuple[str, ...] = ("do-not-merge/hold", "do-not-merge/work-in-progress")


def missing_required(labels: Collection[str], required: Collection[str] = REQUIRED_LABELS) -> tuple[str, ...]:
    return tuple(label for label in required if label not in labels)


def present_blocking(labels: Collection[str], blocking: Collection[str] = QUEUE_BLOCKING_LABELS) -> tuple[str, ...]:
    return tuple(label for label in blocking if label in labels)


def queue_labels_ok(
    labels: Collection[str],
    required: Collection[str] = REQUIRED_LABELS,
    blocking: Collection[str] = QUEUE_BLOCKING_LABELS,
) -> bool:
    """Port of ``labels_ok`` in auto-queue.sh."""
    return not missing_required(labels, required) and not present_blocking(labels, blocking)
