"""E2E status: the one required ``E2E`` status of a pull request, kept by a single workflow from the first push to the
result.

The full-install E2E starts from a workflow that runs on the default branch, after the pull request is approved on
its latest commit (CI runs in parallel and is not waited for). Its jobs are therefore not on the pull request, and a
required check that has not reported blocks the merge without saying why. This module keeps ONE commit status named
``E2E`` on the pull request's head commit: what the E2E is waiting for, then that it is running, then how it ended (or
that the change does not affect any suite). A commit status, not a check run: the pull request page lists statuses, but
not check runs that a workflow writes through the API (they hang on an unrelated workflow's check group and are not
drawn). The newest status of a context counts, so a status that was left behind never blocks a newer result, and
nothing has to be patched.

Two operations, both driven by the answer of ``e2e-decide`` (``e2e_start``):

* ``report`` shows what the answer means and, for ``start`` only, claims the E2E for this workflow run: it answers
  ``start=true`` when this run may start the suites. A commit gets its E2E once: a run that is alive, or a result that
  is already there, means no second start. A run that died without leaving a result is replaced and the E2E starts
  again. A second attempt of the SAME workflow run (the Actions page "re-run") starts again.
* ``finish`` records the result of the suites.

The workflow run that owns a status is read from its link (``.../actions/runs/<id>``). Only statuses created by the
GitHub Actions bot are read. ``plan`` decides and does no I/O; ``report`` and ``finish`` read and write through the
client.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from osac_ci.e2e_start import CHECK_APP, TIER, line_state, run_of_url, tier_of, tiered  # noqa: F401
from osac_ci.github.api import GitHubClient, GitHubError, check_repo, paginate
from osac_ci.github.snapshot import COMMIT_SHA
from osac_ci.model import CheckRun
from osac_ci.planner import latest_checks
from osac_ci.suite_lines import STATUS_CREATORS, suite_line_runs_if_readable

CONTEXT = "E2E"
CREATOR = "github-actions[bot]"  # who a workflow's built-in token writes statuses as (see STATUS_CREATORS)
KINDS = ("statuses", "checks")  # where the E2E lines are written: commit statuses, or check runs of the app
DESCRIPTION_MAX = 140  # what GitHub keeps of a status description
WAITING_ID = "waiting"  # external id of a status that waits
NOT_AFFECTED_ID = "not-affected"
RUN_PREFIX = "run:"  # external id of a status that belongs to a workflow run: "run:<run id>"
RUNNING_PREFIX = "Running"  # a pending status that starts like this is the claim of a run
NOT_AFFECTED_TITLE = "Not affected by this change"
RESULTS = ("success", "failure", "cancelled")
STATES = ("pending", "success", "failure", "error")  # what GitHub accepts for a status
# What this module writes: those, and "waiting", which is not running yet. A check run that waits is queued (the page
# draws a dot) and only one that runs is in progress (the page draws a spinner); a status has no such difference, so a
# waiting status is pending like a running one.
WRITE_STATES = (*STATES, "waiting")
WORKFLOW_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml")  # a file name of .github/workflows, never a path
SUITE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")  # a suite id: it ends up in job names, so no spaces or slashes
LINE_MAX = 100  # the longest name a suite line may have

WAITING_TITLES = {
    "skip-draft": "Waiting: the pull request is a draft",
    "wait-approval": "Waiting for an approval",
    "blocked-changes-requested": "Waiting: changes were requested",
    "blocked-cheap-checks-failed": "Waiting: CI failed",
    "wait-ci": "Waiting for CI",
}


BY_HAND_PREFIX = (
    "By hand: "  # a waiting check that also says what suites a person started: "By hand: vmaas running · Waiting…"
)
BY_HAND_WORDS = {"active": "running", "passed": "passed", "failed": "failed"}


def by_hand_text(states: Mapping[str, str]) -> str:
    """What the suites started by hand say, for the front of a waiting check: ``By hand: vmaas running, caas passed``.
    Suites with no line are left out; empty when none has one."""
    parts = [f"{suite} {BY_HAND_WORDS[state]}" for suite, state in states.items() if state in BY_HAND_WORDS]
    return BY_HAND_PREFIX + ", ".join(parts) if parts else ""


def waiting_part(description: str) -> str:
    """The wait a waiting check names (``Waiting: the pull request is a draft``), without a by-hand front."""
    if description.startswith(BY_HAND_PREFIX) and " · " in description:
        return description.split(" · ", 1)[1]
    return description


def wait_state(states: Mapping[str, str]) -> str:
    """The state of a waiting check: ``pending`` (in progress, the page spins) while a suite started by hand is going,
    else ``waiting`` (queued)."""
    return "pending" if "active" in states.values() else "waiting"


def with_by_hand(states: Mapping[str, str], waiting: str) -> str:
    front = by_hand_text(states)
    return describe(f"{front} · {waiting}" if front else waiting)


class Phase(StrEnum):
    WAITING = "waiting"
    RUNNING = "running"
    DONE = "done"


@dataclass(frozen=True)
class Existing:
    """The newest ``E2E`` status of the GitHub Actions bot on a commit."""

    state: str  # pending | success | failure | error
    description: str
    external_id: str  # waiting | not-affected | run:<id> | "" (see ``existing_from``)

    @property
    def phase(self) -> Phase:
        if self.state != "pending":
            return Phase.DONE
        return Phase.RUNNING if self.external_id.startswith(RUN_PREFIX) else Phase.WAITING

    @property
    def run_id(self) -> int | None:
        """The workflow run that owns this status, when it names one."""
        tail = self.external_id[len(RUN_PREFIX) :] if self.external_id.startswith(RUN_PREFIX) else ""
        return int(tail) if tail.isdigit() else None


@dataclass(frozen=True)
class Write:
    """One status to write. ``link`` points it at the workflow run that is writing it."""

    state: str  # waiting | pending (running) | success | failure | error
    description: str
    link: bool = True


@dataclass(frozen=True)
class Outcome:
    state: str  # waiting | running | not-affected | already-running | already-ran | ignored
    start: bool
    writes: tuple[Write, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Report:
    sha: str
    number: int | None
    state: str
    start: bool
    suites: tuple[str, ...]
    note: str
    suite_checks: tuple[tuple[str, str], ...] = ()  # (suite, line name) of the suites that run, from the policy
    tiers: tuple[tuple[str, str], ...] = ()  # (suite, tier) of the suites asked for through a variant
    cancel: tuple[int, ...] = ()  # the workflow runs a request by hand replaces: the caller stops them
    workflows: tuple[tuple[str, str], ...] = ()  # (suite, workflow file) of the suites that run as a run of their own

    @property
    def inline_suites(self) -> tuple[str, ...]:
        """The suites that run as jobs of the run of the caller (the others are dispatched)."""
        own = {suite for suite, _ in self.workflows}
        return tuple(s for s in self.suites if s not in own)

    @property
    def inline_checks(self) -> tuple[tuple[str, str], ...]:
        own = {suite for suite, _ in self.workflows}
        return tuple((s, line) for s, line in self.suite_checks if s not in own)

    @property
    def dispatched(self) -> tuple[Mapping[str, str], ...]:
        """What the decision starts as runs of their own: the suite, its workflow file, its line and its tier."""
        lines, tiers = dict(self.suite_checks), dict(self.tiers)
        return tuple(
            {"suite": suite, "workflow": file, "check": lines.get(suite, ""), "tier": tiers.get(suite, "")}
            for suite, file in self.workflows
        )


def validate_workflows(raw: object, suites: Iterable[str]) -> tuple[tuple[str, str], ...]:
    """The suite -> workflow file mapping of the decision. It ends up in an API path, so each part is checked: the suite
    is one the decision starts, the file a name of .github/workflows (no path, no query)."""
    if not raw:
        return ()
    if not isinstance(raw, Mapping):
        raise ValueError("the workflows must be an object of suite id to workflow file")
    started = set(suites)
    out: list[tuple[str, str]] = []
    for suite, file in raw.items():
        if suite not in started or not isinstance(file, str) or not WORKFLOW_FILE.fullmatch(file):
            raise ValueError(f"invalid workflow for {suite!r}: {file!r}")
        out.append((suite, file))
    return tuple(out)


def validate_lines(raw: object) -> dict[str, str]:
    """The suite id -> line name mapping the decision carries (from the ``check`` of each suite's job in the policy). It
    is read from data, so each part is checked: an id is a short lower-case word (it names a job), a line name is one
    short line of printable text, and none may be ``E2E`` itself, the required line that no suite may overwrite."""
    if not isinstance(raw, Mapping):
        raise ValueError("the suite checks must be an object of suite id to line name")
    out: dict[str, str] = {}
    for suite, line in raw.items():
        if not isinstance(suite, str) or not SUITE_ID.fullmatch(suite):
            raise ValueError(f"invalid suite id: {suite!r}")
        if not isinstance(line, str) or not line.strip() or len(line) > LINE_MAX or not line.isprintable():
            raise ValueError(f"invalid line name for {suite}: {line!r}")
        if line.strip() == CONTEXT:
            raise ValueError(f"the line of {suite} cannot be named {CONTEXT}: that is the required line")
        out[suite] = line.strip()
    if len(set(out.values())) != len(out):
        raise ValueError("two suites have the same line name")
    return out


def describe(text: str) -> str:
    """A status description is one line of at most 140 characters; a longer one is cut with an ellipsis."""
    one = " ".join(text.split())
    return one if len(one) <= DESCRIPTION_MAX else one[: DESCRIPTION_MAX - 1].rstrip() + "…"


# deciding -------------------------------------------------------------------------------------------------------


def _suites(entry: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(s) for s in entry.get("suites") or ())


def _tiers(entry: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """The tier each suite that runs is asked for (a variant of it was ticked). It is read from data and ends up in a
    job's inputs, so each part is checked: the suite is one that runs, the tier a short lower-case word."""
    raw = entry.get("tiers") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("the tiers must be an object of suite id to tier")
    suites = {*_suites(entry), *_selected(entry)}  # a decision that starts nothing still names the suites it counted
    out: list[tuple[str, str]] = []
    for suite, tier in raw.items():
        if suite not in suites or not isinstance(tier, str) or not TIER.fullmatch(tier):
            raise ValueError(f"invalid tier for {suite!r}: {tier!r}")
        out.append((suite, tier))
    return tuple(out)


def _cancel(entry: Mapping[str, Any]) -> tuple[int, ...]:
    """The runs a request replaces. They end up in an API path, so each must be a number."""
    raw = entry.get("cancel") or ()
    if not isinstance(raw, (list, tuple)) or not all(
        isinstance(r, int) and not isinstance(r, bool) and r > 0 for r in raw
    ):
        raise ValueError("the runs to cancel must be a list of run numbers")
    return tuple(raw)


def waiting_description(entry: Mapping[str, Any]) -> str:
    """What the E2E waits for, in the 140 characters a status has: the title, then the reason. The decision's whole
    explanation is in the job summary of the run the status links to."""
    action = str(entry.get("action", ""))
    title = WAITING_TITLES.get(action, f"Waiting: {action}")
    reason = str(entry.get("reason", "")).strip()
    # a reason that only says again what the title says (a draft) is left out
    text = f"{title}: {reason}" if reason and reason.lower() not in title.lower() else title
    return with_by_hand(_by_hand(entry), text)


def _by_hand(entry: Mapping[str, Any]) -> dict[str, str]:
    raw = entry.get("by_hand") or {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, Mapping) else {}


def _waiting(entry: Mapping[str, Any], existing: Existing | None, alive: Callable[[int], bool]) -> Outcome:
    write = Write(wait_state(_by_hand(entry)), waiting_description(entry))
    if existing is None:
        return Outcome("waiting", False, (write,))
    if existing.phase is Phase.WAITING:
        if existing.description == write.description:
            return Outcome("waiting", False)
        return Outcome("waiting", False, (write,))
    if existing.phase is Phase.RUNNING:
        if existing.run_id is not None and alive(existing.run_id):
            return Outcome("already-running", False, note=f"run {existing.run_id} is running it")
        return Outcome("waiting", False, (write,))
    return Outcome("already-ran", False, note="a result is already on this commit")


def _hand_writes(entry: Mapping[str, Any], existing: Existing | None) -> tuple[Write, ...]:
    if existing is None or existing.phase is not Phase.WAITING:
        return ()
    counted = {suite for suite in _selected(entry)}
    states = {s: st for s, st in _by_hand(entry).items() if s in counted}
    states.update({s: "active" for s in _suites(entry) if s in counted})
    text = with_by_hand(states, waiting_part(existing.description))
    return () if text == existing.description else (Write(wait_state(states), text),)


def _claim(entry: Mapping[str, Any]) -> Write:
    suites = _suites(entry)
    return Write("pending", describe(f"{RUNNING_PREFIX}: {', '.join(suites) or 'the full-install suites'}"))


def _start(entry: Mapping[str, Any], existing: Existing | None, run_id: int, alive: Callable[[int], bool]) -> Outcome:
    if existing is None or existing.phase is Phase.WAITING:
        return Outcome("running", True, (_claim(entry),))
    if existing.phase is Phase.RUNNING:
        if existing.run_id == run_id:
            return Outcome("running", True, note="this run already claimed it")
        if existing.run_id is not None and alive(existing.run_id):
            return Outcome("already-running", False, note=f"run {existing.run_id} is running it")
        return Outcome("running", True, (_claim(entry),), note=f"run {existing.run_id} ended without a result")
    if existing.run_id == run_id:  # the same workflow run, started again from the Actions page
        return Outcome("running", True, (_claim(entry),), note="re-run of the same workflow run")
    return Outcome(
        "already-ran",
        False,
        note="the E2E already ran on this commit; re-run it from its workflow run, or push a new commit",
    )


def _not_affected(existing: Existing | None) -> Outcome:
    if existing is not None and existing.phase is not Phase.WAITING:
        return Outcome("not-affected", False, note="the status already has its answer")
    return Outcome("not-affected", False, (Write("success", NOT_AFFECTED_TITLE),))


def replaceable(existing: Existing | None) -> bool:
    """Whether the sum of the suites may be written over what the E2E check says: nothing yet, a wait, or a failure (a
    run that failed does not stop later runs by hand from passing every suite). A pass and a running run stay."""
    if existing is None or existing.phase is Phase.WAITING:
        return True
    return existing.phase is Phase.DONE and existing.state != "success"


def _already_passed(entry: Mapping[str, Any], existing: Existing | None) -> Outcome:
    """Every suite the change needs passed on this commit (run by hand): the E2E is the sum of them. A pass and a run
    that is running stay."""
    if not replaceable(existing):
        return Outcome("already-ran", False, note="a result is already on this commit")
    tiers = dict(_tiers(entry))
    names = ", ".join(f"{s} ({tiers[s]})" if s in tiers else s for s in _selected(entry))
    return Outcome("already-passed", False, (Write("success", describe(f"Passed: {names}")),))


def _selected(entry: Mapping[str, Any]) -> tuple[str, ...]:
    raw = entry.get("selected_checks") or {}
    return tuple(str(k) for k in raw) if isinstance(raw, Mapping) else ()


def plan(entry: Mapping[str, Any], existing: Existing | None, run_id: int, alive: Callable[[int], bool]) -> Outcome:
    """What to do for one decision, given the newest ``E2E`` status on the commit. ``alive`` says whether a workflow
    run is still going; it is only asked when the answer depends on it."""
    action = str(entry.get("action", ""))
    if action == "skip-closed":
        return Outcome("ignored", False, note="the pull request is closed")
    if action == "skip-not-applicable":
        return _not_affected(existing)
    if action == "already-passed":
        return _already_passed(entry, existing)
    if action == "skip-not-a-request":
        return Outcome("ignored", False, note="the comment asks for nothing")
    if action in ("nothing-to-start", "running-by-hand"):
        return Outcome("ignored", False, note=str(entry.get("reason", "")))
    if action == "start":
        if entry.get("manual"):
            # started by hand: only the suites' lines change; the E2E check is not claimed (see finish_by_hand). A check
            # that waits says so in front of its wait, so the page does not read "waiting" while the suites run.
            return Outcome("running", True, _hand_writes(entry, existing), note="started by hand")
        return _start(entry, existing, run_id, alive)
    return _waiting(entry, existing, alive)


# reading and writing GitHub ---------------------------------------------------------------------------------------


def check_sha(sha: str) -> str:
    """A commit id ends up in a URL path and in a request body: only 40 lower-case hex digits, nothing around them (a
    trailing newline would pass ``match``, so the whole string has to match)."""
    if not COMMIT_SHA.fullmatch(sha):
        raise ValueError(f"invalid commit id: {sha!r}")
    return sha


def newest_status(
    client: GitHubClient, repo: str, sha: str, context: str, kind: str = "statuses"
) -> Mapping[str, Any] | None:
    """The newest line of this name that this workflow side wrote on the commit, as a status-shaped mapping (``state``,
    ``description``, ``target_url``). Statuses: those of the OSAC CI app or the GitHub Actions bot (GitHub lists them
    newest first). Check runs: those of the OSAC CI app, the newest by id; an in-progress run is ``pending``, a
    finished one ``success``, ``failure`` or ``error`` (a cancelled one)."""
    if kind == "checks":
        runs = paginate(
            client,
            f"/repos/{check_repo(repo)}/commits/{check_sha(sha)}/check-runs",
            key="check_runs",
            params={"check_name": context, "filter": "all"},
        )
        mine = [r for r in runs if r.get("name") == context and (r.get("app") or {}).get("slug") == CHECK_APP]
        if not mine:
            return None
        run = max(mine, key=lambda r: int(r["id"]))
        done = str(run.get("status")) == "completed"
        state = (
            {"success": "success", "failure": "failure"}.get(str(run.get("conclusion")), "error") if done else "pending"
        )
        return {
            "state": state,
            "description": str((run.get("output") or {}).get("title") or ""),
            "target_url": run.get("details_url") or "",
        }
    statuses = paginate(client, f"/repos/{check_repo(repo)}/commits/{check_sha(sha)}/statuses")
    for status in statuses:
        if status.get("context") == context and (status.get("creator") or {}).get("login") in STATUS_CREATORS:
            return status  # type: ignore[no-any-return]
    return None


def existing_from(status: Mapping[str, Any]) -> Existing:
    """What ``plan`` needs to know about a status, read back from what was written: a pending status that says
    "Running" is the claim of the run its link names; any other pending one waits; the success that says "not
    affected" is that answer; any other finished one is the result of the run its link names."""
    state = str(status.get("state", ""))
    description = str(status.get("description") or "")
    run = run_of_url(str(status.get("target_url") or ""))
    if state == "pending":
        external = f"{RUN_PREFIX}{run}" if run is not None and description.startswith(RUNNING_PREFIX) else WAITING_ID
    elif state == "success" and description == NOT_AFFECTED_TITLE:
        external = NOT_AFFECTED_ID
    else:
        external = f"{RUN_PREFIX}{run}" if run is not None else ""
    return Existing(state, description, external)


def latest_check(client: GitHubClient, repo: str, sha: str, kind: str = "statuses") -> Existing | None:
    status = newest_status(client, repo, sha, CONTEXT, kind)
    return existing_from(status) if status is not None else None


def run_is_alive(client: GitHubClient, repo: str, run_id: int) -> bool:
    response = client.request("GET", f"/repos/{check_repo(repo)}/actions/runs/{run_id}")
    if response.status == 404:
        return False
    if not 200 <= response.status < 300 or not isinstance(response.data, dict):
        raise GitHubError(response.status, f"GET the workflow run {run_id}")
    return str(response.data.get("status")) != "completed"


def post_status(
    client: GitHubClient,
    repo: str,
    sha: str,
    context: str,
    state: str,
    description: str,
    target_url: str = "",
    kind: str = "statuses",
) -> None:
    """Write one line. Statuses: a commit status. Check runs: a check run of the token's app (the OSAC CI app), in
    progress for ``pending`` (the page draws a spinner), else completed with ``success``, ``failure`` or ``cancelled``;
    the newest of a name counts, so nothing is patched."""
    if state not in WRITE_STATES:
        raise ValueError(f"state must be one of {', '.join(WRITE_STATES)}, not {state!r}")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}, not {kind!r}")
    text = describe(description)
    if kind == "checks":
        body: dict[str, Any] = {
            "name": context,
            "head_sha": check_sha(sha),
            "output": {"title": text, "summary": text + (f"\n\n[Details]({target_url})" if target_url else "")},
        }
        if state == "waiting":
            body["status"] = "queued"
        elif state == "pending":
            body["status"] = "in_progress"
        else:
            body["status"] = "completed"
            body["conclusion"] = {"success": "success", "failure": "failure", "error": "cancelled"}[state]
        if target_url:
            body["details_url"] = target_url
        response = client.request("POST", f"/repos/{check_repo(repo)}/check-runs", body=body)
        what = "check"
    else:
        body = {"state": "pending" if state == "waiting" else state, "context": context, "description": text}
        if target_url:
            body["target_url"] = target_url
        response = client.request("POST", f"/repos/{check_repo(repo)}/statuses/{check_sha(sha)}", body=body)
        what = "status"
    if response.status not in (200, 201):
        message = response.data.get("message", "") if isinstance(response.data, dict) else ""
        raise GitHubError(response.status, f"writing the {context} {what} failed: {message}")


def write_status(client: GitHubClient, repo: str, sha: str, write: Write, run_url: str, kind: str = "statuses") -> None:
    post_status(client, repo, sha, CONTEXT, write.state, write.description, run_url if write.link else "", kind)


def report(
    client: GitHubClient,
    repo: str,
    entries: Sequence[Mapping[str, Any]],
    run_id: int,
    run_url: str,
    kind: str = "statuses",
    lines_kind: str | None = None,
) -> Report:
    """Show the answer of ``e2e-decide`` on the pull request and, for ``start``, claim the E2E for this run. Nothing
    is done unless exactly one pull request was decided. ``kind`` is where the E2E line is written; the lines of the
    suites are written as ``lines_kind`` (the same by default): commit statuses link their row straight to the job."""
    if not all(isinstance(e, Mapping) for e in entries):
        raise ValueError("every decision must be an object")
    if len(entries) != 1:
        note = "no open pull request has this head commit" if not entries else "several pull requests have this head"
        return Report("", None, "ignored", False, (), note)
    entry = entries[0]
    sha = str(entry["head_sha"])
    # the lines of the suites that run, checked before anything is written
    wanted = validate_lines(entry.get("suite_checks") or {})
    if wanted:
        unnamed = [suite for suite in _suites(entry) if suite not in wanted]
        if unnamed:
            raise ValueError(f"the decision gives no line name for: {', '.join(unnamed)}")
    lines = tuple((suite, wanted[suite]) for suite in _suites(entry) if suite in wanted)
    if lines_kind is not None and lines_kind not in KINDS:
        raise ValueError(f"lines kind must be one of {', '.join(KINDS)}, not {lines_kind!r}")
    tiers = dict(_tiers(entry))
    existing = latest_check(client, repo, sha, kind)
    outcome = plan(entry, existing, run_id, lambda rid: run_is_alive(client, repo, rid))
    for write in outcome.writes:
        write_status(client, repo, sha, write, run_url, kind)
    number = int(entry["number"])
    by_hand = bool(entry.get("manual"))
    if outcome.start and by_hand:
        for suite, line in lines:
            waiting = tiered("Waiting for a runner", tiers.get(suite, ""))
            post_status(client, repo, sha, line, "waiting", waiting, run_url, lines_kind or kind)
    elif outcome.start:
        # Two runs for the same commit can both have read "no claim yet". Look again after writing: if the newest status
        # is not this run's, another run claimed the E2E in between and this one must not start the suites. The calling
        # workflow must also run one decision at a time per commit; this look only narrows the window.
        newest = latest_check(client, repo, sha, kind)
        if newest is None or newest.run_id != run_id:
            other = f"run {newest.run_id}" if newest is not None and newest.run_id else "another run"
            return Report(sha, number, "already-running", False, _suites(entry), f"{other} claimed the E2E first")
        # The suite lines appear together with E2E, not a job start later (and replace a result left by an earlier
        # attempt of this run). The tracking job keeps them current from here on.
        for suite, line in lines:
            waiting = tiered("Waiting for a runner", tiers.get(suite, ""))
            post_status(client, repo, sha, line, "waiting", waiting, run_url, lines_kind or kind)
    cancel = _cancel(entry) if outcome.start and by_hand else ()
    workflows = validate_workflows(entry.get("workflows"), _suites(entry))
    return Report(
        sha, number, outcome.state, outcome.start, _suites(entry), outcome.note, lines, _tiers(entry), cancel, workflows
    )


def finish(
    client: GitHubClient, repo: str, sha: str, result: str, run_url: str, detail: str = "", kind: str = "statuses"
) -> None:
    """Record how the suites ended."""
    if result not in RESULTS:
        raise ValueError(f"result must be one of {', '.join(RESULTS)}, not {result!r}")
    title = {"success": "Passed", "failure": "Failed", "cancelled": "Cancelled"}[result]
    state = {"success": "success", "failure": "failure", "cancelled": "error"}[result]
    write_status(client, repo, sha, Write(state, f"{title}: {detail}" if detail else title), run_url, kind)


def _latest_lines(client: GitHubClient, repo: str, sha: str, lines: Mapping[str, str]) -> Mapping[str, CheckRun]:
    """The newest line of each name on the commit. A line is a check run of the app or a commit status (see
    ``suite_lines``)."""
    raw = [
        r
        for r in paginate(client, f"/repos/{check_repo(repo)}/commits/{check_sha(sha)}/check-runs", key="check_runs")
        if (r.get("app") or {}).get("slug") == CHECK_APP
    ]
    raw += suite_line_runs_if_readable(client, repo, sha, lines.values(), CHECK_APP)
    runs = tuple(
        CheckRun(
            r["name"],
            r["status"],
            r.get("conclusion"),
            r.get("started_at"),
            app=CHECK_APP,
            title=str((r.get("output") or {}).get("title") or "") if isinstance(r.get("output"), Mapping) else "",
            url=str(r.get("details_url") or r.get("html_url") or ""),
        )
        for r in raw
    )
    return latest_checks(runs)


def suite_readings(
    client: GitHubClient, repo: str, sha: str, lines: Mapping[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
    """For each suite (id -> line name), what its line says on the commit (none, active, passed or failed) and the tier
    it says it ran with ("" for the suite itself); the newest line of a name counts."""
    latest = _latest_lines(client, repo, sha, lines)
    return {suite: line_state(latest, line) for suite, line in lines.items()}, {
        suite: tier_of(latest[line].title) if line in latest else "" for suite, line in lines.items()
    }


def suite_states(client: GitHubClient, repo: str, sha: str, lines: Mapping[str, str]) -> dict[str, str]:
    """``suite_readings`` without the tiers: what each suite's line says on the commit."""
    return suite_readings(client, repo, sha, lines)[0]


AGAIN_SUFFIX = " running again"  # the end of the text of a check that waits for a suite that is being run again


def _running_again(
    client: GitHubClient,
    repo: str,
    latest: Mapping[str, CheckRun],
    lines: Mapping[str, str],
    skip_run: int | None,
) -> list[str]:
    """The suites whose line says it is going in a run that is alive, other than ``skip_run`` (the run that is writing:
    a line of its own that still says it is going was not completed, it is not a retest)."""
    return [
        suite
        for suite, line in lines.items()
        if line_state(latest, line) == "active"
        and (run := run_of_url(latest[line].url)) is not None
        and run != skip_run
        and run_is_alive(client, repo, run)
    ]


def finish_from_lines(
    client: GitHubClient,
    repo: str,
    sha: str,
    result: str,
    run_url: str,
    detail: str,
    suite_checks: Mapping[str, str],
    kind: str = "checks",
) -> str:
    """After the run that started the suites: the E2E check says how the suites ended, but a suite of this run that
    failed may have been run again since, by hand, in a run of its own. When this run ended in failure, the lines of its
    suites decide what the check says: every one passed (the retest did), so ``Passed``; one is being run again, so the
    check waits (the run of the retest writes the answer when it ends); otherwise ``Failed`` as before. A result that is
    not a failure (a pass, a cancel) is written as it is."""
    lines = validate_lines(suite_checks)
    if result != "failure" or not lines:
        finish(client, repo, sha, result, run_url, detail, kind)
        return result
    latest = _latest_lines(client, repo, sha, lines)
    states = {suite: line_state(latest, line) for suite, line in lines.items()}
    again = _running_again(client, repo, latest, lines, run_of_url(run_url))
    if again:
        text = f"Waiting: {', '.join(again)}{AGAIN_SUFFIX}"
        # in progress, not queued: a retest is going, so the check shows it (it still counts as a wait: existing_from)
        write_status(client, repo, sha, Write("pending", describe(text)), run_url, kind)
        return "waiting"
    if all(state == "passed" for state in states.values()):
        named = ", ".join(
            f"{suite} ({tier})" if (tier := tier_of(latest[line].title)) else suite for suite, line in lines.items()
        )
        write_status(client, repo, sha, Write("success", describe(f"Passed: {named}")), run_url, kind)
        return "passed"
    finish(client, repo, sha, result, run_url, detail, kind)
    return result


RUNNING_SUFFIX = " running"  # the end of the text of a check that waits for suites that run as runs of their own


def _waits_for_suites(existing: Existing | None) -> bool:
    """Whether the check is the wait ``sum_from_lines`` writes (or the wait for a suite run again)."""
    if existing is None or existing.phase is not Phase.WAITING:
        return False
    part = waiting_part(existing.description)
    return part.startswith("Waiting: ") and part.endswith((RUNNING_SUFFIX, AGAIN_SUFFIX))


def sum_from_lines(
    client: GitHubClient, repo: str, sha: str, suite_checks: Mapping[str, str], run_url: str, kind: str = "checks"
) -> str:
    """The E2E check as the sum of the lines of the suites the change needs, for suites that run as runs of their own:
    whoever sees one of them end (the run that started them, or the handler of the run that ended) adds them up.

    A suite that is still going keeps the check waiting (it names the suites); when none is going, every suite passed is
    ``Passed`` and anything less is ``Failed``. Only the claim of the run that started the suites, the wait this writes
    and (for a pass) what ``replaceable`` allows are written over: a result that is already there stays."""
    lines = validate_lines(suite_checks)
    if not lines:
        return "nothing to add up: the E2E check is left as it is"
    latest = _latest_lines(client, repo, sha, lines)
    states = {suite: line_state(latest, line) for suite, line in lines.items()}
    existing = latest_check(client, repo, sha, kind)
    own = run_of_url(run_url)
    mine = (
        existing is None
        or _waits_for_suites(existing)
        # the claim of the run that is writing, or of a run that ended without answering (a claim of a run that goes
        # writes its own answer: this one would be a partial sum)
        or (
            existing.phase is Phase.RUNNING
            and (existing.run_id == own or existing.run_id is None or not run_is_alive(client, repo, existing.run_id))
        )
    )
    going = _running_again(client, repo, latest, lines, own)
    if going:
        if not mine:
            return f"{', '.join(going)} still running, the E2E check is left as it is"
        text = f"Waiting: {', '.join(going)}{RUNNING_SUFFIX}"
        if existing is None or existing.description != text:
            write_status(client, repo, sha, Write("pending", describe(text)), run_url, kind)
        return "waiting"
    if all(state == "passed" for state in states.values()):
        if not (mine or replaceable(existing)):
            return "the E2E check already has its answer"
        named = ", ".join(
            f"{suite} ({tier})" if (tier := tier_of(latest[line].title)) else suite for suite, line in lines.items()
        )
        write_status(client, repo, sha, Write("success", describe(f"Passed: {named}")), run_url, kind)
        return "passed"
    if not mine:
        return "the E2E check already has its answer"
    failed = [suite for suite, state in states.items() if state == "failed"]
    if not failed:
        # a suite with no line has reported nothing (it did not start, or its line is not written yet): that is not a
        # failure of it, so the check is left as it is (a later decision or retest settles it)
        return "a suite has no line yet and none failed: the E2E check is left as it is"
    write_status(client, repo, sha, Write("failure", describe(f"Failed: {', '.join(failed)}")), run_url, kind)
    return "failure"


def finish_with_dispatched(
    client: GitHubClient,
    repo: str,
    sha: str,
    result: str,
    run_url: str,
    detail: str,
    suite_checks: Mapping[str, str],
    kind: str = "checks",
) -> str:
    """After the run that started the suites, when some of them run as runs of their own: ``suite_checks`` are all the
    lines of the suites it started, ``result`` is how its own jobs ended. A pass of its own jobs is only part of the
    answer, so the lines decide (``sum_from_lines``: waiting while a suite goes); a failure or a cancel is handled as
    for a run of jobs only (``finish_from_lines``)."""
    if result == "success":
        return sum_from_lines(client, repo, sha, suite_checks, run_url, kind)
    return finish_from_lines(client, repo, sha, result, run_url, detail, suite_checks, kind)


def finish_by_hand(
    client: GitHubClient, repo: str, sha: str, selected: Mapping[str, str], run_url: str, kind: str = "checks"
) -> str:
    """After a run started by hand: the E2E check is the sum of the suites the change needs, so it turns green only when
    every one of them has passed (run by hand or by the approval). Anything less leaves it as it is: a partial run never
    makes the required check pass, and a failure here is shown on the suite's own line, not as a result of the E2E."""
    if kind != "checks":
        raise ValueError("runs started by hand write check runs: the kind must be checks")
    lines = validate_lines(selected)
    if not lines:
        return "nothing selected: the E2E check is left as it is"
    states, tiers = suite_readings(client, repo, sha, lines)
    existing = latest_check(client, repo, sha, kind)
    if any(state != "passed" for state in states.values()):
        left = ", ".join(s for s, state in states.items() if state != "passed")
        if (
            existing is not None
            and existing.phase is Phase.WAITING
            and waiting_part(existing.description).endswith(AGAIN_SUFFIX)
        ):
            # the run that failed ended and left the check waiting for a suite that was being run again: that retest has
            # ended too (or another is going), so the check says how it stands now
            again = _running_again(client, repo, _latest_lines(client, repo, sha, lines), lines, run_of_url(run_url))
            if again:
                text = f"Waiting: {', '.join(again)}{AGAIN_SUFFIX}"
                if text != waiting_part(existing.description):
                    write_status(client, repo, sha, Write("pending", describe(text)), run_url, kind)
                return f"{', '.join(again)} is still being run again: the E2E check keeps waiting"
            write_status(client, repo, sha, Write("failure", describe(f"Failed: {left}")), run_url, kind)
            return f"not every suite has passed ({left}), none is being run again: the E2E check is failed"
        if existing is not None and existing.phase is Phase.WAITING:
            text = with_by_hand(states, waiting_part(existing.description))
            if text != existing.description:
                write_status(client, repo, sha, Write(wait_state(states), text), run_url, kind)
        return f"not every suite has passed yet ({left}): the E2E check is not turned green"
    if not replaceable(existing):
        return "the E2E check already passed or a run is running it"
    named = ", ".join(f"{suite} ({tiers[suite]})" if tiers.get(suite) else suite for suite in lines)
    write_status(client, repo, sha, Write("success", describe(f"Passed: {named}")), run_url, kind)
    return "every suite passed: the E2E check is green"


# rendering ------------------------------------------------------------------------------------------------------


def render(found: Report) -> str:
    if found.state == "ignored":
        return f"### E2E status: nothing to do\n\n{found.note}\n"
    lines = [f"### E2E status: **{found.state}**" + (" (starting the suites)" if found.start else ""), ""]
    if found.suites:
        tiers = dict(found.tiers)
        lines.append(f"- suites: {', '.join(f'{s} ({tiers[s]})' if s in tiers else s for s in found.suites)}")
    lines.append(f"- commit: `{found.sha[:7]}`")
    if found.note:
        lines.append(f"- {found.note}")
    return "\n".join(lines) + "\n"


def to_json(found: Report) -> str:
    return json.dumps(
        {
            "sha": found.sha,
            "number": found.number,
            "state": found.state,
            "start": found.start,
            "suites": list(found.suites),
            "suite_checks": dict(found.suite_checks),
            "tiers": dict(found.tiers),
            "cancel": list(found.cancel),
            "workflows": dict(found.workflows),
            # what the caller runs as jobs of its own run, and what the decision dispatches as runs of their own
            "inline_suites": list(found.inline_suites),
            "inline_suite_checks": dict(found.inline_checks),
            "dispatched": list(found.dispatched),
            "note": found.note,
        },
        indent=2,
    )
