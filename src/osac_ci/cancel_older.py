"""Cancel the E2E run of an older commit of a pull request.

When a pull request gets a new commit, the E2E that is still running for an older one is of no use any more and holds a
runner. The run is found by what the older commit's own ``E2E`` line says: the decision that started the suites wrote
``Running: ...`` on the commit, with a link to its workflow run. That is the one place that ties a run to a commit, so a
run needs no commit in its title, and a title can be as short as it likes.

Only the last few older commits are looked at (a pull request does not have a long tail of running E2Es), and never a
commit that belongs to another open pull request too.
"""

from __future__ import annotations

from collections.abc import Mapping

from osac_ci.e2e_check import CONTEXT, RUNNING_PREFIX, check_sha, newest_status, run_of_url
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
    older = [sha for sha in commits if sha != head_sha][-MAX_OLDER:]
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
    return cancelled
