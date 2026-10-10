"""Locks: conditions that must hold before a job may start.

A job names the locks it needs (or takes the policy's defaults); each lock is open when any one of its signals holds,
and the job is open when every lock is. The planner uses this to report a job as *locked* instead of failed or passed.

Two kinds of signal, answering two different questions:

* who is asking: ``org-member`` (the author, or the owner of the fork, is a member, or the pull request is from a branch
  of the repository itself), ``trusted-bot`` and ``authorized-commit`` (a member authorized exactly this commit, or put
  the ``ok-to-test`` label on it, depending on ``trust.authorization``). Together they are ``fork_secrets_authorized``;
* has anyone looked at it: ``human-approval``, ``coderabbit-approval``, ``lgtm-label``, ``e2e-ready-label`` (see
  rules/e2e_unlock.py), and ``legacy-readiness``, which is today's osac-test-infra ladder as it is, sticky ``lgtm``
  included. A lock that uses it describes the current behavior exactly.

This module does no I/O: every fact it needs is in the snapshot.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass

from osac_ci.model import Snapshot
from osac_ci.policy import MEMBERSHIP_SIGNALS, E2EUnlock, Job, Policy
from osac_ci.rules import e2e_unlock, readiness
from osac_ci.rules.fork import OK_TO_TEST

CODE_MEMBERSHIP = "membership"  # the code of a job held back by a lock about who is asking
AUTH_DETAIL = "fork PR is not authorized to use secrets"

_WORDS = {
    "org-member": "an author in the organization",
    "trusted-bot": "a trusted bot as the author",
    "authorized-commit": "a member's authorization of this commit",
    "legacy-readiness": "an lgtm, a trusted e2e-ready label, or a CodeRabbit approval on the current commit",
}


@dataclass(frozen=True)
class LockDecision:
    """The first closed lock of a job."""

    lock: str
    reason: str
    code: str  # CODE_MEMBERSHIP, or the code of the readiness decision (changes-requested, needs-signal)
    signals: tuple[str, ...]


def describe(signals: Collection[str]) -> str:
    """What opens a lock, in words. Alternatives are joined with "or"."""
    return " or ".join(_WORDS.get(s) or e2e_unlock.describe([s]) for s in signals)


def signals_of(policy: Policy, job: Job, lock: str) -> tuple[str, ...]:
    """The signals that open ``lock`` for ``job``: the job's own override, else the lock's."""
    return tuple(job.lock_overrides.get(lock) or policy.locks[lock].open_when)


def _member_signal_holds(snapshot: Snapshot, policy: Policy, signal: str) -> bool:
    if signal == "org-member":
        # A pull request from a branch of the repository itself is trusted: pushing there needs write access.
        return (
            not snapshot.is_fork
            or snapshot.author_is_org_member
            or bool(
                snapshot.fork_owner and snapshot.fork_owner != snapshot.author and snapshot.fork_owner_is_org_member
            )
        )
    if signal == "trusted-bot":
        return snapshot.author in policy.trust.trusted_bots
    # authorized-commit
    if policy.trust.authorization == "sha-bound":
        return bool(snapshot.authorized_by)
    return OK_TO_TEST in snapshot.labels


def _is_open(
    snapshot: Snapshot,
    policy: Policy,
    signals: tuple[str, ...],
    block_on_changes_requested: bool,
    labels: frozenset[str],
) -> tuple[bool, str, str]:
    """``(open, reason, code)`` for one lock. The reason and code describe what is missing when it is closed."""
    member = [s for s in signals if s in MEMBERSHIP_SIGNALS]
    other = [s for s in signals if s not in MEMBERSHIP_SIGNALS]
    if any(_member_signal_holds(snapshot, policy, s) for s in member):
        return True, "", ""
    if other == ["legacy-readiness"]:
        decision = readiness.decide(labels, snapshot.reviews, snapshot.head_sha, snapshot.label_events)
        return decision.allowed, decision.reason, decision.code
    if other:
        config = E2EUnlock.model_validate(
            {"mode": "policy", "any_of": other, "block_on_changes_requested": block_on_changes_requested}
        )
        decision = e2e_unlock.decide(snapshot, other, config, policy.approval)
        if decision.allowed:
            return True, "", ""
        reason = decision.reason if not member else f"{decision.reason}, or {describe(member)}"
        return False, reason, decision.code
    return False, AUTH_DETAIL, CODE_MEMBERSHIP


def evaluate(snapshot: Snapshot, policy: Policy, job: Job, labels: frozenset[str]) -> LockDecision | None:
    """The first closed lock of ``job`` (in the order the job lists them), or ``None`` when every lock is open.

    ``labels`` are the labels as the planner sees them, which include ``lgtm`` when native approval is satisfied."""
    for name in policy.effective_locks(job):
        signals = signals_of(policy, job, name)
        opened, reason, code = _is_open(
            snapshot, policy, signals, policy.locks[name].block_on_changes_requested, labels
        )
        if not opened:
            return LockDecision(name, reason, code, signals)
    return None
