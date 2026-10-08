"""Fork-PR secret authorization.

Mirrors osac-test-infra `.github/actions/authorize-fork-pr/action.yml`, which today runs inside six execution
workflows and needs a Vault-issued org token. A fork PR may use secrets when any of these holds:

1. the PR carries the ``ok-to-test`` label;
2. the PR author is an org member;
3. the fork owner differs from the author and the fork owner is an org member (GitHub Apps cannot be org members
   but may push to a fork owned by one).

The membership facts are resolved by the adapter (they need an org-scoped credential), so this stays pure. That
credential is a new requirement for the control plane (see the design doc, open questions).
"""

from __future__ import annotations

from osac_ci.model import Snapshot

OK_TO_TEST = "ok-to-test"


def fork_secrets_authorized(snapshot: Snapshot) -> bool:
    if not snapshot.is_fork:
        return True
    if OK_TO_TEST in snapshot.labels:
        return True
    if snapshot.author_is_org_member:
        return True
    return bool(snapshot.fork_owner) and snapshot.fork_owner != snapshot.author and snapshot.fork_owner_is_org_member
