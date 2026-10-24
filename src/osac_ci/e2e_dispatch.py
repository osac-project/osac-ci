"""Start E2E suites as runs of their own.

A suite whose job in the policy names a ``workflow`` does not run as a job of the run of the decision: the decision
starts that workflow with ``workflow_dispatch``, so the suite is a run of its own. A failed suite can then be run
again the moment it ends, by GitHub's own button (which waits for the whole run of a job that is not alone) or by
``/test``, and no suite waits for another.

What is verified on GitHub (see docs/RULES.md): the dispatch answers with the id of the run it started when it is
asked to (``return_run_details``), the run starts within seconds, the workflow's own token may start it, and
``workflow_run`` fires for it when it is requested and when it completes (also for a re-run and a cancel). The
workflow runs from the default branch, like the suites called by the decision, with the inputs the decision gives: the
commit that was approved, nothing else.

Everything that ends up in the request is checked here (it is read from the decision, which is read from a pull
request), and the run is named by the suite's own workflow (``E2E suite · <suite> · #<pull request> · <commit>``),
which is how the run is recognised again when it ends (see ``suite_ended``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from osac_ci.e2e_check import (
    SUITE_ID,
    WORKFLOW_FILE,
    check_sha,
    describe,
    newest_status,
    post_status,
    run_of_url,
    sum_from_lines,
    tier_of,
    validate_lines,
)
from osac_ci.e2e_start import TIER, suite_checks_for_files, suites_of, tiered
from osac_ci.e2e_suites_check import run_state
from osac_ci.github.api import GitHubClient, check_repo, get, paginate
from osac_ci.policy import Policy

REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")  # a branch name
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*(\[bot\])?")
ASSOCIATION = re.compile(r"[A-Z_]{0,30}")
RUN_NAME = re.compile(r"E2E suite · (?P<suite>[a-z0-9][a-z0-9_-]{0,39}) · #(?P<number>[0-9]+) · (?P<sha>[0-9a-f]{40})")
RUN_NAME_PREFIX = "E2E suite · "


@dataclass(frozen=True)
class Started:
    """A suite that was started as a run of its own."""

    suite: str
    run: int
    url: str
    check: str = ""  # the line of the suite


def run_name(suite: str, number: int, sha: str) -> str:
    """What the suite's workflow names its run (its ``run-name``), for the tests and the docs."""
    return f"{RUN_NAME_PREFIX}{suite} · #{number} · {sha}"


def read_run_name(title: str) -> tuple[str, int, str] | None:
    """(suite, pull request, commit) of a run named by ``run_name``; None for any other title."""
    found = RUN_NAME.fullmatch(title)
    return (found["suite"], int(found["number"]), found["sha"]) if found else None


def _inputs(
    item: Mapping[str, str], sha: str, number: int, head_repo: str, fork_author: str, fork_association: str
) -> dict[str, str]:
    given = {
        "repo": head_repo,
        "sha": sha,
        "pr-number": str(number),
        "fork-pr-author": fork_author,
        "fork-pr-author-association": fork_association,
    }
    if item.get("tier"):  # only a suite with a variant declares it: an input the workflow does not declare is refused
        given["tier"] = item["tier"]
    return given


def dispatch_suites(
    client: GitHubClient,
    repo: str,
    items: Sequence[Mapping[str, str]],
    *,
    sha: str,
    number: int,
    head_repo: str,
    fork_author: str = "",
    fork_association: str = "",
    lines_client: GitHubClient | None = None,
    lines_kind: str = "statuses",
    run_url: str = "",
) -> tuple[list[Started], list[str]]:
    """Start each suite of ``items`` (``{"suite", "workflow", "check", "tier"}``) as a run of its own, and write its
    line as waiting for a runner, linked to that run. Returns the runs started and the suites that could not be started
    (each of those gets a failed line saying so, so the E2E check cannot pass without it)."""
    repo = check_repo(repo)
    sha = check_sha(sha)
    if number < 1:
        raise ValueError("the pull request number must be positive")
    head_repo = check_repo(head_repo)
    if not LOGIN.fullmatch(fork_author) and fork_author != "":
        raise ValueError(f"invalid fork author: {fork_author!r}")
    if not ASSOCIATION.fullmatch(fork_association):
        raise ValueError(f"invalid fork association: {fork_association!r}")
    lines = validate_lines({str(i.get("suite", "")): str(i.get("check", "")) for i in items})
    for item in items:
        if not SUITE_ID.fullmatch(str(item.get("suite", ""))):
            raise ValueError(f"invalid suite id: {item.get('suite')!r}")
        if not WORKFLOW_FILE.fullmatch(str(item.get("workflow", ""))):
            raise ValueError(f"invalid workflow file for {item.get('suite')}: {item.get('workflow')!r}")
        if item.get("tier") and not TIER.fullmatch(str(item["tier"])):
            raise ValueError(f"invalid tier for {item['suite']}: {item['tier']!r}")
    branch = str(get(client, f"/repos/{repo}")["default_branch"])
    if not REF.fullmatch(branch):
        raise ValueError(f"invalid default branch: {branch!r}")
    writer = lines_client or client
    started: list[Started] = []
    failed: list[str] = []
    for item in items:
        suite, tier = item["suite"], item.get("tier", "")
        response = client.request(
            "POST",
            f"/repos/{repo}/actions/workflows/{item['workflow']}/dispatches",
            body={
                "ref": branch,
                "inputs": _inputs(item, sha, number, head_repo, fork_author, fork_association),
                "return_run_details": True,
            },
        )
        data = response.data if isinstance(response.data, Mapping) else {}
        run = data.get("workflow_run_id")
        if response.status != 200 or not isinstance(run, int) or run < 1:
            failed.append(suite)
            message = str(data.get("message", "")) if data else ""
            text = describe(f"Failed: could not be started ({response.status}{': ' + message if message else ''})")
            post_status(writer, repo, sha, lines[suite], "failure", text, run_url, lines_kind)
            continue
        url = f"https://github.com/{repo}/actions/runs/{run}"
        post_status(writer, repo, sha, lines[suite], "waiting", tiered("Waiting for a runner", tier), url, lines_kind)
        started.append(Started(suite, run, url, lines[suite]))
    return started, failed


def suite_ended(
    client: GitHubClient,
    repo: str,
    run_id: int,
    title: str,
    workflow_path: str,
    policy: Policy,
    *,
    lines_kind: str = "statuses",
) -> str:
    """A suite that ran as a run of its own has ended (the ``workflow_run`` event of its workflow): its line gets the
    result, then the E2E check is added up from the lines of the suites the pull request needs (see ``sum_from_lines``).

    What the event says is not believed on its own, since a person with write access can start the workflow with any
    inputs: the title must be the one the suite's workflow gives its run, the workflow must be the one the policy names
    for that suite, and the commit must still be the head of the pull request. A line that a newer run of the suite
    owns is left alone."""
    repo = check_repo(repo)
    found = read_run_name(title)
    if found is None:
        return "not the run of a suite: nothing to do"
    suite, number, sha = found
    file = workflow_path.rsplit("/", 1)[-1]
    mine = next((s for s in suites_of(policy) if s.name == suite and s.workflow == file), None)
    if mine is None:
        return f"{file} is not the workflow of the suite {suite} in the policy: nothing to do"
    base = f"/repos/{repo}/pulls/{number}"
    pr = get(client, base)
    if str((pr.get("head") or {}).get("sha", "")) != sha:
        return "the pull request has moved on to another commit: nothing to do"
    description, state, url, over = run_state(client, repo, run_id)
    if not over:
        return "the run has not ended: nothing to do"
    current = newest_status(client, repo, sha, mine.check, lines_kind)
    owner = run_of_url(str(current.get("target_url") or "")) if current is not None else None
    if owner is not None and owner != run_id:
        return "a newer run of the suite owns its line: nothing to do"
    says = tiered(description, tier_of(str(current.get("description") or "")) if current else "")
    post_status(client, repo, sha, mine.check, state, says, url, lines_kind)
    files: list[str] = []
    for raw in paginate(client, f"{base}/files"):
        files.append(str(raw["filename"]))
        if raw.get("previous_filename"):
            files.append(str(raw["previous_filename"]))
    needed = dict(suite_checks_for_files(policy, files))
    needed.setdefault(mine.name, mine.check)
    return sum_from_lines(client, repo, sha, needed, f"https://github.com/{repo}/actions/runs/{run_id}", "checks")
