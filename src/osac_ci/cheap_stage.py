"""The cheap stage of a pull request, started and followed by the OSAC CI app (the first piece of "Autopilot").

A policy job of kind ``cheap`` that names a ``workflow`` runs as a run of its own, like an E2E suite: the app starts it
with ``workflow_dispatch`` when a pull request opens or gets a new commit, writes its line (``CI / lint``) on the pull
request, follows the run to its end, and adds the lines up into one check (``CI``, or the policy's ``ci_check``).
Nothing of the pull request runs in the controller's own job, the dispatched run is from the default branch, so the
repository's own ``pull_request`` workflow for the same checks is no longer needed and its rows (written by GitHub
Actions) are gone from the pull request: every line is the app's.

The code is the E2E dispatch with another name for the run (``CI job · <job> · #<pull request> · <commit>``) and a
simpler sum: no claim and no approval, the lines alone decide, so the check can be written by whoever sees a job start
or end. What the event says is not believed on its own (see ``ended``), exactly as for the suites.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from osac_ci.e2e_check import (
    SUITE_ID,
    WORKFLOW_FILE,
    _latest_lines,
    check_sha,
    describe,
    newest_status,
    post_status,
    run_of_url,
)
from osac_ci.e2e_dispatch import Started, dispatch_suites, verify_suite_inputs
from osac_ci.e2e_start import Suite, _needed, line_state
from osac_ci.e2e_suites_check import run_state
from osac_ci.github.api import GitHubClient, check_repo, get, paginate
from osac_ci.policy import Policy

RUN_NAME_PREFIX = "CI job · "
RUN_NAME = re.compile(r"CI job · (?P<job>[a-z0-9][a-z0-9_-]{0,39}) · #(?P<number>[0-9]+) · (?P<sha>[0-9a-f]{40})")
DEFAULT_SUM = "CI"
NOT_AFFECTED = "No cheap job applies to this change"


@dataclass(frozen=True)
class Outcome:
    """What ``start`` did: the runs it started, the jobs it could not start, and what the sum says."""

    started: tuple[Started, ...]
    failed: tuple[str, ...]
    sum: str


def run_name(job: str, number: int, sha: str) -> str:
    """What the job's workflow names its run (its ``run-name``), for the tests and the docs."""
    return f"{RUN_NAME_PREFIX}{job} · #{number} · {sha}"


def read_run_name(title: str) -> tuple[str, int, str] | None:
    """(job, pull request, commit) of a run named by ``run_name``; None for any other title."""
    found = RUN_NAME.fullmatch(title)
    return (found["job"], int(found["number"]), found["sha"]) if found else None


def jobs_of(policy: Policy) -> tuple[Suite, ...]:
    """The cheap jobs of the policy that run as a run of their own (the ones that name a ``workflow``), in the shape the
    suites have, so the same rules choose them."""
    return tuple(
        Suite(name, job.check, job.filters, job.filters_any, False, "", "", "", job.workflow)
        for name, job in policy.jobs.items()
        if job.kind == "cheap" and job.workflow
    )


def sum_name(policy: Policy) -> str:
    return policy.ci_check or DEFAULT_SUM


def needed_jobs(policy: Policy, files: Sequence[str]) -> tuple[Suite, ...]:
    """The jobs a change to these files needs. No files means nothing is known, so no job is skipped."""
    jobs = jobs_of(policy)
    return jobs if not files else _needed(jobs, files, policy.path_filters.filters)


def _changed_files(client: GitHubClient, repo: str, number: int) -> list[str]:
    files: list[str] = []
    for raw in paginate(client, f"/repos/{repo}/pulls/{number}/files"):
        files.append(str(raw["filename"]))
        if raw.get("previous_filename"):
            files.append(str(raw["previous_filename"]))
    return files


def add_up(
    client: GitHubClient,
    repo: str,
    sha: str,
    lines: Mapping[str, str],
    context: str,
    run_url: str,
    kind: str = "checks",
) -> str:
    """The ``context`` check as the sum of the lines of the jobs the change needs: waiting while one goes or has not
    started, ``Failed`` when one failed and none goes, ``Passed`` when all passed. It is only written when it says
    something new, so many calls (each job's end) leave one line, not a pile of them."""
    if not lines:
        text, state = NOT_AFFECTED, "success"
    else:
        latest = _latest_lines(client, repo, sha, lines)
        states = {job: line_state(latest, line) for job, line in lines.items()}
        going = [job for job, s in states.items() if s in ("active", "none")]
        failed = [job for job, s in states.items() if s == "failed"]
        if going:
            text, state = f"Running: {', '.join(going)}", "pending"
        elif failed:
            text, state = f"Failed: {', '.join(failed)}", "failure"
        else:
            text, state = f"Passed: {', '.join(lines)}", "success"
    current = newest_status(client, repo, sha, context, kind)
    if current is not None and current.get("state") == state and current.get("description") == describe(text):
        return "unchanged"
    post_status(client, repo, sha, context, state, text, run_url, kind)
    return state


def start(
    client: GitHubClient,
    repo: str,
    number: int,
    policy: Policy,
    *,
    lines_kind: str = "statuses",
    run_url: str = "",
) -> Outcome:
    """A pull request opened or got a new commit: start the cheap jobs the change needs, write their lines as waiting,
    and write the sum as waiting for them. The commit, the repository that holds it and who wrote it are read from the
    pull request here, so what the jobs are told is never something the event said."""
    repo = check_repo(repo)
    pull = get(client, f"/repos/{repo}/pulls/{number}")
    if pull.get("state") != "open":
        return Outcome((), (), "the pull request is not open: nothing to do")
    head = pull.get("head") or {}
    sha = check_sha(str(head.get("sha", "")))
    head_repo = str((head.get("repo") or {}).get("full_name") or "")
    author, association = verify_suite_inputs(client, repo, number, sha, head_repo)
    needed = needed_jobs(policy, _changed_files(client, repo, number))
    context = sum_name(policy)
    if not needed:
        return Outcome((), (), add_up(client, repo, sha, {}, context, run_url, "checks"))
    items = [{"suite": j.name, "workflow": j.workflow, "check": j.check, "tier": ""} for j in needed]
    started, failed = dispatch_suites(
        client,
        repo,
        items,
        sha=sha,
        number=number,
        head_repo=head_repo,
        fork_author=author,
        fork_association=association,
        lines_kind=lines_kind,
        run_url=run_url,
    )
    lines = {j.name: j.check for j in needed}
    return Outcome(tuple(started), tuple(failed), add_up(client, repo, sha, lines, context, run_url, "checks"))


def ended(
    client: GitHubClient,
    repo: str,
    run_id: int,
    title: str,
    workflow_path: str,
    policy: Policy,
    *,
    lines_kind: str = "statuses",
) -> str:
    """A cheap job that ran as a run of its own has ended (the ``workflow_run`` event): its line gets the result and the
    sum is written again. The event is not believed on its own, since a person with write access can start the workflow
    with any inputs: the title must be the one the job's workflow gives its run, the workflow must be the one the policy
    names for that job, and the commit must still be the head of the pull request. A line that a newer run of the job
    owns is left alone."""
    repo = check_repo(repo)
    found = read_run_name(title)
    if found is None:
        return "not the run of a cheap job: nothing to do"
    job, number, sha = found
    file = workflow_path.rsplit("/", 1)[-1]
    mine = next((j for j in jobs_of(policy) if j.name == job and j.workflow == file), None)
    if mine is None or not WORKFLOW_FILE.fullmatch(file) or not SUITE_ID.fullmatch(job):
        return f"{file} is not the workflow of the cheap job {job} in the policy: nothing to do"
    pull = get(client, f"/repos/{repo}/pulls/{number}")
    if str((pull.get("head") or {}).get("sha", "")) != sha:
        return "the pull request has moved on to another commit: nothing to do"
    description, state, url, over = run_state(client, repo, run_id)
    if not over:
        return "the run has not ended: nothing to do"
    current = newest_status(client, repo, sha, mine.check, lines_kind)
    owner = run_of_url(str(current.get("target_url") or "")) if current is not None else None
    if owner is not None and owner != run_id:
        return "a newer run of the job owns its line: nothing to do"
    post_status(client, repo, sha, mine.check, state, description, url, lines_kind)
    needed = {j.name: j.check for j in needed_jobs(policy, _changed_files(client, repo, number))}
    needed.setdefault(mine.name, mine.check)
    return add_up(
        client, repo, sha, needed, sum_name(policy), f"https://github.com/{repo}/actions/runs/{run_id}", "checks"
    )
