"""A fast track: one person of the repository's fast-track team skips every test of one pull request.

The team (``fast_track.team`` of the policy, read from the default branch) runs the repository's fast-track workflow,
from the default branch, with the number of the pull request and a reason. Nothing is believed on the pull request's
say-so: who runs it comes from the run (``github.actor``); the pull request, its author and its files are read here, and
the refusals are the ones below.

A fast track is for one commit. It is the ``E2E`` check of the commit: the app writes it as passed, with the person and
the reason in its title and a mark in its text (see fast_mark.py). Only the app can write that check, so it is its own
proof, and nothing else needs to remember it. A new commit of the pull request has no mark, and no fast track. The rest
follows from the mark:

* the decision starts no suites (``Action.SKIP_FAST_TRACKED``) and no later write of the ``E2E`` check replaces it;
* the repository's CI workflow (``fast_track.ci_workflow``) asks ``fast-track-check`` and passes without its tests, and
  the fast track stops its runs for the commit and runs it again, so the ``CI`` check passes too;
* the merge queue plan asks the same of the pull request of the entry (``for_queue``) and plans no job, and the result
  writes the ``E2E`` check of the entry with the same words.

What it does not do: it does not approve the pull request (the review rule is the repository's, and once it is met the
repository's auto-merge puts the pull request in the merge queue), it does not skip checks that are not tests (the Jira
reference), and it leaves a merge queue entry that is already being tested alone (it is planned again when rebuilt).
"""

from __future__ import annotations

import re
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from osac_ci import e2e_dispatch
from osac_ci.e2e_check import CONTEXT, check_sha, fast_track_on, post_status
from osac_ci.fast_mark import REASON_MIN, FastTrack, clean_reason, encode
from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get, paginate
from osac_ci.paths import matches
from osac_ci.policy import Policy

LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
QUEUE_REF = re.compile(r"(?:refs/heads/)?gh-readonly-queue/[^/]+(?:/[^/]+)*/pr-(\d+)-[0-9a-f]{40}")
CANCEL_WAIT = 60.0  # seconds to wait for a cancelled run of the CI workflow to end before it is run again


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


def _runs(client: GitHubClient, path: str, params: Mapping[str, str]) -> list[Mapping[str, Any]]:
    return [r for r in paginate(client, path, key="workflow_runs", params=params) if isinstance(r, Mapping)]


def _cancel(client: GitHubClient, repo: str, run_id: int) -> bool:
    try:
        response = client.request("POST", f"/repos/{repo}/actions/runs/{run_id}/cancel")
    except GitHubError:
        return False
    return response.status in (202, 404, 409)  # accepted; already gone; already finished


def cancel_suites(client: GitHubClient, repo: str, number: int, sha: str) -> tuple[list[str], list[str]]:
    """Cancel the E2E suites that run as runs of their own for this pull request and this commit, found by the name of
    the run. Returns the suites stopped and the suites that could not be (one failure does not stop the rest)."""
    stopped: list[str] = []
    failed: list[str] = []
    for status in ("queued", "in_progress"):
        for run in _runs(client, f"/repos/{repo}/actions/runs", {"event": "workflow_dispatch", "status": status}):
            found = e2e_dispatch.RUN_NAME.fullmatch(str(run.get("display_title") or ""))
            if (
                found is None
                or int(found["number"]) != number
                or found["sha"] != sha
                or not isinstance(run.get("id"), int)
            ):
                continue
            (stopped if _cancel(client, repo, int(run["id"])) else failed).append(found["suite"])
    return stopped, failed


def rerun_ci(
    client: GitHubClient,
    repo: str,
    sha: str,
    workflow: str,
    sleep: Callable[[float], None] = time.sleep,
    wait: float = CANCEL_WAIT,
) -> str:
    """Run the repository's CI workflow again for the commit, so that its plan sees the fast track and the ``CI`` check
    passes. A run that is still going is cancelled first (a run can only be run again when it has ended). Returns what
    to say about it ("" when the workflow has no run for the commit)."""
    path = f"/repos/{repo}/actions/workflows/{urllib.parse.quote(workflow, safe='')}/runs"
    runs = _runs(client, path, {"head_sha": sha, "event": "pull_request"})
    if not runs:
        return ""
    run = max(runs, key=lambda r: int(r["id"]))
    run_id = int(run["id"])
    if run.get("status") != "completed":
        _cancel(client, repo, run_id)
        waited = 0.0
        while waited < wait:
            current = get(client, f"/repos/{repo}/actions/runs/{run_id}")
            if current.get("status") == "completed":
                break
            sleep(2.0)
            waited += 2.0
        else:
            return f"could not run {workflow} again (the run did not end in {int(wait)} s)"
    try:
        response = client.request("POST", f"/repos/{repo}/actions/runs/{run_id}/rerun")
    except GitHubError:
        return f"could not run {workflow} again"
    return f"ran {workflow} again" if response.status == 201 else f"could not run {workflow} again"


def apply(
    client: GitHubClient,
    repo: str,
    number: int,
    actor: str,
    reason: str,
    policy: Policy,
    run_url: str = "",
    sleep: Callable[[float], None] = time.sleep,
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
    stop = refusal(
        actor=actor,
        in_team=_in_team(client, repo.split("/", 1)[0], rule.team, actor),
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
    stopped, failed = cancel_suites(client, repo, number, sha)
    notes = [f"stopped {', '.join(sorted(set(stopped)))}"] if stopped else []
    notes += [f"could not stop {', '.join(sorted(set(failed)))}"] if failed else []
    # The E2E check first: the CI workflow, run again, reads it.
    summary = (
        f"> **Fast track** by @{actor}: {why}\n\nEvery test of commit `{sha[:7]}` is skipped. A new commit has none."
    )
    if notes:
        summary += f"\n\n{'; '.join(notes).capitalize()}."
    body = {
        "name": CONTEXT,
        "head_sha": sha,
        "status": "completed",
        "conclusion": "success",
        "output": {"title": fast.title[:140], "summary": summary, "text": encode(fast)},
    }
    if run_url:
        body["details_url"] = run_url
    response = client.request("POST", f"/repos/{repo}/check-runs", body=body)
    if response.status not in (200, 201):
        raise GitHubError(response.status, f"writing the {CONTEXT} check failed")
    again = rerun_ci(client, repo, sha, rule.ci_workflow, sleep)
    if again:
        notes.append(again)
    more = f", {'; '.join(notes)}" if notes else ""
    return f"{fast.title} (commit {sha[:7]}{more})"


def _pull_head(client: GitHubClient, repo: str, number: int) -> str:
    pull = get(client, f"/repos/{repo}/pulls/{number}")
    head = pull.get("head")
    if pull.get("state") != "open" or not isinstance(head, Mapping):
        return ""
    return check_sha(str(head.get("sha", "")))


def current(client: GitHubClient, repo: str, number: int) -> FastTrack | None:
    """The fast track of the pull request's current head commit, if it has one."""
    repo = check_repo(repo)
    sha = _pull_head(client, repo, number)
    return fast_track_on(client, repo, sha) if sha else None


def for_queue(client: GitHubClient, repo: str, head_ref: str) -> FastTrack | None:
    """The fast track that holds for a merge queue entry, or None. The pull request the entry is built from is named by
    its branch (``gh-readonly-queue/<base>/pr-<N>-<sha>``); it must be open, and the ``E2E`` check of its current head
    commit, written by the app, must carry a fast track for that commit. Nothing the entry says can make it look
    fast-tracked: the branch only names a pull request, and the proof is read from the app's check."""
    found = QUEUE_REF.fullmatch(head_ref)
    return current(client, repo, int(found.group(1))) if found else None


def outputs(fast: FastTrack | None) -> dict[str, Any]:
    """The plain facts, for a workflow to read."""
    return {"fast": fast is not None, "login": fast.login if fast else "", "reason": fast.reason if fast else ""}


def entry_check(client: GitHubClient, repo: str, sha: str, detail: str, run_url: str = "") -> None:
    """Write the ``E2E`` check of a merge queue entry that is not tested: passed, and why (``detail`` is
    ``login: reason``)."""
    post_status(client, repo, sha, CONTEXT, "success", f"Fast track: skipped by {detail}", run_url, "checks")
