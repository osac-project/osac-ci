import pytest
from helpers import OK_LABELS

from osac_ci.model import Snapshot
from osac_ci.rules.fork import fork_secrets_authorized
from osac_ci.rules.labels import (
    GATE_BLOCKING_LABELS,
    QUEUE_BLOCKING_LABELS,
    REQUIRED_LABELS,
    missing_required,
    present_blocking,
    queue_labels_ok,
)

pytestmark = pytest.mark.unit


def test_required_labels_complete_is_ok() -> None:
    assert queue_labels_ok(OK_LABELS)


@pytest.mark.parametrize("label", REQUIRED_LABELS)
def test_each_required_label_is_needed(label: str) -> None:
    labels = OK_LABELS - {label}
    assert not queue_labels_ok(labels)
    assert missing_required(labels) == (label,)


@pytest.mark.parametrize("label", QUEUE_BLOCKING_LABELS)
def test_each_blocking_label_blocks(label: str) -> None:
    assert not queue_labels_ok(OK_LABELS | {label})
    assert present_blocking(OK_LABELS | {label}) == (label,)


def test_label_gate_and_queue_blockers_differ() -> None:
    # Lifecycle caveat 6: check-labels ignores two blockers that auto-queue enforces.
    assert set(GATE_BLOCKING_LABELS) < set(QUEUE_BLOCKING_LABELS)


def _fork(**kw: object) -> Snapshot:
    return Snapshot(repo="o/r", number=1, head_sha="x", is_fork=True, author="alice", **kw)  # type: ignore[arg-type]


def test_non_fork_is_always_authorized() -> None:
    assert fork_secrets_authorized(Snapshot(repo="o/r", number=1, head_sha="x"))


def test_fork_unauthorized_by_default() -> None:
    assert not fork_secrets_authorized(_fork())


def test_fork_authorized_by_ok_to_test_label() -> None:
    assert fork_secrets_authorized(_fork(labels=frozenset({"ok-to-test"})))


def test_fork_authorized_by_author_membership() -> None:
    assert fork_secrets_authorized(_fork(author_is_org_member=True))


def test_fork_authorized_when_fork_owner_is_member_and_differs_from_author() -> None:
    assert fork_secrets_authorized(_fork(fork_owner="osac-dev-bot", fork_owner_is_org_member=True))


def test_fork_owner_same_as_author_does_not_double_count() -> None:
    assert not fork_secrets_authorized(_fork(fork_owner="alice", fork_owner_is_org_member=True))
