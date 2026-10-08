"""The planner: a pure function from (snapshot, policy, mode) to a verdict.

No I/O, no clock, no randomness. The same inputs always give the same verdict, so it can be replayed against
history and tested exhaustively. Anything that can go wrong while planning is turned into the `planner-error`
state by ``plan_or_error`` (fail closed: the required check fails with a cause and a re-run hint).

Today's rules are encoded as they exist in the legacy scripts; target-state improvements are deliberate and
listed separately in the design doc (for example "skip before readiness").
"""

from __future__ import annotations

from collections.abc import Sequence

from osac_ci.model import (
    CheckRun,
    JobEntry,
    JobStatus,
    Mode,
    Snapshot,
    State,
    Verdict,
)
from osac_ci.paths import applicable
from osac_ci.policy import Job, Policy
from osac_ci.rules import readiness
from osac_ci.rules.fork import fork_secrets_authorized
from osac_ci.rules.labels import missing_required, present_blocking

# A completed check with one of these conclusions counts as passing for a required context (GitHub semantics).
_PASSING = frozenset({"success", "neutral", "skipped"})
_AUTH_DETAIL = "fork PR is not authorized to use secrets"

_NEXT: dict[State, tuple[str, str]] = {
    State.DRAFT: ("mark the PR ready for review", "author"),
    State.NEEDS_AUTHORIZATION: ("an org member comments /ok-to-test", "org member"),
    State.CHECKS_RUNNING: ("wait for the listed checks to finish", "nobody"),
    State.CHECKS_FAILED: ("fix the failing checks and push, or comment /retest for a flaky one", "author"),
    State.AWAITING_APPROVAL: ("get /lgtm and /approve and clear any blocking label", "reviewer or approver"),
    State.AWAITING_E2E_SIGNAL: ("get /lgtm, /e2e-ready, or a CodeRabbit approval on the current head", "reviewer"),
    State.E2E_RUNNING: ("wait for the full-install run to finish (about 100 minutes)", "nobody"),
    State.E2E_FAILED: ("fix the failure and push, or comment /retest if it looks like an infra flake", "author"),
    State.READY_TO_ENQUEUE: ("the merge queue picks it up", "nobody"),
    State.IN_QUEUE: ("wait for the merge queue", "nobody"),
    State.QUEUE_CHECKS_RUNNING: ("wait for the queue checks to finish", "nobody"),
    State.QUEUE_FAILED: ("fix the failure and push; the PR was ejected from the queue", "author"),
    State.QUEUE_PASSED: ("the queue merges it", "nobody"),
    State.PLANNER_ERROR: ("re-run the OSAC CI check; if it keeps failing, contact the infra group", "infra group"),
}


class PlannerError(RuntimeError):
    """The inputs cannot be planned (bad policy, impossible snapshot)."""


def latest_checks(check_runs: Sequence[CheckRun]) -> dict[str, CheckRun]:
    """Latest run per check name. Equal or missing timestamps resolve to the later entry, deterministically."""
    latest: dict[str, CheckRun] = {}
    for run in check_runs:
        current = latest.get(run.name)
        if current is None or (run.started_at or "") >= (current.started_at or ""):
            latest[run.name] = run
    return latest


def _from_check(run: CheckRun | None) -> tuple[JobStatus, str]:
    if run is None:
        return JobStatus.WAITING, "not reported yet"
    if run.status != "completed":
        return JobStatus.RUNNING, run.status
    if run.conclusion in _PASSING:
        return JobStatus.PASSED, "skipped (counts as passing)" if run.conclusion == "skipped" else "passed"
    return JobStatus.FAILED, f"conclusion: {run.conclusion}"


def _evaluate(job_id: str, job: Job, snapshot: Snapshot, mode: Mode, checks: dict[str, CheckRun]) -> JobEntry | None:
    if mode.value not in job.required_at:
        return None
    if not applicable(snapshot.changed_files, job.paths, job.exclude_paths):
        return JobEntry(job_id, job.check, JobStatus.NOT_APPLICABLE, "no changed file matches this job's paths")
    run = checks.get(job.check)
    finished = run is not None and run.status == "completed"
    if mode is Mode.PR and job.kind == "e2e" and job.needs_readiness and not finished:
        if not fork_secrets_authorized(snapshot):
            return JobEntry(job_id, job.check, JobStatus.WAITING, _AUTH_DETAIL)
        decision = readiness.decide(snapshot.labels, snapshot.reviews, snapshot.head_sha, snapshot.label_events)
        if not decision.allowed:
            return JobEntry(job_id, job.check, JobStatus.WAITING, decision.reason)
    status, detail = _from_check(run)
    return JobEntry(job_id, job.check, status, detail)


def _verdict(state: State, headline: str, mode: Mode, blockers: tuple[str, ...], jobs: tuple[JobEntry, ...]) -> Verdict:
    next_action, who = _NEXT[state]
    return Verdict(state, headline, next_action, who, mode, blockers, jobs)


def _names(entries: Sequence[JobEntry]) -> str:
    return ", ".join(e.check for e in entries)


def plan(snapshot: Snapshot, policy: Policy, mode: Mode = Mode.PR) -> Verdict:
    checks = latest_checks(snapshot.check_runs)
    entries = tuple(
        entry
        for job_id, job in policy.jobs.items()
        if (entry := _evaluate(job_id, job, snapshot, mode, checks)) is not None
    )
    kind = {job.check: job.kind for job in policy.jobs.values()}

    def where(status: JobStatus, job_kind: str | None = None) -> list[JobEntry]:
        return [e for e in entries if e.status is status and (job_kind is None or kind[e.check] == job_kind)]

    failed = where(JobStatus.FAILED)
    pending = where(JobStatus.RUNNING) + where(JobStatus.WAITING)
    blockers: list[str] = [f"{e.check}: {e.detail}" for e in failed + pending]

    if mode is Mode.QUEUE:
        if failed:
            return _verdict(State.QUEUE_FAILED, f"queue check failed: {_names(failed)}", mode, tuple(blockers), entries)
        if pending:
            return _verdict(
                State.QUEUE_CHECKS_RUNNING,
                f"queue checks still running: {_names(pending)}",
                mode,
                tuple(blockers),
                entries,
            )
        return _verdict(State.QUEUE_PASSED, "all queue checks passed", mode, (), entries)

    if snapshot.is_draft:
        return _verdict(State.DRAFT, "draft PR: not eligible for the merge queue", mode, tuple(blockers), entries)

    missing = missing_required(snapshot.labels, policy.merge.required_labels)
    blocking = present_blocking(snapshot.labels, policy.merge.blocking_labels)
    label_problems = [f"missing label: {m}" for m in missing] + [f"blocking label: {b}" for b in blocking]
    blockers += label_problems

    failed_cheap, failed_e2e = where(JobStatus.FAILED, "cheap"), where(JobStatus.FAILED, "e2e")
    if failed_cheap:
        return _verdict(
            State.CHECKS_FAILED, f"required checks failed: {_names(failed_cheap)}", mode, tuple(blockers), entries
        )
    if failed_e2e:
        return _verdict(State.E2E_FAILED, f"E2E failed: {_names(failed_e2e)}", mode, tuple(blockers), entries)

    waiting_e2e = [e for e in where(JobStatus.WAITING, "e2e")]
    if any(e.detail == _AUTH_DETAIL for e in waiting_e2e):
        return _verdict(State.NEEDS_AUTHORIZATION, _AUTH_DETAIL, mode, tuple(blockers), entries)

    pending_cheap = where(JobStatus.RUNNING, "cheap") + where(JobStatus.WAITING, "cheap")
    if pending_cheap:
        return _verdict(
            State.CHECKS_RUNNING,
            f"required checks still running: {_names(pending_cheap)}",
            mode,
            tuple(blockers),
            entries,
        )
    if label_problems:
        return _verdict(State.AWAITING_APPROVAL, "; ".join(label_problems), mode, tuple(blockers), entries)
    locked = [e for e in waiting_e2e if e.detail.startswith(("waiting:", "denied:"))]
    if locked:
        return _verdict(State.AWAITING_E2E_SIGNAL, locked[0].detail, mode, tuple(blockers), entries)
    if pending:
        return _verdict(State.E2E_RUNNING, f"E2E in progress: {_names(pending)}", mode, tuple(blockers), entries)

    nothing_ran = bool(entries) and all(e.status is JobStatus.NOT_APPLICABLE for e in entries)
    suffix = " (no required job applies to the changed files)" if nothing_ran else ""
    if snapshot.in_merge_queue:
        return _verdict(State.IN_QUEUE, "in the merge queue" + suffix, mode, (), entries)
    return _verdict(State.READY_TO_ENQUEUE, "all PR-time requirements are met" + suffix, mode, (), entries)


def plan_or_error(snapshot: Snapshot, policy: Policy, mode: Mode = Mode.PR) -> Verdict:
    """Fail closed: any exception becomes a `planner-error` verdict with a cause and a re-run hint."""
    try:
        return plan(snapshot, policy, mode)
    except Exception as exc:  # noqa: BLE001 - the whole point is to never let a planner bug pass as success
        return error_verdict(f"{type(exc).__name__}: {exc}", mode)


def error_verdict(message: str, mode: Mode = Mode.PR) -> Verdict:
    """The verdict for anything that stops the planner from answering, including failing to read the PR."""
    return _verdict(State.PLANNER_ERROR, f"planner error: {message}", mode, (), ())
