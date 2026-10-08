"""Publish the planner's verdict as one check run on the PR head commit.

How a state shows up (measured in the sandbox, see the design doc):

* ``success``         the PR may be enqueued, or already is / passed the queue.
* ``in_progress``     machines are working (checks or E2E running): nothing for a person to do.
* ``action_required`` a person has to act (approve, authorize, fix, mark ready). The PR is blocked but nothing turns
                      red, so a gate never "fails" for a condition that is merely unmet; the summary says why.
* ``failure``         only the planner itself failed (``planner-error``): fail closed, with the cause and a re-run hint.

The newest check run of a name decides, so publishing again always repairs a stale verdict. Everything is computed
from live API state, never from the event that triggered the run, and an unchanged verdict is not posted again.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get
from osac_ci.github.snapshot import fetch_snapshot
from osac_ci.model import Mode, State, Verdict
from osac_ci.planner import error_verdict, plan_or_error
from osac_ci.policy import Policy
from osac_ci.render import render_markdown
from osac_ci.report import list_open_prs

CHECK_NAME = "OSAC CI"
_SUMMARY_LIMIT = 60_000  # the API accepts 65,535 characters
_TITLE_LIMIT = 200

SUCCESS = ("completed", "success")
WORKING = ("in_progress", None)
PERSON_NEEDED = ("completed", "action_required")
BROKEN = ("completed", "failure")

OUTCOME: dict[State, tuple[str, str | None]] = {
    State.READY_TO_ENQUEUE: SUCCESS,
    State.IN_QUEUE: SUCCESS,
    State.QUEUE_PASSED: SUCCESS,
    State.CHECKS_RUNNING: WORKING,
    State.E2E_RUNNING: WORKING,
    State.QUEUE_CHECKS_RUNNING: WORKING,
    State.DRAFT: PERSON_NEEDED,
    State.NEEDS_AUTHORIZATION: PERSON_NEEDED,
    State.CHECKS_FAILED: PERSON_NEEDED,
    State.AWAITING_APPROVAL: PERSON_NEEDED,
    State.AWAITING_E2E_SIGNAL: PERSON_NEEDED,
    State.E2E_FAILED: PERSON_NEEDED,
    State.QUEUE_FAILED: PERSON_NEEDED,
    State.PLANNER_ERROR: BROKEN,
}


@dataclass(frozen=True)
class Outcome:
    pr: int
    head_sha: str
    state: State
    status: str
    conclusion: str | None
    action: str  # created | unchanged | dry-run | failed
    detail: str = ""


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def payload(verdict: Verdict, head_sha: str, *, check_name: str = CHECK_NAME, note: str = "") -> dict[str, Any]:
    """The body of ``POST /repos/{repo}/check-runs`` for this verdict."""
    status, conclusion = OUTCOME[verdict.state]
    summary = _short(render_markdown(verdict), _SUMMARY_LIMIT)
    if note:
        summary = f"{note}\n\n{summary}"
    digest = hashlib.sha256(f"{status}|{conclusion}|{summary}".encode()).hexdigest()[:16]
    body: dict[str, Any] = {
        "name": check_name,
        "head_sha": head_sha,
        "status": status,
        "external_id": f"osac-ci:{digest}",
        "output": {"title": _short(f"{verdict.state.value}: {verdict.headline}", _TITLE_LIMIT), "summary": summary},
    }
    if conclusion:
        body["conclusion"] = conclusion
    return body


def _latest_external_id(client: GitHubClient, repo: str, head_sha: str, check_name: str) -> str | None:
    data = get(
        client,
        f"/repos/{repo}/commits/{head_sha}/check-runs",
        {"check_name": check_name, "filter": "latest", "per_page": "10"},
    )
    runs = sorted(data.get("check_runs", ()), key=lambda r: r.get("id", 0))
    return runs[-1].get("external_id") if runs else None


def publish_pr(
    client: GitHubClient,
    policy: Policy,
    repo: str,
    number: int,
    *,
    org: str | None = None,
    lookup_membership: bool = False,
    check_name: str = CHECK_NAME,
    note: str = "",
    dry_run: bool = False,
) -> Outcome:
    """Evaluate one PR from live state and post the verdict unless the newest check run already says the same."""
    repo = check_repo(repo)
    org = org or repo.split("/", 1)[0]
    head_sha: str = get(client, f"/repos/{repo}/pulls/{number}")["head"]["sha"]
    try:
        snapshot = fetch_snapshot(
            client, repo, number, org=org, lookup_membership=lookup_membership, approval=policy.approval
        )
        verdict = plan_or_error(snapshot, policy, Mode.PR)
        head_sha = snapshot.head_sha
    except (GitHubError, KeyError, ValueError) as exc:
        verdict = error_verdict(f"could not read PR #{number}: {exc}")
    body = payload(verdict, head_sha, check_name=check_name, note=note)
    status, conclusion = body["status"], body.get("conclusion")
    if dry_run:
        return Outcome(number, head_sha, verdict.state, status, conclusion, "dry-run", body["output"]["title"])
    if _latest_external_id(client, repo, head_sha, check_name) == body["external_id"]:
        return Outcome(number, head_sha, verdict.state, status, conclusion, "unchanged")
    response = client.request("POST", f"/repos/{repo}/check-runs", body=body)
    if response.status != 201:
        raise GitHubError(response.status, f"POST /repos/{repo}/check-runs")
    return Outcome(number, head_sha, verdict.state, status, conclusion, "created", body["output"]["title"])


def sweep(
    client: GitHubClient,
    policy: Policy,
    repo: str,
    *,
    org: str | None = None,
    lookup_membership: bool = False,
    check_name: str = CHECK_NAME,
    note: str = "",
    dry_run: bool = False,
    limit: int | None = None,
    workers: int = 4,
) -> list[Outcome]:
    """Publish for every open PR. One PR failing never stops the others; it is reported as ``failed``."""
    prs = list_open_prs(client, repo, include_drafts=True)
    if limit is not None:
        prs = prs[:limit]

    def one(pr: dict[str, Any]) -> Outcome:
        number = pr["number"]
        try:
            return publish_pr(
                client,
                policy,
                repo,
                number,
                org=org,
                lookup_membership=lookup_membership,
                check_name=check_name,
                note=note,
                dry_run=dry_run,
            )
        except (GitHubError, KeyError, ValueError) as exc:
            sha = str((pr.get("head") or {}).get("sha", ""))
            return Outcome(number, sha, State.PLANNER_ERROR, "", None, "failed", str(exc))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, prs))


def describe(outcomes: Sequence[Outcome]) -> str:
    lines = [
        f"PR #{o.pr}: {o.action} {o.conclusion or o.status} ({o.state.value})"
        + (f" {o.detail}" if o.action == "failed" else "")
        for o in outcomes
    ]
    failed = sum(o.action == "failed" for o in outcomes)
    lines.append(f"{len(outcomes)} PRs, {failed} failed")
    return "\n".join(lines) + "\n"
