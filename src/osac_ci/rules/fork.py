"""Fork-PR secret authorization.

A pull request from a fork may use secrets, and start expensive jobs, when any of these holds:

1. the author is in the policy's ``trusted_bots``;
2. the author is an org member;
3. the fork owner differs from the author and the fork owner is an org member (GitHub Apps cannot be org members
   but may push to a fork owned by one);
4. an org member authorized it, in one of two modes (``trust.authorization``):

   * ``label`` (default, today's behavior, mirrors osac-test-infra ``authorize-fork-pr``): the ``ok-to-test`` label.
     The label survives pushes until a workflow strips it, so a commit pushed in that window is trusted too.
   * ``sha-bound``: an org member commented ``/ok-to-test <sha>`` and the adapter found the authorization check run
     on *exactly the current head commit*. A new push has a new SHA and no such check, so it is unauthorized at once:
     there is nothing to strip and no window. Labels are ignored in this mode.

Same-repository PRs are trusted: pushing a branch there already needs write access. The membership facts are
resolved by the adapter (they need an org-scoped credential), so this stays pure.
"""

from __future__ import annotations

from osac_ci.model import Snapshot
from osac_ci.policy import Trust

OK_TO_TEST = "ok-to-test"
COMMAND = "/ok-to-test"


def fork_secrets_authorized(snapshot: Snapshot, trust: Trust = Trust()) -> bool:  # noqa: B008 - Trust is frozen
    if not snapshot.is_fork:
        return True
    if snapshot.author in trust.trusted_bots:
        return True
    if snapshot.author_is_org_member:
        return True
    if snapshot.fork_owner and snapshot.fork_owner != snapshot.author and snapshot.fork_owner_is_org_member:
        return True
    if trust.authorization == "sha-bound":
        return bool(snapshot.authorized_by)
    return OK_TO_TEST in snapshot.labels


def authorization_command(head_sha: str) -> str:
    """The exact comment an org member posts to authorize this commit."""
    return f"{COMMAND} {head_sha[:7]}"
