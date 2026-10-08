"""Build a ``Snapshot`` for one PR from the GitHub API.

Per PR this makes roughly 9 requests (PR, reviews, events, files, 2 pages of check runs, merge-queue lookup, up to
2 membership lookups). It deliberately avoids the bulk ``statusCheckRollup`` GraphQL pull, which returned HTTP
502/504 on this repository for PRs carrying 85 to 114 checks.
"""

from __future__ import annotations

import base64
import binascii
import urllib.parse
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from osac_ci.fingerprint import fingerprint
from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get, paginate
from osac_ci.model import CheckRun, LabelEvent, Review, Snapshot
from osac_ci.policy import Approval
from osac_ci.rules import codeowners

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


QUEUE_BOT = "github-merge-queue[bot]"
_CLEANUP_WINDOW = timedelta(seconds=2)


def _when(event: dict[str, Any]) -> datetime | None:
    stamp = event.get("created_at")
    return datetime.fromisoformat(stamp) if stamp else None


def _is_queue_cleanup(event: dict[str, Any], merged_at: datetime | None) -> bool:
    """The queue removes a PR from itself as it merges it. The API can list that removal just before ``merged``
    (same second), so a removal by the queue bot within a couple of seconds of the merge is cleanup, not a dequeue."""
    removed_at = _when(event)
    if merged_at is None or removed_at is None:
        return False
    actor = (event.get("actor") or {}).get("login")
    return actor == QUEUE_BOT and abs(removed_at - merged_at) <= _CLEANUP_WINDOW


def queued_at_end(events: Sequence[dict[str, Any]]) -> bool:
    """Was the PR in the merge queue at the moment it merged (or is it queued now, if it has not merged)?

    Replays the issue events, oldest first, up to the ``merged`` event. A removal by anyone other than the queue's
    own post-merge cleanup (a human dequeuing and then merging by hand, an ejection, a force-push) means the PR
    was taken out of the queue, so the merge was direct.
    """
    merged_at = next((_when(e) for e in events if e.get("event") == "merged"), None)
    queued = False
    for event in events:
        kind = event.get("event")
        if kind == "added_to_merge_queue":
            queued = True
        elif kind == "removed_from_merge_queue":
            if not _is_queue_cleanup(event, merged_at):
                queued = False
        elif kind == "head_ref_force_pushed":
            queued = False
        elif kind == "merged":
            break
    return queued


CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")  # GitHub's lookup order
_COMPARE_FILE_CAP = 300  # the compare API lists at most this many files; beyond it a fingerprint would be partial
_MAX_FINGERPRINTED_COMMITS = 10


def _quote(path: str) -> str:
    return urllib.parse.quote(path, safe="/")


def fetch_codeowners(client: GitHubClient, repo: str, base_ref: str) -> str | None:
    """CODEOWNERS text from the base branch (never the PR head). ``None`` when the repository has none."""
    for path in CODEOWNERS_PATHS:
        response = client.request("GET", f"/repos/{repo}/contents/{_quote(path)}", params={"ref": base_ref})
        if response.status == 404:
            continue
        if response.status != 200 or not isinstance(response.data, dict):
            raise GitHubError(response.status, f"GET contents/{path}")
        try:
            return base64.b64decode(response.data.get("content", "")).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise GitHubError(response.status, f"CODEOWNERS is not readable text: {exc}") from exc
    return None


def fetch_team_members(client: GitHubClient, team: str) -> frozenset[str] | None:
    """Logins in ``org/team``; ``None`` when the credential cannot read the team (never an empty guess)."""
    org, _, slug = team.partition("/")
    if not org or not slug:
        return None
    path = f"/orgs/{urllib.parse.quote(org, safe='')}/teams/{urllib.parse.quote(slug, safe='')}/members"
    try:
        return frozenset(member["login"] for member in paginate(client, path))
    except GitHubError:
        return None


def fetch_change_fingerprint(client: GitHubClient, repo: str, base_ref: str, sha: str) -> str | None:
    """Fingerprint of what ``sha`` changes relative to the base branch, or ``None`` if it cannot be known exactly."""
    response = client.request("GET", f"/repos/{repo}/compare/{_quote(base_ref)}...{sha}", params={"per_page": "1"})
    if response.status != 200 or not isinstance(response.data, dict):
        return None
    files = response.data.get("files") or []
    if len(files) >= _COMPARE_FILE_CAP:
        return None
    parts: list[str] = []
    for item in sorted(files, key=lambda f: f["filename"]):
        previous = item.get("previous_filename") or item["filename"]
        parts.append(f"diff --git a/{previous} b/{item['filename']}")
        parts.append(f"status {item.get('status', '')}")
        # No patch means binary or too large: fall back to the blob id, which can only err toward "changed".
        parts.append(item["patch"] if item.get("patch") else f"GIT binary patch\nblob {item.get('sha', '')}")
    return fingerprint("\n".join(parts))


def _approval_inputs(
    client: GitHubClient,
    org_client: GitHubClient,
    repo: str,
    base_ref: str,
    head_sha: str,
    reviews: Sequence[Review],
    files: Sequence[str],
) -> tuple[str | None, dict[str, frozenset[str] | None], dict[str, str]]:
    if not base_ref:
        raise ValueError("the pull request has no base branch, cannot read CODEOWNERS")
    text = fetch_codeowners(client, repo, base_ref)
    teams: dict[str, frozenset[str] | None] = {}
    if text is not None:
        rules = codeowners.parse(text)
        for path in files:
            for owner in codeowners.owners_of(rules, path) or ():
                if owner.startswith("@") and "/" in owner and owner[1:] not in teams:
                    teams[owner[1:]] = fetch_team_members(org_client, owner[1:])
    commits = [head_sha]
    for review in reviews:
        if review.state == "APPROVED" and review.commit_id and review.commit_id not in commits:
            commits.append(review.commit_id)
    fingerprints: dict[str, str] = {}
    for sha in commits[:_MAX_FINGERPRINTED_COMMITS]:
        value = fetch_change_fingerprint(client, repo, base_ref, sha)
        if value is not None:
            fingerprints[sha] = value
    return text, teams, fingerprints


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
    approval: Approval | None = None,
    org_client: GitHubClient | None = None,
) -> Snapshot:
    """Read everything the planner needs.

    ``lookup_membership=False`` approximates org membership from ``author_association`` (MEMBER or OWNER), which
    needs no org-scoped credential. The real authorization check needs one (see the design doc).

    ``approval`` is the policy's native-approval section; when given, CODEOWNERS (from the base branch), the members
    of owner teams and the change fingerprints of the head and of every approved commit are read as well.

    ``org_client`` is used only for the organization lookups (membership and team members). It lets a token that can
    read the organization, and nothing else, be kept apart from the one that reads and posts on the pull request.
    """
    org_client = org_client or client
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
    raw_events = list(paginate(client, f"{base}/issues/{number}/events"))
    events = tuple(
        LabelEvent(raw["event"], raw["label"]["name"], (raw.get("actor") or {}).get("login", ""))
        for raw in raw_events
        if raw.get("label")
    )
    runs = tuple(
        CheckRun(raw["name"], raw["status"], raw.get("conclusion"), raw.get("started_at"))
        for raw in paginate(client, f"{base}/commits/{head_sha}/check-runs", key="check_runs")
    )
    files = tuple(raw["filename"] for raw in paginate(client, f"{base}/pulls/{number}/files"))

    if lookup_membership:
        author_member = is_org_member(org_client, org, author)
        owner_member = bool(fork_owner) and fork_owner != author and is_org_member(org_client, org, fork_owner)
    else:
        author_member = pr.get("author_association") in {"MEMBER", "OWNER"}
        owner_member = False

    base_ref: str = (pr.get("base") or {}).get("ref", "")  # only the native-approval inputs need it
    owners_text, team_members, fingerprints = (
        _approval_inputs(client, org_client, repo, base_ref, head_sha, reviews, files) if approval else (None, {}, {})
    )

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
        queued_per_events=queued_at_end(raw_events),
        base_ref=base_ref,
        codeowners=owners_text,
        team_members=team_members,
        change_fingerprints=fingerprints,
    )
