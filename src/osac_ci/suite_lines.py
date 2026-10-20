"""The suite lines of a pull request, when they are commit statuses.

A suite's line on the pull request ("E2E / VMaaS Full Install") is either a check run of the OSAC CI app or a commit
status. A status has one advantage: its row on the pull request links straight to the job (a click on the name opens
the logs), where the row of a check run opens the Checks tab first, and GitHub does not let an app change that link.
It has two costs: a running suite shows a dot, not a spinner, and the row has no app icon.

Everything that decides from the lines (the decision, the control comment, the sum of a run started by hand) reads
check runs. So that none of it changes, ``suite_line_runs`` reads the statuses of the suites' lines and hands them back
in the shape of check runs of the app: pending is a run in progress, success, failure and error (a cancelled suite) are
completed runs that passed, failed or were cancelled, and the link of the status is the details link. The older lines
stay readable: when a commit has both, the newest of a name wins, as it always did.

Only the app's own statuses are lines. Any workflow of the repository can write a status as the GitHub Actions bot, and
a line that says "passed" is what the decision believes (a suite is not run again) and what the sum of a run started by
hand turns into a green ``E2E`` check of the app, so a status of the Actions bot, or of anyone else, is never one.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from osac_ci.github.api import GitHubClient, GitHubError, check_repo, paginate
from osac_ci.github.snapshot import COMMIT_SHA

# Who wrote a status of the E2E flow: the OSAC CI app, or a workflow's built-in token (the first way the statuses were
# written). Used to find the newest ``E2E`` status of a run (``e2e_check.newest_status``), which only says which run
# owns it.
STATUS_CREATORS = ("osac-ci[bot]", "github-actions[bot]")
# Who may have written a suite line as a status, as something the decision and the sum of a run by hand believe: the
# OSAC CI app alone.
SUITE_LINE_CREATORS = ("osac-ci[bot]",)
CONCLUSIONS = {"success": "success", "failure": "failure", "error": "cancelled"}


def suite_line_runs(
    client: GitHubClient, repo: str, sha: str, contexts: Iterable[str], app: str
) -> list[dict[str, Any]]:
    """The statuses of the commit named in ``contexts`` and written by the app, as check runs of ``app``, oldest
    first (so that, of two with the same time, the later one is the newer, as ``latest_checks`` reads them)."""
    wanted = set(contexts)
    if not wanted:
        return []
    if not COMMIT_SHA.fullmatch(sha):
        raise ValueError(f"invalid commit id: {sha!r}")
    runs: list[dict[str, Any]] = []
    for status in paginate(client, f"/repos/{check_repo(repo)}/commits/{sha}/statuses"):  # newest first
        context = status.get("context")
        if context not in wanted or (status.get("creator") or {}).get("login") not in SUITE_LINE_CREATORS:
            continue
        state = str(status.get("state"))
        link = str(status.get("target_url") or "")
        runs.append(
            {
                "name": context,
                "status": "completed" if state in CONCLUSIONS else "in_progress",
                "conclusion": CONCLUSIONS.get(state),
                "started_at": status.get("created_at"),
                "app": {"slug": app},
                "details_url": link,
                "html_url": link,
                "output": {"title": str(status.get("description") or "")},
            }
        )
    runs.reverse()
    return runs


def suite_line_runs_if_readable(
    client: GitHubClient, repo: str, sha: str, contexts: Iterable[str], app: str
) -> list[dict[str, Any]]:
    """``suite_line_runs``, but a token that may not read commit statuses (403, 404) gives no lines instead of an error.
    For a reader whose lines are check runs (the default), where the statuses are not needed and a missing permission
    must not break it."""
    try:
        return suite_line_runs(client, repo, sha, contexts, app)
    except GitHubError as exc:
        if exc.status in (401, 403, 404):
            return []
        raise
