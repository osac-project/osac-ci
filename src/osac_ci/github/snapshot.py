"""Build a ``Snapshot`` for one PR from the GitHub API.

Per PR this makes roughly 9 requests (PR, reviews, events, files, 2 pages of check runs, merge-queue lookup, up to
2 membership lookups). It deliberately avoids the bulk ``statusCheckRollup`` GraphQL pull, which returned HTTP
502/504 on this repository for PRs carrying 85 to 114 checks.
"""

from __future__ import annotations

import base64
import binascii
import re
import urllib.parse
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from osac_ci.fingerprint import fingerprint
from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get, paginate
from osac_ci.model import CheckRun, LabelEvent, Review, Snapshot
from osac_ci.paths import any_match
from osac_ci.policy import Approval, ProtectedPaths, Trust
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


AUTH_APP = "github-actions"  # the app a workflow's built-in token posts as; a fork's read-only token cannot post
AUTH_ID = re.compile(r"^osac-ci-auth:v1:(?P<login>[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?):(?P<sha>[0-9a-f]{40})$")


def authorization_external_id(login: str, head_sha: str) -> str:
    return f"osac-ci-auth:v1:{login}:{head_sha}"


def find_authorizers(
    org_client: GitHubClient, org: str, runs: Sequence[CheckRun], head_sha: str, trust: Trust
) -> tuple[str, ...]:
    """Logins of the org members who authorized exactly ``head_sha``, newest first, each once.

    An authorization is a successful check run named ``trust.check_name`` posted by the workflow app, whose external
    id names the authorizer and the commit. The runs were listed for the head commit, and the id must name it too. The
    authorizer must still be an org member now: leaving the org withdraws the authorization. Every one is kept, not just
    the newest, so a replay can tell who had authorized at an earlier moment."""
    found: list[str] = []
    for run in sorted(runs, key=lambda r: r.started_at or "", reverse=True):
        if (run.name, run.status, run.conclusion, run.app) != (trust.check_name, "completed", "success", AUTH_APP):
            continue
        match = AUTH_ID.match(run.external_id)
        if not match or match["sha"] != head_sha or match["login"] in found:
            continue
        if is_org_member(org_client, org, match["login"]):
            found.append(match["login"])
    return tuple(found)


def find_authorizer(org_client: GitHubClient, org: str, runs: Sequence[CheckRun], head_sha: str, trust: Trust) -> str:
    """The newest org member who authorized exactly ``head_sha``, or ``""`` (see ``find_authorizers``)."""
    return next(iter(find_authorizers(org_client, org, runs, head_sha, trust)), "")


# A merge-queue entry lives on a branch GitHub creates: gh-readonly-queue/<base branch>/pr-<number>-<head sha>.
QUEUE_BRANCH = re.compile(r"^gh-readonly-queue/(?P<base>.+)/pr-(?P<number>\d+)-(?P<sha>[0-9a-f]{40})$")
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def parse_queue_branch(name: str) -> str | None:
    """The base branch of a merge-queue branch name, or ``None`` when the name is not one."""
    found = QUEUE_BRANCH.match(name)
    return found["base"] if found else None


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


def _queue_state(events: Sequence[dict[str, Any]]) -> tuple[bool, str]:
    """Replay the issue events, oldest first, up to the ``merged`` event: is the PR queued at the end, and since when?

    A removal by anyone other than the queue's own post-merge cleanup (a human dequeuing and then merging by hand,
    an ejection, a force-push) means the PR was taken out of the queue."""
    merged_at = next((_when(e) for e in events if e.get("event") == "merged"), None)
    queued, since = False, ""
    for event in events:
        kind = event.get("event")
        if kind == "added_to_merge_queue":
            queued, since = True, str(event.get("created_at") or "")
        elif kind == "removed_from_merge_queue":
            if not _is_queue_cleanup(event, merged_at):
                queued, since = False, ""
        elif kind == "head_ref_force_pushed":
            queued, since = False, ""
        elif kind == "merged":
            break
    return queued, since


def queued_at_end(events: Sequence[dict[str, Any]]) -> bool:
    """Was the PR in the merge queue at the moment it merged (or is it queued now, if it has not merged)?

    A removal by anyone other than the queue's own post-merge cleanup means the PR was taken out of the queue, so the
    merge was direct."""
    return _queue_state(events)[0]


def queue_entry_time(events: Sequence[dict[str, Any]]) -> str:
    """When the queue entry that was still there at the end was created, or ``""`` if the PR was not queued then.

    A push removes a PR from the queue, so for a queue-merged PR its head commit at that moment is its final head."""
    queued, since = _queue_state(events)
    return since if queued else ""


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
    return text, teams, _fingerprints(client, repo, base_ref, head_sha, reviews)


def _fingerprints(
    client: GitHubClient, repo: str, base_ref: str, head_sha: str, reviews: Sequence[Review]
) -> dict[str, str]:
    """Change fingerprints of the head and of every commit that got an approval."""
    commits = [head_sha]
    for review in reviews:
        if review.state == "APPROVED" and review.commit_id and review.commit_id not in commits:
            commits.append(review.commit_id)
    fingerprints: dict[str, str] = {}
    for sha in commits[:_MAX_FINGERPRINTED_COMMITS]:
        value = fetch_change_fingerprint(client, repo, base_ref, sha)
        if value is not None:
            fingerprints[sha] = value
    return fingerprints


def _dismissals(raw_events: Sequence[dict[str, Any]]) -> dict[int, tuple[str, str]]:
    """Review id -> (when it was dismissed, the state it had before), from the ``review_dismissed`` events."""
    found: dict[int, tuple[str, str]] = {}
    for event in raw_events:
        if event.get("event") != "review_dismissed":
            continue
        review = event.get("dismissed_review") or {}
        if isinstance(review.get("review_id"), int) and review.get("state"):
            found[review["review_id"]] = (str(event.get("created_at") or ""), str(review["state"]).upper())
    return found


def _with_dismissal(review: Review, dismissals: dict[int, tuple[str, str]]) -> Review:
    known = dismissals.get(review.id) if review.id is not None else None
    if review.state != "DISMISSED" or known is None:
        return review
    return replace(review, dismissed_at=known[0], state_before_dismissal=known[1])


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


def fetch_queue_snapshot(client: GitHubClient, repo: str, sha: str, base_ref: str) -> Snapshot:
    """What the planner needs to judge a merge-queue commit: the checks on it and the files the queue group changes.

    A queue commit is not a pull request, so there are no labels, reviews or authors to read. The changed files are the
    difference to the base branch, which covers every PR stacked in the entry. When that list cannot be read completely
    (the compare API stops at 300 files) it is marked unknown so that no job is skipped on a guess."""
    repo = check_repo(repo)
    if not COMMIT_SHA.match(sha):
        raise ValueError(f"invalid commit sha: {sha!r}")
    runs = tuple(
        CheckRun(
            raw["name"],
            raw["status"],
            raw.get("conclusion"),
            raw.get("started_at"),
            raw.get("external_id") or "",
            (raw.get("app") or {}).get("slug", ""),
        )
        for raw in paginate(client, f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs")
    )
    response = client.request("GET", f"/repos/{repo}/compare/{_quote(base_ref)}...{sha}", params={"per_page": "1"})
    files = response.data.get("files") if response.status == 200 and isinstance(response.data, dict) else None
    known = files is not None and len(files) < _COMPARE_FILE_CAP
    return Snapshot(
        repo=repo,
        number=0,
        head_sha=sha,
        check_runs=runs,
        changed_files=tuple(f["filename"] for f in files) if known and files else (),
        changed_files_known=known,
        base_ref=base_ref,
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
    trust: Trust | None = None,
    protected: Sequence[ProtectedPaths] = (),
) -> Snapshot:
    """Read everything the planner needs.

    ``lookup_membership=False`` approximates org membership from ``author_association`` (MEMBER or OWNER), which
    needs no org-scoped credential. The real authorization check needs one (see the design doc).

    ``approval`` is the policy's native-approval section; when given, CODEOWNERS (from the base branch), the members
    of owner teams and the change fingerprints of the head and of every approved commit are read as well.

    ``protected`` are the policy's protected-path rules; for each rule whose files changed, the members of its approver
    teams are read (and the change fingerprints, when the rule carries approvals over a rebase).

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
    dismissals = _dismissals(raw_events)
    reviews = tuple(_with_dismissal(r, dismissals) for r in reviews)
    events = tuple(
        LabelEvent(
            raw["event"], raw["label"]["name"], (raw.get("actor") or {}).get("login", ""), raw.get("created_at") or ""
        )
        for raw in raw_events
        if raw.get("label")
    )
    runs = tuple(
        CheckRun(
            raw["name"],
            raw["status"],
            raw.get("conclusion"),
            raw.get("started_at"),
            raw.get("external_id") or "",
            (raw.get("app") or {}).get("slug", ""),
            raw.get("completed_at"),
        )
        for raw in paginate(client, f"{base}/commits/{head_sha}/check-runs", key="check_runs")
    )
    files = tuple(raw["filename"] for raw in paginate(client, f"{base}/pulls/{number}/files"))

    if lookup_membership:
        author_member = is_org_member(org_client, org, author)
        owner_member = bool(fork_owner) and fork_owner != author and is_org_member(org_client, org, fork_owner)
    else:
        author_member = pr.get("author_association") in {"MEMBER", "OWNER"}
        owner_member = False

    authorizers: tuple[str, ...] = ()
    if trust and trust.authorization == "sha-bound" and is_fork and not (author_member or owner_member):
        authorizers = find_authorizers(org_client, org, runs, head_sha, trust)

    base_ref: str = (pr.get("base") or {}).get("ref", "")  # only the native-approval inputs need it
    owners_text, team_members, fingerprints = (
        _approval_inputs(client, org_client, repo, base_ref, head_sha, reviews, files) if approval else (None, {}, {})
    )
    matched = [rule for rule in protected if any(any_match(rule.paths, f) for f in files)]
    if matched:
        if not base_ref:
            raise ValueError("the pull request has no base branch, cannot judge protected paths")
        team_members = dict(team_members)
        for rule in matched:
            for owner in rule.approvers:
                if "/" in owner and owner[1:] not in team_members:
                    team_members[owner[1:]] = fetch_team_members(org_client, owner[1:])
        if not fingerprints and any(rule.carry_over == "trivial-rebase" for rule in matched):
            fingerprints = _fingerprints(client, repo, base_ref, head_sha, reviews)

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
        enqueued_at=queue_entry_time(raw_events),
        base_ref=base_ref,
        codeowners=owners_text,
        team_members=team_members,
        change_fingerprints=fingerprints,
        authorized_by=authorizers[0] if authorizers else "",
        authorizers=authorizers,
    )
