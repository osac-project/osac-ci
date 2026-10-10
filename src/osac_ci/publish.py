"""Publish the planner's verdict as one check run on the PR head commit.

How a state shows up (measured in the sandbox, see the design doc):

* ``success``         the PR may be enqueued, or already is / passed the queue.
* ``in_progress``     machines are working (checks or E2E running): nothing for a person to do.
* ``action_required`` a person has to act (approve, authorize, fix, mark ready). The PR is blocked but nothing turns
                      red, so a gate never "fails" for a condition that is merely unmet; the summary says why.
* ``failure``         only the planner itself failed (``planner-error``): fail closed, with the cause and a re-run hint.

The newest check run of a name decides, so publishing again always repairs a stale verdict. Everything is computed
from live API state, never from the event that triggered the run, and an unchanged verdict is not posted again.

The same check name is posted on merge-queue commits (``publish_queue``), where the required checks are the jobs
required at ``queue``: ``success`` once they all passed, ``in_progress`` while any is pending, ``failure`` when one
failed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import zip_longest
from typing import Any

from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get, rate_remaining
from osac_ci.github.snapshot import COMMIT_SHA, fetch_queue_snapshot, fetch_snapshot
from osac_ci.github.standing import ACTIONS_APP_ID, fetch_standings
from osac_ci.model import Mode, State, Verdict
from osac_ci.planner import error_verdict, plan_or_error
from osac_ci.policy import Policy
from osac_ci.render import render_markdown
from osac_ci.report import list_open_prs
from osac_ci.stale import StaleRules, select_stale

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
    # A failed required check on a queue commit is a real failure, not an unmet condition, and it must be red: an
    # action_required check would leave the entry waiting for its timeout instead of being ejected.
    State.QUEUE_FAILED: BROKEN,
    State.PLANNER_ERROR: BROKEN,
}


class Sweep(list["Outcome"]):
    """The outcomes of one sweep, plus how many PRs were open (so a partial sweep can say it was partial)."""

    def __init__(self, outcomes: Iterable[Outcome], open_prs: int, reasons: dict[int, str] | None = None) -> None:
        super().__init__(outcomes)
        self.open_prs = open_prs
        self.reasons = reasons or {}  # stale-only sweep: why each PR was looked at


@dataclass(frozen=True)
class Outcome:
    pr: int
    head_sha: str
    state: State
    status: str
    conclusion: str | None
    action: str  # created | unchanged | dry-run | failed | skipped
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
    """The id of the newest run of this check on the commit, to avoid posting the same verdict twice. If the listing
    fails there is nothing to compare with: post anyway, so a failing API never hides a fail-closed verdict."""
    try:
        data = get(
            client,
            f"/repos/{repo}/commits/{head_sha}/check-runs",
            {"check_name": check_name, "filter": "latest", "per_page": "10"},
        )
    except GitHubError:
        return None
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
    org_client: GitHubClient | None = None,
    refresh: bool = False,
) -> Outcome:
    """Evaluate one PR from live state and post the verdict unless the newest check run already says the same.

    ``refresh`` posts it even then. A stale-only sweep decides what to look at from the time of the newest check run, so
    a verdict that was re-checked and found unchanged must still move that time, or the PR would be looked at again by
    every sweep until it changed."""
    repo = check_repo(repo)
    org = org or repo.split("/", 1)[0]
    head_sha: str = get(client, f"/repos/{repo}/pulls/{number}")["head"]["sha"]
    try:
        snapshot = fetch_snapshot(
            client,
            repo,
            number,
            org=org,
            lookup_membership=lookup_membership,
            approval=policy.approval,
            protected=policy.protected_paths,
            override=policy.override,
            org_client=org_client,
            trust=policy.trust,
        )
        verdict = plan_or_error(snapshot, policy, Mode.PR)
        head_sha = snapshot.head_sha
    except (GitHubError, KeyError, ValueError) as exc:
        verdict = error_verdict(f"could not read PR #{number}: {exc}")
    body = payload(verdict, head_sha, check_name=check_name, note=note)
    status, conclusion = body["status"], body.get("conclusion")
    if dry_run:
        return Outcome(number, head_sha, verdict.state, status, conclusion, "dry-run", body["output"]["title"])
    if not refresh and _latest_external_id(client, repo, head_sha, check_name) == body["external_id"]:
        return Outcome(number, head_sha, verdict.state, status, conclusion, "unchanged")
    response = client.request("POST", f"/repos/{repo}/check-runs", body=body)
    if response.status != 201:
        raise GitHubError(response.status, f"POST /repos/{repo}/check-runs")
    return Outcome(number, head_sha, verdict.state, status, conclusion, "created", body["output"]["title"])


def publish_queue(
    client: GitHubClient,
    policy: Policy,
    repo: str,
    sha: str,
    base_ref: str,
    *,
    check_name: str = CHECK_NAME,
    note: str = "",
    dry_run: bool = False,
) -> Outcome:
    """Evaluate a merge-queue commit and post the verdict on it, unless the newest run already says the same.

    Fails closed like ``publish_pr``: if the commit cannot be read the check is a visible failure naming the cause,
    never a pass. The snapshot never needs a token that can read the organization."""
    repo = check_repo(repo)
    if not COMMIT_SHA.match(sha):
        raise ValueError(f"invalid commit sha: {sha!r}")  # nothing can be posted on it, so refuse before any request
    try:
        verdict = plan_or_error(fetch_queue_snapshot(client, repo, sha, base_ref), policy, Mode.QUEUE)
    except (GitHubError, KeyError, ValueError) as exc:
        verdict = error_verdict(f"could not read queue commit {sha[:7]}: {exc}", Mode.QUEUE)
    body = payload(verdict, sha, check_name=check_name, note=note)
    status, conclusion = body["status"], body.get("conclusion")
    if dry_run:
        return Outcome(0, sha, verdict.state, status, conclusion, "dry-run", body["output"]["title"])
    if _latest_external_id(client, repo, sha, check_name) == body["external_id"]:
        return Outcome(0, sha, verdict.state, status, conclusion, "unchanged")
    response = client.request("POST", f"/repos/{repo}/check-runs", body=body)
    if response.status != 201:
        raise GitHubError(response.status, f"POST /repos/{repo}/check-runs")
    return Outcome(0, sha, verdict.state, status, conclusion, "created", body["output"]["title"])


_FIND_PAGES = 3  # 300 open pull requests, most recently updated first: a PR that just changed is near the top


def find_open_prs(client: GitHubClient, repo: str, *, sha: str = "", owner: str = "", branch: str = "") -> list[int]:
    """The open pull requests a workflow run belongs to, from what the run says about itself.

    The head commit is exact and is tried first. It also covers a run started by a review, which reports the base
    repository as its head repository (and the fork's branch name), so the owner and branch of such a run name nothing.
    Only when no open PR has that head, and the run gave an owner and a branch, is the open PR with that head owner and
    branch used (a run on a fork reports both correctly). Nothing is guessed: no match is an empty list."""
    repo = check_repo(repo)
    if sha:
        if not COMMIT_SHA.match(sha):
            raise ValueError(f"invalid commit sha: {sha!r}")
        for page in range(1, _FIND_PAGES + 1):
            batch = get(
                client,
                f"/repos/{repo}/pulls",
                {"state": "open", "sort": "updated", "direction": "desc", "per_page": "100", "page": str(page)},
            )
            found = [int(pr["number"]) for pr in batch if (pr.get("head") or {}).get("sha") == sha]
            if found:
                return found
            if len(batch) < 100:
                break
    if owner and branch:
        batch = get(client, f"/repos/{repo}/pulls", {"state": "open", "head": f"{owner}:{branch}", "per_page": "10"})
        return [int(pr["number"]) for pr in batch]
    return []


def select_prs(
    prs: Sequence[dict[str, Any]], *, recent: int | None, rotate: int | None, tick: int, limit: int | None = None
) -> list[dict[str, Any]]:
    """Which open PRs one sweep looks at.

    With no budget (both ``None``) every PR. Otherwise the ``recent`` most recently updated ones (``prs`` is listed
    newest first), because activity is where a verdict goes stale, plus a slice of ``rotate`` of the others. The slice
    advances with ``tick`` (a counter that grows by one per sweep interval) and wraps, so with no other state every PR
    is looked at within ceil(others / rotate) sweeps, as long as it stays among the others.

    The two groups are interleaved (recent, rotated, recent, rotated, ...), not listed one after the other. A sweep that
    runs low on requests stops starting PRs, and with the groups back to back the same recent PRs would always start
    first and every rotated PR would be skipped for good. Interleaved, both keep being served while the quota lasts;
    under pressure the rotation just covers the backlog more slowly, and the PRs left out are reported as skipped.

    ``limit`` caps the whole sweep and is shared between the two groups the same way (half each, with what one group
    cannot use going to the other). The rotated slice is then only as long as the limit lets through, so the rotation
    still steps through every PR of the others: truncating the interleaved list afterwards would process the first
    PR of each slice and never reach the rest."""
    if recent is None and rotate is None:
        return list(prs) if limit is None else list(prs[: max(0, limit)])
    recent = max(0, recent or 0)  # a negative count must never slice from the end
    rotate = max(0, rotate or 0)
    take_recent, take_rotate = recent, rotate
    if limit is not None:
        limit = max(0, limit)
        if rotate and recent:
            take_recent = min(recent, -(-limit // 2))
            take_rotate = min(rotate, limit - take_recent)
            take_recent = min(recent, limit - take_rotate)  # what the rotated group cannot use goes to the recent one
        else:
            take_recent, take_rotate = min(recent, limit), min(rotate, limit)
    first = list(prs[:recent][:take_recent])
    others = sorted(prs[recent:], key=lambda pr: pr["number"])
    if not take_rotate or not others:
        return first
    slices = -(-len(others) // take_rotate)
    start = (tick % slices) * take_rotate
    rotated = others[start : start + take_rotate]
    mixed = [pr for pair in zip_longest(first, rotated) for pr in pair if pr is not None]
    return mixed


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
    org_client: GitHubClient | None = None,
    recent: int | None = None,
    rotate: int | None = None,
    tick: int = 0,
    reserve: int = 0,
    stale: StaleRules | None = None,
    now: datetime | None = None,
    verdict_app_id: int | None = ACTIONS_APP_ID,
) -> Sweep:
    """Publish for the open PRs one sweep covers (all of them without a budget, see ``select_prs``).

    One PR failing never stops the others; it is reported as ``failed``. When the client knows how many requests it has
    left and that falls below ``reserve``, the PRs not started yet are reported as ``skipped``, not failed: running
    out of quota must not turn a sweep red or leave a half-written state, and the next sweep picks them up.

    The reserve is approximate: with several workers, a few can read the same remaining count before any of them has
    spent requests, so a sweep can overshoot it by about ``workers`` times the cost of one PR (7 to 11 requests)."""
    reasons: dict[int, str] = {}
    if stale is not None:
        # Decide from one listing of every open PR with its latest verdict, and read only the ones that need it.
        if recent is not None or rotate is not None:
            raise ValueError("a stale-only sweep chooses its PRs itself; do not combine it with recent or rotate")
        standings = fetch_standings(client, repo, check_name, verdict_app_id)
        open_prs = len(standings)
        chosen = select_stale(standings, now or datetime.now(UTC), stale)[: None if limit is None else max(0, limit)]
        reasons = {s.number: why for s, why in chosen}
        prs = [{"number": s.number, "head": {"sha": s.head_sha}} for s, _ in chosen]
    else:
        prs = list_open_prs(client, repo, include_drafts=True)
        open_prs = len(prs)
        prs = select_prs(prs, recent=recent, rotate=rotate, tick=tick, limit=limit)

    def one(pr: dict[str, Any]) -> Outcome:
        number = pr["number"]
        left = rate_remaining(client)
        if reserve and left is not None and left < reserve:
            sha = str((pr.get("head") or {}).get("sha", ""))
            return Outcome(number, sha, State.PLANNER_ERROR, "", None, "skipped", f"only {left} requests left")
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
                org_client=org_client,
                refresh=stale is not None,
            )
        except (GitHubError, KeyError, ValueError) as exc:
            sha = str((pr.get("head") or {}).get("sha", ""))
            return Outcome(number, sha, State.PLANNER_ERROR, "", None, "failed", str(exc))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return Sweep(pool.map(one, prs), open_prs, reasons)


def describe(outcomes: Sequence[Outcome]) -> str:
    reasons = outcomes.reasons if isinstance(outcomes, Sweep) else {}
    lines = [
        f"PR #{o.pr}: skipped ({o.detail})"
        if o.action == "skipped"
        else f"PR #{o.pr}: {o.action} {o.conclusion or o.status} ({o.state.value})"
        + (f" {o.detail}" if o.action == "failed" else "")
        + (f" [{reasons[o.pr]}]" if o.pr in reasons else "")
        for o in outcomes
    ]
    failed = sum(o.action == "failed" for o in outcomes)
    skipped = sum(o.action == "skipped" for o in outcomes)
    summary = f"{len(outcomes)} PRs, {failed} failed"
    if skipped:
        summary += f", {skipped} skipped for the request reserve"
    if isinstance(outcomes, Sweep) and outcomes.reasons:
        summary += f" (stale-only: {len(outcomes)} of {outcomes.open_prs} open PRs needed a new verdict)"
    elif isinstance(outcomes, Sweep) and outcomes.open_prs != len(outcomes):
        summary += f" (this sweep covers {len(outcomes)} of {outcomes.open_prs} open PRs)"
    lines.append(summary)
    return "\n".join(lines) + "\n"
