"""Protected paths: files whose change needs an approval from named people.

Pull request code can change the workflows, filters and scripts that produce the checks the same pull request is judged
by. A check that reports success then proves little, and the only reliable control is a person who is trusted for those
files reading the change. The policy (not the pull request) names the files and the people. An approval counts when it
covers the current changes: given on this commit, or carried over a rebase when the policy allows it and the changes are
the same. The author never approves their own pull request.
"""

from __future__ import annotations

from dataclasses import dataclass

from osac_ci.model import Snapshot
from osac_ci.paths import any_match
from osac_ci.policy import Approval, ProtectedPaths
from osac_ci.rules import approval

_SHOWN_FILES = 3


@dataclass(frozen=True)
class ProtectedDecision:
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    approvers: tuple[str, ...] = ()  # who could unlock, for the next-step text


def matched_files(snapshot: Snapshot, rule: ProtectedPaths) -> list[str]:
    return [path for path in snapshot.changed_files if any_match(rule.paths, path)]


def evaluate(snapshot: Snapshot, rules: tuple[ProtectedPaths, ...]) -> ProtectedDecision:
    """Problems for every rule whose files changed and that none of its approvers has approved."""
    problems: list[str] = []
    notes: list[str] = []
    who: list[str] = []
    for rule in rules:
        files = matched_files(snapshot, rule)
        if not files:
            continue
        policy = Approval(min_approvals=1, require_code_owners=False, carry_over=rule.carry_over)
        found = approval.approvers(snapshot, policy)
        allowed = set().union(*(approval.logins_of(snapshot, owner) for owner in rule.approvers))
        if allowed & found.valid.keys():
            notes += found.notes
            continue
        shown = ", ".join(files[:_SHOWN_FILES]) + (" ..." if len(files) > _SHOWN_FILES else "")
        problems += found.stale
        problems.append(f"changes protected files ({shown}): needs an approval from {', '.join(rule.approvers)}")
        who += [a for a in rule.approvers if a not in who]
    return ProtectedDecision(tuple(dict.fromkeys(problems)), tuple(dict.fromkeys(notes)), tuple(who))
