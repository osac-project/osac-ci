"""The jobs of the cheap stage of a pull request, read from its check runs: the legs of the repository's own workflows
(``CI``, say), each with where it stands and whether it can be run again.

The status comment lists them next to the E2E suites, and a ``/test <job>`` comment runs a failed one again. Nothing
here starts anything: it reads. A job is a check run of the head commit written by GitHub Actions, and for Actions its
id is the id of the job, so a check run is enough to name the job, link to it and ask for it to be run again; the
workflow it belongs to is found through the run its link points into (the run, not the commit, says the workflow's
name: a ``pull_request_target`` run is tied to the base commit and is not found by the head commit). Jobs that were
skipped say nothing and are left out.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get, paginate

ACTIONS_APP = "github-actions"
RUN_IN_URL = re.compile(r"/actions/runs/(\d+)")
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
IGNORED_CONCLUSIONS = frozenset({"skipped", "neutral", "stale"})
NAME_MAX = 100  # the longest name of a workflow the repository may list
COMMAND_PREFIX = "ci-"  # a job is asked for as ``/test ci-<its name>``: never a suite id of its own
COMMAND_MAX = 40  # the longest id a ``/test`` comment takes


@dataclass(frozen=True)
class CheapJob:
    """A job of a cheap workflow on the head commit. ``state`` is active | passed | failed | cancelled; ``run_done`` is
    whether its run has ended, the only time one job of it can be run again."""

    name: str
    state: str
    url: str = ""
    job_id: int = 0
    run_id: int = 0
    run_done: bool = False
    running: bool = False  # active, and started (else it waits for a runner)
    workflow: str = ""
    command: str = ""  # what follows ``/test`` to run it again; empty when no name of it is unambiguous

    @property
    def can_run_again(self) -> bool:
        return self.run_done and self.state in ("failed", "cancelled") and self.job_id > 0 and bool(self.command)


def parse_workflows(text: str) -> tuple[str, ...]:
    """The workflows whose jobs are listed, as the command line gives them: a JSON list of names (``[]`` or empty:
    none). Each name is checked: it is compared with names GitHub gives, so it must be one short printable line."""
    if not text.strip():
        return ()
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"the cheap workflows must be a JSON list of names: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError("the cheap workflows must be a JSON list of names")
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip() or len(item) > NAME_MAX or not item.isprintable():
            raise ValueError(f"invalid workflow name: {item!r}")
        names.append(item)
    return tuple(dict.fromkeys(names))


def command_of(name: str) -> str:
    """The id a ``/test`` comment names the job by (``Lint / Run linters`` -> ``ci-lint-run-linters``): lower case and
    dashes, within the length a comment takes; empty when nothing of the name is left."""
    words = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[: COMMAND_MAX - len(COMMAND_PREFIX)].strip("-")
    return COMMAND_PREFIX + words if words else ""


def _state(check: Mapping[str, object]) -> str | None:
    """active | passed | failed | cancelled for a check run; None for one that says nothing (skipped, neutral)."""
    if check.get("status") != "completed":
        return "active"
    conclusion = str(check.get("conclusion") or "")
    if conclusion == "success":
        return "passed"
    if conclusion in FAILED_CONCLUSIONS:
        return "failed"
    if conclusion == "cancelled":
        return "cancelled"
    return None if conclusion in IGNORED_CONCLUSIONS or not conclusion else "failed"


def _run_of(check: Mapping[str, object]) -> int | None:
    found = RUN_IN_URL.search(str(check.get("details_url") or check.get("html_url") or ""))
    return int(found.group(1)) if found else None


def read_cheap_jobs(
    client: GitHubClient, repo: str, checks: Sequence[Mapping[str, object]], workflows: Sequence[str]
) -> tuple[CheapJob, ...]:
    """The jobs of the newest run of each of ``workflows`` among the check runs of the head commit. A run that cannot be
    read is left out (the status then shows less, never something wrong)."""
    if not workflows:
        return ()
    repo = check_repo(repo)
    by_run: dict[int, list[Mapping[str, object]]] = {}
    for check in checks:
        app = check.get("app")
        if not isinstance(app, Mapping) or app.get("slug") != ACTIONS_APP:
            continue
        run = _run_of(check)
        if run is not None:
            by_run.setdefault(run, []).append(check)
    newest: dict[str, tuple[int, int, bool]] = {}  # workflow name -> (run number, run id, run is done)
    names: dict[int, str] | None = None  # workflow id -> its name, read once and only when a run is found
    for run in by_run:
        try:
            info = get(client, f"/repos/{repo}/actions/runs/{run}")
            # The name of a run is its ``run-name`` when the workflow gives one, so the workflow is named by its id.
            if names is None:
                names = {
                    int(w["id"]): str(w["name"])
                    for w in paginate(client, f"/repos/{repo}/actions/workflows", key="workflows")
                }
        except GitHubError:
            continue
        workflow_id = info.get("workflow_id")
        name = names.get(workflow_id, "") if isinstance(workflow_id, int) else ""
        number = info.get("run_number")
        if name not in workflows or not isinstance(number, int):
            continue
        if name not in newest or number > newest[name][0]:
            newest[name] = (number, run, info.get("status") == "completed")
    jobs: list[CheapJob] = []
    for workflow, (_, run, done) in newest.items():
        for check in by_run[run]:
            state = _state(check)
            job_id = check.get("id")
            if state is None or not isinstance(job_id, int):
                continue
            jobs.append(
                CheapJob(
                    name=str(check.get("name") or ""),
                    state=state,
                    url=str(check.get("html_url") or check.get("details_url") or ""),
                    job_id=job_id,
                    run_id=run,
                    run_done=done,
                    running=state == "active" and check.get("status") == "in_progress",
                    workflow=workflow,
                )
            )
    # A command that two jobs share (names that differ only in punctuation, or in a long tail) names neither of them.
    taken = Counter(command_of(j.name) for j in jobs)
    jobs = [replace(j, command=c) if (c := command_of(j.name)) and taken[c] == 1 else j for j in jobs]
    order = {"failed": 0, "active": 1, "cancelled": 2, "passed": 3}
    return tuple(sorted(jobs, key=lambda j: (order[j.state], j.workflow, j.name)))


HEAD_SHA = re.compile(r"[0-9a-f]{40}")


def rerun_jobs(
    client: GitHubClient, repo: str, sha: str, job_ids: Sequence[int], workflows: Sequence[str]
) -> list[tuple[str, bool, str]]:
    """Run each of ``job_ids`` again (GitHub runs again the job and the jobs that wait for it, in the run it belongs
    to). Nothing in the request is believed: each id must still be a failed or cancelled job of a finished run of a
    listed workflow on ``sha``, read now, so an id that is not (or a job somebody has already started again) is
    refused. Returns (job, started, what happened) for each."""
    repo = check_repo(repo)
    if not HEAD_SHA.fullmatch(sha):
        raise ValueError(f"invalid commit: {sha!r}")
    if any(isinstance(i, bool) or not isinstance(i, int) or i < 1 for i in job_ids):
        raise ValueError("the jobs must be a list of ids")
    checks = list(paginate(client, f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs"))
    can = {j.job_id: j for j in read_cheap_jobs(client, repo, checks, workflows) if j.can_run_again}
    out: list[tuple[str, bool, str]] = []
    for job_id in dict.fromkeys(job_ids):
        job = can.get(job_id)
        if job is None:
            out.append((str(job_id), False, "is not a failed job of a finished run of a listed workflow: left alone"))
            continue
        response = client.request("POST", f"/repos/{repo}/actions/jobs/{job_id}/rerun")
        if 200 <= response.status < 300:
            out.append((job.name, True, "run again"))
        else:
            message = str(response.data.get("message", "")) if isinstance(response.data, Mapping) else ""
            out.append(
                (job.name, False, f"could not be run again ({response.status}{': ' + message if message else ''})")
            )
    return out
