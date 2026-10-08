"""Native approval: reviews from code owners, kept valid across rebases.

This replaces the ``lgtm`` / ``approved`` labels when the policy has an ``approval:`` section. The rules:

* Only human reviewers count. The latest review of each person decides: an approval stands until they request changes
  or it is dismissed. A review that only comments changes nothing. The PR author never approves their own PR.
* Anyone with outstanding "changes requested" blocks the PR until that review is dismissed or replaced.
* An approval is *fresh* when it was given on the current head commit. Otherwise it is *carried over* if the policy
  allows and the changes the PR makes are the same as on the commit that was approved (a rebase or a merge of the
  base branch). Anything else is *stale* and does not count: the reviewer has not seen this code.
* Every changed file that has code owners needs an approval from one of them; with no CODEOWNERS file, or for files no
  rule covers, only ``min_approvals`` applies. Team owners are expanded from ``Snapshot.team_members``; a team that
  could not be read fails closed with an error, never a silent pass.

A carried-over approval is safe because the merge queue still tests the PR on top of the current base before it
merges, so a rebase that changes behavior is caught there, not by a person re-approving an identical diff.
"""

from __future__ import annotations

from dataclasses import dataclass

from osac_ci.model import Review, Snapshot
from osac_ci.policy import Approval
from osac_ci.rules import codeowners

_STANCE = {"APPROVED": "approve", "CHANGES_REQUESTED": "changes", "DISMISSED": None}
_SHOWN_FILES = 3


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def _latest_stances(reviews: tuple[Review, ...], author: str) -> dict[str, Review]:
    """Latest approve / changes-requested review per human reviewer (a dismissed review clears their stance)."""
    ordered = sorted(reviews, key=lambda r: (r.submitted_at or "", r.id or 0))
    latest: dict[str, Review] = {}
    for review in ordered:
        if review.user_type != "User" or review.user.lower() == author.lower() or review.state not in _STANCE:
            continue
        if _STANCE[review.state] is None:
            latest.pop(review.user.lower(), None)
        else:
            latest[review.user.lower()] = review
    return latest


def _logins(snapshot: Snapshot, owner: str) -> set[str]:
    if owner.startswith("@") and "/" in owner:
        members = snapshot.team_members.get(owner[1:])
        if members is None:
            raise ValueError(f"cannot read the members of code owner team {owner}; check the credential's org scope")
        return {m.lower() for m in members}
    if owner.startswith("@"):
        return {owner[1:].lower()}
    return set()  # an e-mail address cannot be matched to a login


def _freshness(review: Review, snapshot: Snapshot, policy: Approval) -> str:
    """'fresh', 'carried' or 'stale'."""
    if review.commit_id == snapshot.head_sha:
        return "fresh"
    if policy.carry_over == "trivial-rebase" and review.commit_id:
        before = snapshot.change_fingerprints.get(review.commit_id)
        now = snapshot.change_fingerprints.get(snapshot.head_sha)
        if before is not None and before == now:
            return "carried"
    return "stale"


def evaluate(snapshot: Snapshot, policy: Approval) -> ApprovalDecision:
    stances = _latest_stances(snapshot.reviews, snapshot.author)
    problems: list[str] = []
    notes: list[str] = []

    blockers = sorted(r.user for r in stances.values() if _STANCE[r.state] == "changes")
    if blockers:
        problems.append(f"changes requested by {', '.join(blockers)}")

    stale: list[str] = []
    valid: dict[str, Review] = {}
    for login, review in stances.items():
        if _STANCE[review.state] != "approve":
            continue
        kind = _freshness(review, snapshot, policy)
        short = (review.commit_id or "")[:9]
        if kind == "stale":
            stale.append(
                f"approval from {review.user} is out of date: the PR's changes differ from what they approved ({short})"
            )
            continue
        valid[login] = review
        if kind == "carried":
            notes.append(f"approval from {review.user} carried over from {short}: same changes after a rebase")

    if len(valid) < policy.min_approvals:
        problems.append(f"needs {policy.min_approvals} approving review(s) from someone else, has {len(valid)}")

    if policy.require_code_owners and snapshot.codeowners is not None:
        rules = codeowners.parse(snapshot.codeowners)
        uncovered: dict[tuple[str, ...], list[str]] = {}
        for path in snapshot.changed_files:
            owners = codeowners.owners_of(rules, path)
            if owners is None:
                continue
            allowed = set().union(*(_logins(snapshot, o) for o in owners))
            if not (allowed & valid.keys()):
                uncovered.setdefault(owners, []).append(path)
        for owners, paths in uncovered.items():
            shown = ", ".join(paths[:_SHOWN_FILES]) + (" ..." if len(paths) > _SHOWN_FILES else "")
            problems.append(f"needs an approval from a code owner ({', '.join(owners)}) for: {shown}")

    # An out-of-date approval only blocks when it is part of why the requirements are not met.
    if problems:
        return ApprovalDecision(False, tuple(stale + problems), tuple(notes))
    return ApprovalDecision(True, (), tuple(notes + stale))
