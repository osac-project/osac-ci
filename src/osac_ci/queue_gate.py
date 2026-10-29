"""The ``CI Autopilot`` check of a merge queue entry.

A queue entry is a commit GitHub builds from the pull request on top of the latest base. The jobs that test it (the
repository's cheap jobs and the E2E suites) run inline in one run of the entry workflow, and one job, ``result`` (see
queue_guard.py), watches them. This module writes what that job learns as the gate of the entry: the same check,
with the same name, the same icon and the same table as on a pull request, so a ruleset can require ``CI Autopilot``
alone on both. It starts in progress when the plan has chosen the jobs, and is settled by the first failure (which
names what failed) or by every job passing; a cancelled entry (a newer one replaced it) is closed as cancelled.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from osac_ci import autopilot_summary
from osac_ci.e2e_check import SUITE_ID, check_sha, describe
from osac_ci.e2e_suites_check import suite_progress
from osac_ci.github.api import GitHubClient, check_repo, paginate

RESULTS = ("start", "success", "failure", "cancelled")
ICONS = {"passed": "✅", "failed": "❌", "running": "⏳", "waiting": "▫️", "cancelled": "⛔", "skipped": "▫️"}
WORDS = {"waiting": "waiting", "running": "running", "passed": "passed", "failed": "failed", "cancelled": "cancelled"}


def _body(
    client: GitHubClient,
    repo: str,
    sha: str,
    run_id: int,
    jobs: list[str],
    result: str,
    detail: str,
    previous: Mapping[str, Any] | None,
    run_url: str,
) -> dict[str, Any]:
    progress = suite_progress(client, repo, run_id, jobs) if jobs else {}
    rows = [f"| {ICONS.get(state, '▫️')} | {job} | {WORDS.get(state, state)} |" for job, state in progress.items()]
    named = f" · {detail}" if detail else ""
    if result == "start":
        status, conclusion, said = "in_progress", None, f"queue entry: testing {', '.join(jobs) or 'nothing'}"
        title = describe(f"Testing the queue entry: {', '.join(jobs) or 'nothing selected'}")
    elif result == "success":
        status, conclusion, said = "completed", "success", "queue entry passed"
        title = describe(f"Passed: {', '.join(jobs)}" if jobs else "Passed")
    elif result == "failure":
        status, conclusion, said = "completed", "failure", f"queue entry failed{named}"
        title = describe(f"Failed: {detail}" if detail else "Failed")
    else:
        status, conclusion, said = "completed", "cancelled", f"queue entry cancelled{named}"
        title = describe(f"Cancelled: {detail}" if detail else "Cancelled")
    events = autopilot_summary._previous_events(previous)
    line = f"- {autopilot_summary.stamp()} UTC · {said}"
    events = [line, *events][: autopilot_summary.EVENTS_KEPT]
    table = ["| | Job | State |", "|---|---|---|", *rows] if rows else ["No job was selected for this entry."]
    summary = "\n".join([f"### {autopilot_summary.NAME} · queue entry `{sha[:7]}`", "", *table])
    text = "\n".join([autopilot_summary.EVENTS_MARK, "", *events])
    return {"status": status, "conclusion": conclusion, "title": title, "summary": summary, "text": text}


def write(
    client: GitHubClient,
    repo: str,
    sha: str,
    run_id: int,
    jobs: list[str],
    result: str,
    detail: str = "",
    run_url: str = "",
) -> str:
    """Write the gate of the queue entry for ``result``: ``start`` (in progress), ``success``, ``failure`` (naming
    ``detail``) or ``cancelled``. A gate that already settled and goes again is written as a new run (GitHub does not
    reopen a completed check run); otherwise the entry's gate is updated."""
    repo, sha = check_repo(repo), check_sha(sha)
    if result not in RESULTS:
        raise ValueError(f"result must be one of {', '.join(RESULTS)}")
    if any(not SUITE_ID.fullmatch(j) for j in jobs):
        raise ValueError("the jobs must be a list of ids")
    existing = None
    runs = paginate(
        client,
        f"/repos/{repo}/commits/{sha}/check-runs",
        key="check_runs",
        params={"check_name": autopilot_summary.NAME},
    )
    for run in runs:
        mine = run.get("name") == autopilot_summary.NAME and (run.get("app") or {}).get("slug") == "osac-ci"
        if mine and (existing is None or int(run["id"]) > int(existing["id"])):
            existing = run
    body = _body(client, repo, sha, run_id, jobs, result, detail, existing, run_url)
    reopened = existing is not None and existing.get("status") == "completed" and body["status"] != "completed"
    return autopilot_summary._write(client, repo, sha, body, run_url, None if reopened else existing)
