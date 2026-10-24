"""Cancel the E2E run of an older commit of a pull request.

When a pull request gets a new commit, the E2E that is still running for an older one is of no use any more and holds a
runner. The run is found by what the older commit's own ``E2E`` line says: the decision that started the suites wrote
``Running: ...`` on the commit, with a link to its workflow run. That is the one place that ties a run to a commit, so a
run needs no commit in its title, and a title can be as short as it likes.

Only the last few older commits are looked at (a pull request does not have a long tail of running E2Es), only those
that come before the head commit in the pull request (a newer one is a newer head), and never a commit that belongs to
another open pull request too.

The suites that run as runs of their own (see ``e2e_dispatch``) are not linked from the ``E2E`` line once the run that
started them has ended, so they are found by their name instead: ``E2E suite · <suite> · #<pull request> · <commit>``.
"""

from __future__ import annotations

from collections.abc import Mapping

from osac_ci.e2e_check import CONTEXT, RUNNING_PREFIX, check_sha, newest_status, run_of_url
from osac_ci.e2e_dispatch import read_run_name
from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get, paginate

MAX_OLDER = 10  # how many older commits are looked at, the newest first


def _belongs_to_another_open_pull_request(client: GitHubClient, repo: str, sha: str, number: int) -> bool:
    pulls = get(client, f"/repos/{repo}/commits/{sha}/pulls")
    return any(isinstance(p, Mapping) and p.get("state") == "open" and p.get("number") != number for p in pulls)


def cancel_older(client: GitHubClient, repo: str, number: int, head_sha: str) -> list[tuple[str, int]]:
    """Cancel the runs that are running the E2E of an older commit of the pull request. Returns (commit, run id) of
    each run it asked GitHub to cancel."""
    repo = check_repo(repo)
    head_sha = check_sha(head_sha)
    commits = [
        str(c["sha"]) for c in paginate(client, f"/repos/{repo}/pulls/{int(number)}/commits") if isinstance(c, Mapping)
    ]
    if head_sha not in commits:
        return []  # the pull request no longer has this commit (a force push): nothing is "older" for certain
    # Only the commits before the head: a push may land while this decision runs, and the newer commit is the new head,
    # whose own decision may already be running its E2E.
    older = commits[: commits.index(head_sha)][-MAX_OLDER:]
    cancelled: list[tuple[str, int]] = []
    for sha in reversed(older):
        line = newest_status(client, repo, check_sha(sha), CONTEXT, "checks")
        if (
            line is None
            or line.get("state") != "pending"
            or not str(line.get("description", "")).startswith(RUNNING_PREFIX)
        ):
            continue  # nothing running for this commit (not started, waiting, or over)
        run = run_of_url(str(line.get("target_url") or ""))
        if run is None or _belongs_to_another_open_pull_request(client, repo, sha, int(number)):
            continue
        response = client.request("POST", f"/repos/{repo}/actions/runs/{run}/cancel")
        if response.status not in (202, 404, 409):  # accepted; already gone; already finished
            raise GitHubError(response.status, f"cancelling run {run}")
        cancelled.append((sha, run))
    cancelled += _cancel_suite_runs(client, repo, int(number), set(older))
    return cancelled


def _cancel_suite_runs(client: GitHubClient, repo: str, number: int, older: set[str]) -> list[tuple[str, int]]:
    """Cancel the suites of the pull request that were started as runs of their own for one of the older commits and
    have not ended (queued or running). Found by the name of the run, which the suite's workflow sets from its inputs
    (the same pull request and commit that the run was started for)."""
    cancelled: list[tuple[str, int]] = []
    shared: dict[str, bool] = {}
    for status in ("queued", "in_progress"):
        runs = paginate(
            client,
            f"/repos/{repo}/actions/runs",
            key="workflow_runs",
            params={"event": "workflow_dispatch", "status": status},
        )
        for run in runs:
            named = read_run_name(str(run.get("display_title") or "")) if isinstance(run, Mapping) else None
            if named is None or named[1] != number or named[2] not in older or not isinstance(run.get("id"), int):
                continue
            sha = named[2]
            if sha not in shared:
                shared[sha] = _belongs_to_another_open_pull_request(client, repo, sha, number)
            if shared[sha]:
                continue
            response = client.request("POST", f"/repos/{repo}/actions/runs/{run['id']}/cancel")
            if response.status not in (202, 404, 409):
                raise GitHubError(response.status, f"cancelling run {run['id']}")
            cancelled.append((sha, int(run["id"])))
    return cancelled
