"""E2E unlock policy: which signal lets an expensive job start.

Today's rules (rules/readiness.py, a port of the osac-test-infra bash) are one hard-coded ladder over labels and
reviews, including a sticky ``lgtm`` that keeps E2E unlocked after the code has changed. This module is the same
decision as an explicit, per-suite policy (``e2e.unlock`` in the policy file):

* ``human-approval``      a human has approved the current changes (fresh, or carried over a rebase by the approval
                          rules) and the planner's approval policy accepts that reviewer;
* ``coderabbit-approval`` CodeRabbit's latest decision is APPROVED on exactly the head commit. It re-reviews every
                          push, so an approval of an older commit never counts;
* ``lgtm-label``         the ``lgtm`` label is on the PR *now* (a transition aid while approvals still use labels);
* ``e2e-ready-label``    the ``e2e-ready`` label, applied by the trusted workflow actor.

Any one signal unlocks. An outstanding human "changes requested" blocks all of them (``block_on_changes_requested``).
There is no sticky signal: what unlocks E2E is about the commit that is going to be tested.
"""

from __future__ import annotations

from collections.abc import Collection

from osac_ci.model import Snapshot
from osac_ci.policy import Approval, E2EUnlock
from osac_ci.rules import approval, readiness
from osac_ci.rules.readiness import Readiness

_WORDS = {
    "human-approval": "a human approval of the current changes",
    "coderabbit-approval": "a CodeRabbit approval on the current commit",
    "lgtm-label": "the lgtm label",
    "e2e-ready-label": "the e2e-ready label (applied by the trusted workflow)",
}


def _coderabbit_on_head(snapshot: Snapshot) -> bool:
    """CodeRabbit's latest decision is APPROVED on exactly the head. Whether a human blocks it is the policy's call."""
    latest = readiness.coderabbit_latest(snapshot.reviews)
    return (
        bool(snapshot.head_sha)
        and latest is not None
        and latest.state == "APPROVED"
        and latest.commit_id == snapshot.head_sha
    )


def describe(signals: Collection[str]) -> str:
    return " or ".join(_WORDS[s] for s in signals)


def decide(
    snapshot: Snapshot,
    signals: Collection[str],
    unlock: E2EUnlock,
    approval_policy: Approval | None,
) -> Readiness:
    if unlock.block_on_changes_requested and readiness.human_has_changes_requested(snapshot.reviews):
        return Readiness(False, "waiting: human CHANGES_REQUESTED still open")

    found = approval.approvers(snapshot, approval_policy or Approval()) if "human-approval" in signals else None
    denied = ""
    for signal in signals:
        if signal == "human-approval" and found and found.valid:
            names = ", ".join(sorted(r.user for r in found.valid.values()))
            return Readiness(True, f"allowed: human approval from {names}")
        if signal == "coderabbit-approval" and _coderabbit_on_head(snapshot):
            return Readiness(True, f"allowed: APPROVED review on head from {readiness.CODERABBIT_LOGIN}")
        if signal == "lgtm-label" and "lgtm" in snapshot.labels:
            return Readiness(True, "allowed: lgtm label present")
        if signal == "e2e-ready-label" and "e2e-ready" in snapshot.labels:
            if readiness.e2e_ready_applied_by_trusted_actor(snapshot.label_events):
                return Readiness(True, "allowed: e2e-ready label present (applied by trusted actor)")
            denied = "e2e-ready label present but applied by untrusted actor"

    why = [f"needs {describe(signals)}"]
    if found and found.stale:
        why.append(found.stale[0])
    if "coderabbit-approval" in signals:
        latest = readiness.coderabbit_latest(snapshot.reviews)
        if latest and latest.state == "APPROVED" and latest.commit_id and latest.commit_id != snapshot.head_sha:
            why.append(f"CodeRabbit approved an older commit {latest.commit_id[:7]}")
    if denied:
        why.append(denied)
    return Readiness(False, "waiting: " + "; ".join(why))
