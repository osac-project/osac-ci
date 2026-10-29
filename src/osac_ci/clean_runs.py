"""Delete the finished runs of a repository's service workflows that did nothing.

The workflows that look after pull requests (the E2E decision, the early comment, the review relay) wake up on every
comment, review and CI event in the repository, and most wake-ups have nothing to do: the run ends *skipped*. They
fill the Actions tab of a repository whose people never asked for them. A workflow in this repository (osac-ci) deletes
them shortly after, from here, so the product repository's list shows the runs that did something.

Only a run that ended ``skipped`` is deleted, and only after a few minutes: a run that failed, was cancelled or passed
is evidence and stays, and a run that is still going is never touched. A run that cannot be dated is left alone.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import islice

from osac_ci.github.api import GitHubClient, GitHubError, check_repo, paginate

WORKFLOW_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml")  # a file name of .github/workflows, never a path
MIN_AGE_MINUTES = 5  # a skipped run is shown this long, so a person who just commented can still see that it was heard
MAX_RUNS = 500  # per workflow and sweep: the next sweep takes the rest


@dataclass(frozen=True)
class Cleaned:
    """What a sweep did for one workflow of one repository."""

    repo: str
    workflow: str
    found: int = 0  # skipped runs old enough to delete
    deleted: int = 0
    failed: tuple[int, ...] = field(default=())  # run ids GitHub refused to delete
    missing: bool = False  # the repository has no such workflow file: nothing to do


def deletable(run: Mapping[str, object], now: datetime, min_age_minutes: int) -> bool:
    """A finished run that was skipped and has been so for at least ``min_age_minutes``."""
    if run.get("status") != "completed" or run.get("conclusion") != "skipped":
        return False
    try:
        updated = datetime.fromisoformat(str(run["updated_at"]))
    except (KeyError, ValueError):
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    return now - updated >= timedelta(minutes=min_age_minutes)


def clean(
    client: GitHubClient,
    repo: str,
    workflows: list[str],
    *,
    now: datetime | None = None,
    min_age_minutes: int = MIN_AGE_MINUTES,
    dry_run: bool = False,
) -> list[Cleaned]:
    """Delete the skipped runs of each workflow file of ``repo``. Returns what was done, one entry per workflow."""
    repo = check_repo(repo)
    if min_age_minutes < 1:
        raise ValueError("the minimum age must be at least one minute")
    for name in workflows:
        if not WORKFLOW_FILE.fullmatch(name):
            raise ValueError(f"not a workflow file name: {name!r}")
    now = now or datetime.now(UTC)
    done: list[Cleaned] = []
    for name in workflows:
        try:
            runs = list(
                islice(
                    paginate(
                        client,
                        f"/repos/{repo}/actions/workflows/{name}/runs",
                        key="workflow_runs",
                        params={"status": "skipped"},
                    ),
                    MAX_RUNS,
                )
            )
        except GitHubError as exc:
            if exc.status == 404:  # the repository has no workflow of that name
                done.append(Cleaned(repo, name, missing=True))
                continue
            raise
        old = [int(r["id"]) for r in runs if isinstance(r, Mapping) and deletable(r, now, min_age_minutes)]
        failed: list[int] = []
        deleted = 0
        for run_id in old:
            if dry_run:
                continue
            response = client.request("DELETE", f"/repos/{repo}/actions/runs/{run_id}")
            if response.status in (204, 404):  # deleted, or already gone
                deleted += 1
            else:
                failed.append(run_id)
        done.append(Cleaned(repo, name, len(old), deleted, tuple(failed)))
    return done


def render(results: list[Cleaned], *, dry_run: bool) -> str:
    lines = []
    for r in results:
        if r.missing:
            lines.append(f"{r.repo}: no workflow {r.workflow}")
        elif dry_run:
            lines.append(f"{r.repo}: {r.workflow}: {r.found} skipped run(s) would be deleted")
        else:
            lines.append(
                f"{r.repo}: {r.workflow}: deleted {r.deleted} of {r.found} skipped run(s)"
                + (f", {len(r.failed)} refused" if r.failed else "")
            )
    return "\n".join(lines) + "\n"


START_TITLE = re.compile(
    r"Autopilot #(?P<number>[0-9]+) \("
)  # the title the caller gives its run for a pull request event


def clean_start_runs(
    client: GitHubClient, repo: str, workflow: str, number: int, *, keep_failed: bool = True
) -> list[int]:
    """Delete the finished runs of the entry workflow that were started by an event of pull request ``number`` (titled
    ``Autopilot #<number> (<action>) · <branch>``). They did their work (the cheap jobs are started, the comment is
    written) and every one adds rows to the pull request page: the run that started the jobs, the comment, and the jobs
    of the other events as skipped. What the app wrote (the lines, the CI and E2E checks, the CI Autopilot summary)
    belongs to no run and stays. A run that failed stays, as evidence. Returns the ids deleted."""
    repo = check_repo(repo)
    if not WORKFLOW_FILE.fullmatch(workflow):
        raise ValueError(f"not a workflow file name: {workflow!r}")
    if number < 1:
        raise ValueError("the pull request number must be positive")
    gone: list[int] = []
    runs = paginate(
        client,
        f"/repos/{repo}/actions/workflows/{workflow}/runs",
        key="workflow_runs",
        params={"event": "pull_request_target", "status": "completed"},
    )
    for run in islice(runs, MAX_RUNS):
        if not isinstance(run, Mapping):
            continue
        found = START_TITLE.match(str(run.get("display_title") or ""))
        if found is None or int(found["number"]) != number:
            continue
        if keep_failed and run.get("conclusion") not in ("success", "skipped"):
            continue
        response = client.request("DELETE", f"/repos/{repo}/actions/runs/{int(run['id'])}")
        if response.status in (204, 404):
            gone.append(int(run["id"]))
    return gone
