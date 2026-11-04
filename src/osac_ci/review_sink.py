"""Take the review sink off a pull request once it has an approval that counts.

Some repositories give GitHub's review assignment for a team (CODEOWNERS sends a path to the team) one account to
assign, so that nobody is picked and the team is not notified (github-config: "maintainers" and ``osac-ci-bot``). The
account approves nothing: its request stays pending on every pull request, with a yellow dot, for no one to answer.
This removes the request when the work it stood in for is done: someone else, a person who may approve or CodeRabbit,
has approved the current commit (the same approval that turns auto-merge on). The one case that needs nothing is the
sink itself approving: GitHub clears a request when the person asked reviews.

Only the request of that one account goes, and only when it is still requested; the team's request, the other people's
requests and every review stay. GitHub notifies nobody when a request is removed. A new commit that asks for a review
again may assign the sink again, and the next approval takes it off again.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from osac_ci.e2e_check import check_sha
from osac_ci.e2e_start import approvals_of, reviews_with_roles
from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get, paginate

LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


def clear(client: GitHubClient, repo: str, number: int, sink: str) -> str:
    """Remove the sink's review request from the pull request if a person has approved its head commit. Returns what was
    done, in a line."""
    repo = check_repo(repo)
    if not LOGIN.fullmatch(sink):
        raise ValueError("the sink must be a login")
    pull = get(client, f"/repos/{repo}/pulls/{number}")
    if pull.get("state") != "open":
        return "the pull request is not open: nothing to do"
    # GitHub logins and repository names are case-insensitive: the account is found as GitHub spells it, and that
    # spelling is the one used from here on.
    asked = {
        str(r.get("login")).casefold(): str(r.get("login"))
        for r in pull.get("requested_reviewers") or []
        if isinstance(r, Mapping)
    }
    if sink.casefold() not in asked:
        return f"{sink} is not asked to review: nothing to do"
    sink = asked[sink.casefold()]
    head = pull.get("head")
    sha = check_sha(str(head.get("sha", ""))) if isinstance(head, Mapping) else ""
    author = str((pull.get("user") or {}).get("login", ""))
    head_repo = str(((head or {}).get("repo") or {}).get("full_name") or "") if isinstance(head, Mapping) else ""
    fork = head_repo.casefold() != repo.casefold()
    raw: list[Mapping[str, Any]] = [
        r for r in paginate(client, f"/repos/{repo}/pulls/{number}/reviews") if isinstance(r, Mapping)
    ]
    reviews = reviews_with_roles(client, repo, raw, author)
    approvers, _stale, _blocking = approvals_of(reviews, sha, author, fork)
    others = [a for a in approvers if a.casefold() != sink.casefold()]
    if not others:
        return f"nobody but {sink} has approved {sha[:7]}: it stays"
    response = client.request("DELETE", f"/repos/{repo}/pulls/{number}/requested_reviewers", body={"reviewers": [sink]})
    if response.status not in (200, 201, 204):
        message = response.data.get("message", "") if isinstance(response.data, dict) else ""
        raise GitHubError(response.status, f"removing the review request of {sink}: {message}")
    return f"removed the request of {sink} ({', '.join(others)} approved {sha[:7]})"
