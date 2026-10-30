"""A fast track: one person of the repository's fast-track team skips every test of one pull request.

The team (``fast_track.team`` of the policy, read from the default branch) runs the repository's fast-track workflow,
from the default branch, with the number of the pull request and a reason. Nothing is believed on the pull request's
say-so: who runs it comes from the run (``github.actor``); the pull request, its author and its files are read here, and
the refusals are the ones below. A fast track is for one commit: the ``CI Autopilot`` check of that commit passes with
the person and the reason in its title (see fast_mark.py), and the merge queue entry of the pull request is not tested
either (``for_queue``). A new commit of the pull request has no fast track.

What it does not do: it does not approve the pull request (the review rule is the repository's), and it does not stop
what already runs.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any

from osac_ci import autopilot_summary
from osac_ci.e2e_check import check_sha
from osac_ci.fast_mark import REASON_MIN, FastTrack, clean_reason
from osac_ci.github.api import GitHubClient, check_repo, get, paginate
from osac_ci.paths import matches
from osac_ci.policy import Policy

LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
QUEUE_REF = re.compile(r"(?:refs/heads/)?gh-readonly-queue/[^/]+(?:/[^/]+)*/pr-(\d+)-[0-9a-f]{40}")


def refusal(
    *,
    actor: str,
    in_team: bool,
    author: str,
    files: Sequence[str],
    reason: str,
    state: str,
    team: str,
    allow_author: bool,
    protected: Sequence[str],
) -> str | None:
    """Why this fast track is refused (a sentence the person reads), or None when it may go ahead. Pure."""
    if state != "open":
        return "the pull request is not open"
    if len(reason) < REASON_MIN:
        return f"say why in a few words ({REASON_MIN} characters at least)"
    if not in_team:
        return f"{actor} is not in the {team} team, which is the only one that may fast-track"
    if actor == author and not allow_author:
        return "a person cannot fast-track their own pull request"
    touched = sorted({f for f in files if any(matches(p, f) for p in protected)})
    if touched:
        shown = ", ".join(touched[:3]) + (f" and {len(touched) - 3} more" if len(touched) > 3 else "")
        return f"it changes files that decide how a change is judged ({shown}): those are reviewed, never fast-tracked"
    return None


def _in_team(client: GitHubClient, org: str, team: str, login: str) -> bool:
    path = f"/orgs/{urllib.parse.quote(org, safe='')}/teams/{urllib.parse.quote(team, safe='')}/memberships/{login}"
    response = client.request("GET", path)
    if response.status == 404:
        return False
    if response.status != 200 or not isinstance(response.data, Mapping):
        raise ValueError(f"could not read the {team} team membership of {login} (HTTP {response.status})")
    return response.data.get("state") == "active"


def _files(client: GitHubClient, repo: str, number: int) -> list[str]:
    files: list[str] = []
    for raw in paginate(client, f"/repos/{repo}/pulls/{number}/files"):
        files.append(str(raw["filename"]))
        if raw.get("previous_filename"):
            files.append(str(raw["previous_filename"]))
    return files


def apply(
    client: GitHubClient,
    repo: str,
    number: int,
    actor: str,
    reason: str,
    policy: Policy,
    run_url: str = "",
) -> str:
    """Fast-track the pull request. Raises ``ValueError`` with the sentence the person reads when it is refused."""
    repo = check_repo(repo)
    rule = policy.fast_track
    if rule is None:
        raise ValueError("the policy of this repository has no fast_track: nobody may fast-track")
    if not LOGIN.fullmatch(actor):
        raise ValueError("the actor is not a login")
    pull = get(client, f"/repos/{repo}/pulls/{number}")
    head = pull.get("head")
    sha = check_sha(str(head.get("sha", ""))) if isinstance(head, Mapping) else ""
    user = pull.get("user")
    author = str(user.get("login", "")) if isinstance(user, Mapping) else ""
    why = clean_reason(reason)
    org = repo.split("/", 1)[0]
    stop = refusal(
        actor=actor,
        in_team=_in_team(client, org, rule.team, actor),
        author=author,
        files=_files(client, repo, number) if pull.get("state") == "open" else (),
        reason=why,
        state=str(pull.get("state")),
        team=rule.team,
        allow_author=rule.allow_author,
        protected=rule.protected_paths,
    )
    if stop is not None:
        raise ValueError(f"fast track refused: {stop}")
    fast = FastTrack(sha, actor, why)
    event = f"Fast track by {actor}: {why}"
    autopilot_summary.update(client, repo, number, policy, event, run_url, fast=fast)
    return f"{fast.title} (commit {sha[:7]})"


def for_queue(client: GitHubClient, repo: str, head_ref: str) -> FastTrack | None:
    """The fast track that holds for a merge queue entry, or None. The pull request the entry is built from is named by
    its branch (``gh-readonly-queue/<base>/pr-<N>-<sha>``); it must be open, and the gate check of its current head
    commit, written by the app, must carry a fast track for that commit. Nothing the entry says can make it look
    fast-tracked: the branch only names a pull request, and the proof is read from the app's check."""
    repo = check_repo(repo)
    found = QUEUE_REF.fullmatch(head_ref)
    if found is None:
        return None
    pull = get(client, f"/repos/{repo}/pulls/{int(found.group(1))}")
    head = pull.get("head")
    if pull.get("state") != "open" or not isinstance(head, Mapping):
        return None
    sha = check_sha(str(head.get("sha", "")))
    return autopilot_summary.fast_track_of(autopilot_summary.gate_of(client, repo, sha), sha)


def current(client: GitHubClient, repo: str, number: int) -> FastTrack | None:
    """The fast track of the pull request's current head commit, if it has one (read from the app's gate check)."""
    repo = check_repo(repo)
    pull = get(client, f"/repos/{repo}/pulls/{number}")
    head = pull.get("head")
    if pull.get("state") != "open" or not isinstance(head, Mapping):
        return None
    sha = check_sha(str(head.get("sha", "")))
    return autopilot_summary.fast_track_of(autopilot_summary.gate_of(client, repo, sha), sha)


def queue_outputs(fast: FastTrack | None) -> dict[str, Any]:
    """What the queue plan says about the entry: the plain facts, for the workflow to read."""
    return {"fast": fast is not None, "login": fast.login if fast else "", "reason": fast.reason if fast else ""}
