"""The summary check of the controller: one check run of the OSAC CI app, ``CI Autopilot``, on the head commit of a pull
request, that says what the controller has done and where each job stands.

A row that comes from a workflow job is always GitHub Actions' (its icon, its name), and it goes away with the run; a
check run the app writes has the app's icon and stays. The controller updates this one whenever something happens (the
cheap jobs start, one ends, the decision says what the E2E does, a suite ends): the table is the state of every line of
the policy now, and the events are what happened, newest first, so a person reading the check sees the whole story
without opening a log. It is the gate of the pull request, in step with the lines: in progress until everything the
policy has has passed (a waiting E2E, a missing approval, a job that goes), a failure when anything failed, a success
only when all passed. It is never neutral, which a required check would count as passed. A repository can require this
check alone.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from osac_ci.cheap_stage import jobs_of, sum_name
from osac_ci.e2e_check import _latest_lines, check_sha, describe, newest_status
from osac_ci.e2e_start import line_state, suites_of
from osac_ci.fast_mark import FastTrack, decode, encode
from osac_ci.github.api import GitHubClient, check_repo, get, paginate
from osac_ci.policy import Policy

NAME = "CI Autopilot"
E2E_SUM = "E2E"
EVENTS_KEPT = 12
EVENT_MAX = 300
EVENTS_MARK = "#### Events"
EVENT_LINE = re.compile(r"^- (\d\d:\d\d:\d\d) UTC · (.*)$")
ICONS = {"passed": "✅", "failed": "❌", "active": "⏳", "none": "▫️"}


def stamp(now: datetime | None = None) -> str:
    """The time of an event, as the events of the check write it."""
    return (now or datetime.now(UTC)).strftime("%H:%M:%S")


def _row(name: str, state: str, text: str, url: str) -> str:
    shown = f"[{text}]({url})" if url and text else (text or "not started")
    return f"| {ICONS.get(state, '▫️')} | {name} | {shown} |"


def _previous_events(existing: Mapping[str, Any] | None) -> list[str]:
    output = (existing or {}).get("output")
    text = str(output.get("text") or "") if isinstance(output, Mapping) else ""
    return [line for line in text.splitlines() if EVENT_LINE.match(line)]


def _sum_text(sums: Mapping[str, Mapping[str, Any] | None], name: str) -> tuple[str, str]:
    found = sums.get(name)
    if found is None:
        return "not written yet", "none"
    state = {"success": "passed", "failure": "failed"}.get(str(found.get("state")), "active")
    return str(found.get("description") or "") or state, state


def build(
    client: GitHubClient,
    repo: str,
    sha: str,
    policy: Policy,
    event: str,
    previous: Mapping[str, Any] | None,
    now: datetime | None = None,
    fast: FastTrack | None = None,
) -> dict[str, Any]:
    """The body of the check run: title, summary and events, and the status and conclusion that go with them. With a
    fast track (a person with the right skipped every test of this commit) the gate passes whatever the lines say, and
    says who did it and why."""
    cheap = {j.name: j.check for j in jobs_of(policy)}
    suites = {s.name: s.check for s in suites_of(policy) if not s.manual_only and not s.variant_of}
    jira = policy.jira_reference
    lines = [*cheap.values(), *suites.values(), *([jira.check] if jira else [])]
    latest = _latest_lines(client, repo, sha, {str(i): c for i, c in enumerate(lines)}) if lines else {}
    rows: list[str] = []
    states: list[str] = []
    for check in lines:
        state = line_state(latest, check)
        run = latest.get(check)
        rows.append(_row(check, state, run.title if run is not None else "", run.url if run is not None else ""))
        states.append(state)
    ci_name = sum_name(policy)
    sums = {n: newest_status(client, repo, sha, n, "checks") for n in (ci_name, E2E_SUM)}
    ci_text, ci_state = _sum_text(sums, ci_name)
    e2e_text, e2e_state = _sum_text(sums, E2E_SUM)
    # The check is the gate of the pull request: a required check that is not settled keeps it from merging, and a
    # neutral one would let it through, so nothing but a pass of everything the policy has is a success. What the
    # policy does not have (no cheap job, no suite) is not waited for.
    parts = [ci_state] if cheap else []
    parts += [e2e_state] if suites else []
    if jira:  # a line the controller writes itself: not written yet counts as not passed
        parts.append(line_state(latest, jira.check))
    if fast is not None:
        status, conclusion = "completed", "success"
    elif "failed" in states or "failed" in parts:
        status, conclusion = "completed", "failure"
    elif "active" in states or any(p != "passed" for p in parts):
        status, conclusion = "in_progress", None
    else:
        status, conclusion = "completed", "success"
    stamp = (now or datetime.now(UTC)).strftime("%H:%M:%S")
    events = _previous_events(previous)
    fresh = f"- {stamp} UTC · {' '.join(event.split())[:EVENT_MAX]}"
    if event.strip() and not (events and events[0][len("- 00:00:00 UTC · ") :] == fresh[len("- 00:00:00 UTC · ") :]):
        events.insert(0, fresh)
    events = events[:EVENTS_KEPT]
    title = describe(fast.title if fast is not None else f"{ci_name}: {ci_text} · {E2E_SUM}: {e2e_text}")
    banner = [f"> **Fast track** by @{fast.login}: {fast.reason}", ""] if fast is not None else []
    table = ["| | Line | State |", "|---|---|---|", *rows] if rows else ["No job of the policy has a line yet."]
    summary = "\n".join(
        [
            f"### {NAME} · `{sha[:7]}`",
            "",
            *banner,
            f"**{ci_name}:** {ci_text}  ",
            f"**{E2E_SUM}:** {e2e_text}",
            "",
            *table,
        ]
    )
    text = "\n".join([EVENTS_MARK, "", *events]) if events else ""
    if fast is not None:
        text = "\n".join([encode(fast), "", text]).rstrip("\n")
    return {"status": status, "conclusion": conclusion, "title": title, "summary": summary, "text": text}


def _write(
    client: GitHubClient, repo: str, sha: str, body: Mapping[str, Any], run_url: str, existing: Mapping[str, Any] | None
) -> str:
    payload: dict[str, Any] = {
        "status": body["status"],
        "output": {"title": body["title"], "summary": body["summary"], "text": body["text"]},
    }
    if body["conclusion"]:
        payload["conclusion"] = body["conclusion"]
    if run_url:
        payload["details_url"] = run_url
    if existing is None:
        payload = {"name": NAME, "head_sha": sha, **payload}
        response = client.request("POST", f"/repos/{repo}/check-runs", body=payload)
        verb = "created"
    else:
        response = client.request("PATCH", f"/repos/{repo}/check-runs/{int(existing['id'])}", body=payload)
        verb = "updated"
    if response.status not in (200, 201):
        message = response.data.get("message", "") if isinstance(response.data, dict) else ""
        raise ValueError(f"writing the {NAME} check failed ({response.status}): {message}")
    return verb


def gate_of(client: GitHubClient, repo: str, sha: str) -> Mapping[str, Any] | None:
    """The newest ``CI Autopilot`` check run of the OSAC CI app on the commit (None if the app wrote none)."""
    found = None
    for run in paginate(
        client, f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs", params={"check_name": NAME}
    ):
        mine = run.get("name") == NAME and (run.get("app") or {}).get("slug") == "osac-ci"
        if mine and (found is None or int(run["id"]) > int(found["id"])):
            found = run
    return found


def fast_track_of(run: Mapping[str, Any] | None, sha: str) -> FastTrack | None:
    """The fast track written into a gate check run for this commit, if any (see fast_mark.py)."""
    output = (run or {}).get("output")
    return decode(str(output.get("text") or ""), sha) if isinstance(output, Mapping) else None


def update(
    client: GitHubClient,
    repo: str,
    number: int,
    policy: Policy,
    event: str,
    run_url: str = "",
    now: datetime | None = None,
    fast: FastTrack | None = None,
) -> str:
    """Say what just happened on the pull request: read the lines, add the event, and write the check. Closed or missing
    pull requests get nothing. A fast track that the check already carries for this commit is kept; ``fast`` makes a new
    one."""
    repo = check_repo(repo)
    pull = get(client, f"/repos/{repo}/pulls/{number}")
    if pull.get("state") != "open":
        return "the pull request is not open: nothing to do"
    head = pull.get("head")
    sha = check_sha(str(head.get("sha", ""))) if isinstance(head, Mapping) else ""
    existing = gate_of(client, repo, sha)
    body = build(client, repo, sha, policy, event, existing, now, fast or fast_track_of(existing, sha))
    # GitHub does not reopen a check run that was completed: a check that was settled and goes again (a job run again, a
    # suite started after CI passed) is written as a new run, and the newest of the name is the one the page shows.
    reopened = existing is not None and existing.get("status") == "completed" and body["status"] != "completed"
    return _write(client, repo, sha, body, run_url, None if reopened else existing)
