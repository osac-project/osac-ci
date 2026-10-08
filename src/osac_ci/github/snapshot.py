"""Build a ``Snapshot`` for one PR from the GitHub API.

Per PR this makes roughly 9 requests (PR, reviews, events, files, 2 pages of check runs, merge-queue lookup, up to
2 membership lookups). It deliberately avoids the bulk ``statusCheckRollup`` GraphQL pull, which returned HTTP
502/504 on this repository for PRs carrying 85 to 114 checks.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get, paginate
from osac_ci.model import CheckRun, LabelEvent, Review, Snapshot

_QUEUE_QUERY = """
query($id: ID!) { node(id: $id) { ... on PullRequest { mergeQueueEntry { id } } } }
"""


def is_org_member(client: GitHubClient, org: str, login: str) -> bool:
    """204 means member, 404 means not a member; anything else is an error (never guessed)."""
    path = f"/orgs/{urllib.parse.quote(org, safe='')}/members/{urllib.parse.quote(login, safe='')}"
    status = client.request("GET", path).status
    if status == 204:
        return True
    if status == 404:
        return False
    raise GitHubError(status, f"GET {path}")


def _in_merge_queue(client: GitHubClient, node_id: str) -> bool:
    response = client.request("POST", "/graphql", body={"query": _QUEUE_QUERY, "variables": {"id": node_id}})
    if response.status != 200 or not isinstance(response.data, dict) or response.data.get("errors"):
        raise GitHubError(response.status, "merge queue lookup failed")
    node = (response.data.get("data") or {}).get("node") or {}
    return node.get("mergeQueueEntry") is not None


def _review(raw: dict[str, Any]) -> Review | None:
    user = raw.get("user")
    if not user:  # deleted account: the legacy rules ignore reviews without a user too
        return None
    return Review(
        user=user["login"],
        state=raw["state"],
        user_type=user.get("type") or "User",
        submitted_at=raw.get("submitted_at"),
        id=raw.get("id"),
        commit_id=raw.get("commit_id"),
    )


def fetch_snapshot(
    client: GitHubClient,
    repo: str,
    number: int,
    *,
    org: str,
    lookup_membership: bool = True,
) -> Snapshot:
    """Read everything the planner needs.

    ``lookup_membership=False`` approximates org membership from ``author_association`` (MEMBER or OWNER), which
    needs no org-scoped credential. The real authorization check needs one (see the design doc).
    """
    repo = check_repo(repo)
    if number < 1:
        raise ValueError(f"invalid PR number: {number}")
    base = f"/repos/{repo}"
    pr = get(client, f"{base}/pulls/{number}")
    head_sha: str = pr["head"]["sha"]
    head_repo = pr["head"].get("repo")  # None when the fork was deleted
    is_fork = head_repo is None or head_repo["full_name"] != repo
    author: str = pr["user"]["login"]
    fork_owner = (head_repo or {}).get("owner", {}).get("login", "") if is_fork else ""

    reviews = tuple(r for raw in paginate(client, f"{base}/pulls/{number}/reviews") if (r := _review(raw)))
    events = tuple(
        LabelEvent(raw["event"], raw["label"]["name"], (raw.get("actor") or {}).get("login", ""))
        for raw in paginate(client, f"{base}/issues/{number}/events")
        if raw.get("label")
    )
    runs = tuple(
        CheckRun(raw["name"], raw["status"], raw.get("conclusion"), raw.get("started_at"))
        for raw in paginate(client, f"{base}/commits/{head_sha}/check-runs", key="check_runs")
    )
    files = tuple(raw["filename"] for raw in paginate(client, f"{base}/pulls/{number}/files"))

    if lookup_membership:
        author_member = is_org_member(client, org, author)
        owner_member = bool(fork_owner) and fork_owner != author and is_org_member(client, org, fork_owner)
    else:
        author_member = pr.get("author_association") in {"MEMBER", "OWNER"}
        owner_member = False

    return Snapshot(
        repo=repo,
        number=number,
        head_sha=head_sha,
        is_draft=bool(pr.get("draft")),
        is_fork=is_fork,
        author=author,
        author_is_org_member=author_member,
        fork_owner=fork_owner,
        fork_owner_is_org_member=owner_member,
        labels=frozenset(label["name"] for label in pr.get("labels", ())),
        reviews=reviews,
        label_events=events,
        check_runs=runs,
        changed_files=files,
        in_merge_queue=_in_merge_queue(client, pr["node_id"]),
    )
