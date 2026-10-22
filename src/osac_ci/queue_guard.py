"""One job that looks after a merge queue entry: it watches what the entry is tested with, tells the queue the result
as soon as it is known, and stops what is still running.

A merge queue entry is tested by several workflow runs on the same commit: the E2E suites (the run this job is part of)
and the others, such as the unit and integration tests of ``CI`` and the Jira check. The merge rule needs the required
checks of all of them, so one that fails settles the entry, and what still runs for it (the other suites take about 100
minutes each, on real clusters) is wasted, while the entry keeps its place in the queue and everything behind it waits.

``watch`` looks at the suite jobs of this run and at the jobs of the other runs on the commit that belong to a workflow
providing a required check, and returns on the first failure. ``verdict`` says what the required ``E2E`` check should
be: ``failure`` naming the suites that failed (or ``cancelled`` when the cause was another workflow: ``E2E`` did not
fail, it was stopped), ``cancelled`` when the run was cancelled, ``success`` when every suite passed. ``stop_runs``
cancels every run of the commit that has not ended, this one last. The job writes the check before it stops anything,
so it does not wait for the cancelled suites to shut their clusters down.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass

from osac_ci.e2e_check import SUITE_ID
from osac_ci.e2e_suites_check import PASSED_CONCLUSIONS, suite_progress
from osac_ci.github.api import GitHubClient, GitHubError, check_repo, paginate

ACTIVE_RUNS = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})
DETAIL_MAX = 100


@dataclass(frozen=True)
class Verdict:
    result: str  # success | failure | cancelled
    detail: str  # what the check says after the result ("Failed: beta")
    stop: bool  # runs of the commit may still be going: stop them


def branch_name(ref: str) -> str:
    """The branch a merge group is built on, from ``github.event.merge_group.base_ref`` (``refs/heads/main``)."""
    return ref.removeprefix("refs/heads/")


def _runs_of(client: GitHubClient, repo: str, sha: str) -> list[Mapping[str, object]]:
    runs = paginate(
        client,
        f"/repos/{check_repo(repo)}/actions/runs",
        key="workflow_runs",
        params={"head_sha": sha, "event": "merge_group"},
    )
    return [r for r in runs if isinstance(r, Mapping)]


def _jobs_of(client: GitHubClient, repo: str, run_id: int) -> list[Mapping[str, object]]:
    jobs = paginate(
        client, f"/repos/{check_repo(repo)}/actions/runs/{int(run_id)}/jobs", key="jobs", params={"filter": "latest"}
    )
    return [j for j in jobs if isinstance(j, Mapping)]


def required_failures(client: GitHubClient, repo: str, sha: str, this_run: int, required: Collection[str]) -> list[str]:
    """The failed jobs of the other runs on the commit that belong to a workflow providing a required check (a run with
    a job named like a required check: ``CI``, ``jira-reference``). A failed job of such a run settles the entry
    whether or not the job is itself the required one, since the check is the sum of the run's jobs. Runs of workflows
    that provide no required check (CodeQL, say) are left alone, so an optional failure does not stop the entry."""
    found: list[str] = []
    for run in _runs_of(client, repo, sha):
        if run.get("id") == this_run:
            continue
        jobs = _jobs_of(client, repo, int(str(run.get("id"))))
        if not any(str(j.get("name")) in required for j in jobs):
            continue
        for job in jobs:
            if job.get("status") == "completed" and str(job.get("conclusion")) not in PASSED_CONCLUSIONS:
                found.append(f"{run.get('name')} / {job.get('name')}")
    return found


def _look(
    client: GitHubClient, repo: str, sha: str, run_id: int, suites: list[str], required: Collection[str]
) -> tuple[dict[str, str], list[str], list[str]]:
    progress = suite_progress(client, repo, run_id, suites) if suites else {}
    failed = [s for s, state in progress.items() if state == "failed"]
    return progress, failed, required_failures(client, repo, sha, run_id, required)


def watch(
    client: GitHubClient,
    repo: str,
    sha: str,
    run_id: int,
    suites: list[str],
    required: Collection[str],
    *,
    timeout: float = 14400,
    interval: float = 30,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Look until something settles the entry. ``failed``: a suite failed, or a required workflow of the commit has a
    failed job. ``done``: every suite has ended without a failure. ``timeout``: neither happened in time."""
    if any(not SUITE_ID.fullmatch(s) for s in suites):
        raise ValueError("the suites must be a list of suite ids")
    deadline = clock() + timeout
    while True:
        progress, failed, others = _look(client, repo, sha, run_id, suites, required)
        if failed or others:
            return "failed"
        if all(state in ("passed", "cancelled", "skipped") for state in progress.values()):
            return "done"
        if clock() >= deadline:
            return "timeout"
        sleep(interval)


def verdict_of(progress: Mapping[str, str], plan_result: str, others_failed: list[str], watch_outcome: str) -> Verdict:
    """What the required ``E2E`` check of the entry should say. ``watch_outcome`` is how the watch step ended:
    ``success`` (it returned), ``cancelled`` (the run is being cancelled), ``failure``, ``timeout`` or ``skipped``
    (there was nothing to watch)."""
    if plan_result == "cancelled":
        return Verdict("cancelled", "", False)
    if plan_result != "success":
        return Verdict("failure", "", False)  # no suite was selected: never let the entry pass untested
    failed = [s for s, state in progress.items() if state == "failed"]
    if failed:
        return Verdict("failure", ", ".join(failed)[:DETAIL_MAX], True)
    if others_failed:
        return Verdict("cancelled", f"stopped, {others_failed[0]} failed"[:DETAIL_MAX], True)
    everything = ", ".join(progress)
    if watch_outcome == "cancelled":
        if progress and all(state == "passed" for state in progress.values()):
            return Verdict("success", everything, False)
        return Verdict("cancelled", "", False)
    if watch_outcome == "timeout" and any(state in ("waiting", "running") for state in progress.values()):
        return Verdict("failure", "a suite did not end in time", True)
    if any(state == "cancelled" for state in progress.values()):
        return Verdict("cancelled", "", False)
    if any(state != "passed" for state in progress.values()):
        return Verdict("failure", everything, False)  # a selected suite did not run to success (it was skipped)
    return Verdict("success", everything, False)


def verdict(
    client: GitHubClient,
    repo: str,
    sha: str,
    run_id: int,
    suites: list[str],
    required: Collection[str],
    plan_result: str,
    watch_outcome: str,
) -> Verdict:
    """``verdict_of`` for the state of the entry now."""
    if plan_result != "success" or not suites:
        return verdict_of({}, plan_result, [], watch_outcome)
    progress, _, others = _look(client, repo, sha, run_id, suites, required)
    return verdict_of(progress, plan_result, others, watch_outcome)


def stop_runs(client: GitHubClient, repo: str, sha: str, this_run: int) -> list[int]:
    """Cancel every run of the commit that has not ended, the other ones first and this one last (cancelling it ends
    this job too). A run that ended in the meantime (409) is not an error. Returns the runs asked to stop."""
    runs = [r for r in _runs_of(client, repo, sha) if str(r.get("status")) in ACTIVE_RUNS]
    ids = [int(str(r["id"])) for r in runs if r.get("id") != this_run]
    if any(r.get("id") == this_run for r in runs):
        ids.append(this_run)
    for run_id in ids:
        response = client.request("POST", f"/repos/{check_repo(repo)}/actions/runs/{run_id}/cancel")
        if response.status not in (202, 409):
            raise GitHubError(response.status, f"cancelling run {run_id}")
    return ids
