"""The planner: a pure function from (snapshot, policy, mode) to a verdict.

No I/O, no clock, no randomness. The same inputs always give the same verdict, so it can be replayed against
history and tested exhaustively. Anything that can go wrong while planning is turned into the `planner-error`
state by ``plan_or_error`` (fail closed: the required check fails with a cause and a re-run hint).

Today's rules are encoded as they exist in the legacy scripts; target-state improvements are deliberate and
listed separately in the design doc (for example "skip before readiness").
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from osac_ci.model import (
    CheckRun,
    JobEntry,
    JobStatus,
    Mode,
    Snapshot,
    State,
    Verdict,
)
from osac_ci.paths import applicable, filters_hold
from osac_ci.policy import Job, Policy
from osac_ci.rules import approval, e2e_unlock, locks, protected, readiness
from osac_ci.rules.fork import authorization_command, fork_secrets_authorized
from osac_ci.rules.labels import missing_required, present_blocking

# A completed check with one of these conclusions counts as passing for a required context (GitHub semantics).
_PASSING = frozenset({"success", "neutral", "skipped"})
_AUTH_DETAIL = locks.AUTH_DETAIL

_CHANGES_REQUESTED_NEXT = "address the requested changes; the reviewer approves again or dismisses their review"
_NATIVE_APPROVAL_NEXT = ("get a code owner to approve the current changes and clear any blocking label", "code owner")

_NEXT: dict[State, tuple[str, str]] = {
    State.DRAFT: ("mark the PR ready for review", "author"),
    State.NEEDS_AUTHORIZATION: ("an org member comments /ok-to-test", "org member"),
    State.CHECKS_RUNNING: ("wait for the listed checks to finish", "nobody"),
    State.CHECKS_FAILED: ("fix the failing checks and push, or comment /retest for a flaky one", "author"),
    State.AWAITING_APPROVAL: ("get /lgtm and /approve and clear any blocking label", "reviewer or approver"),
    State.AWAITING_E2E_SIGNAL: ("get /lgtm, /e2e-ready, or a CodeRabbit approval on the current head", "reviewer"),
    State.AWAITING_UNLOCK: ("open the lock named in the blockers", "reviewer"),
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


_FAILED_RUN = frozenset({"failure", "timed_out"})


def _filters_verdict(job: Job, snapshot: Snapshot, policy: Policy) -> bool | None:
    """True or False when the job's named path filters decide applicability, None when they cannot (the job names no
    filter, the PR lists no changed files, or the list could not be read completely: none is a reason to skip)."""
    if not (job.filters or job.filters_any) or not snapshot.changed_files or not snapshot.changed_files_known:
        return None
    return filters_hold(snapshot.changed_files, policy.path_filters.filters, job.filters, job.filters_any)


def _filter_note(job: Job, run: CheckRun | None, holds: bool | None) -> str:
    """Shadow mode: say where the filters and the check's own outcome disagree. Only two cases are unambiguous. A
    workflow that finds nothing to do usually still reports success, so a passing check proves nothing either way.
    (A failure on irrelevant files can also mean the workflow's own filter step failed: it then fails its checks.)"""
    if holds is None or run is None or run.status != "completed":
        return ""
    names = ", ".join((*job.filters, *job.filters_any))
    if not holds and run.conclusion in _FAILED_RUN:
        why = f"but its conclusion was {run.conclusion}"
        return f"path filter: {names} say {job.check} does not apply to these files, {why}"
    # A readiness-gated job is skipped on purpose until it is unlocked, so a skip there says nothing about the filters.
    if holds and run.conclusion == "skipped" and not job.needs_readiness:
        return f"path filter: {names} say {job.check} applies to these files, but it was skipped"
    return ""


def _skipped_but_applicable(
    job: Job, run: CheckRun | None, holds: bool | None, snapshot: Snapshot, policy: Policy
) -> bool:
    """Enforce mode with ``skipped_applicable: fail``: a job that applies to this PR, whose check was skipped, did not
    do its work. GitHub counts a skip as a pass, so a workflow changed to skip itself would go unnoticed.

    A job applies when its named filters hold, when its globs matched, or when nothing narrows it (it runs on every
    PR). With a filter or glob but no usable file list nothing can be said, so the skip stands. A readiness-gated job
    is skipped on purpose until it is unlocked, so it is exempt."""
    settings = policy.path_filters
    if settings.mode != "enforce" or settings.skipped_applicable != "fail" or job.needs_readiness:
        return False
    if run is None or run.status != "completed" or run.conclusion != "skipped":
        return False
    if holds is not None:
        return holds
    if job.filters or job.filters_any:
        return False
    if job.paths:
        return snapshot.changed_files_known
    return True


def _has_result(run: CheckRun | None, holds: bool | None) -> bool:
    """Did the job report something that stands without asking the locks?

    A check that completed with a conclusion other than ``skipped`` ran, so its result stands. A skipped check is a
    result only when the path filters say the job does not apply to these files: the workflows report that case as
    skipped too (real pull requests show it), and it means "nothing to do", not "held back". Otherwise a skip is not
    evidence of anything and a closed lock still holds the job."""
    if run is None or run.status != "completed":
        return False
    return run.conclusion != "skipped" or holds is False


def _evaluate(
    job_id: str,
    job: Job,
    snapshot: Snapshot,
    mode: Mode,
    checks: dict[str, CheckRun],
    labels: frozenset[str],
    policy: Policy,
) -> JobEntry | None:
    if mode.value not in job.required_at:
        return None
    if snapshot.changed_files_known and not applicable(snapshot.changed_files, job.paths, job.exclude_paths):
        return JobEntry(job_id, job.check, JobStatus.NOT_APPLICABLE, "no changed file matches this job's paths")
    holds = _filters_verdict(job, snapshot, policy)
    if holds is False and policy.path_filters.mode == "enforce":
        names = ", ".join((*job.filters, *job.filters_any))
        return JobEntry(job_id, job.check, JobStatus.NOT_APPLICABLE, f"path filters do not hold ({names})")
    run = checks.get(job.check)
    finished = run is not None and run.status == "completed"
    if mode is Mode.PR and policy.effective_locks(job) and not _has_result(run, holds):
        # Without a real result of its own, a closed lock holds the job back and its own check is ignored: a skipped
        # required check would otherwise count as a pass.
        held = locks.evaluate(snapshot, policy, job, labels)
        if held is not None:
            return JobEntry(job_id, job.check, JobStatus.LOCKED, held.reason, held.code, lock=held.lock)
    if _skipped_but_applicable(job, run, holds, snapshot, policy):
        names = ", ".join((*job.filters, *job.filters_any)) or ", ".join(job.paths)
        return JobEntry(
            job_id,
            job.check,
            JobStatus.FAILED,
            f"skipped, but the path rules ({names}) say it applies to these files",
        )
    if mode is Mode.PR and job.kind == "e2e" and job.needs_readiness and not finished:
        if not fork_secrets_authorized(snapshot, policy.trust):
            return JobEntry(job_id, job.check, JobStatus.WAITING, _AUTH_DETAIL)
        unlock = policy.e2e.unlock
        if unlock.mode == "policy":
            decision = e2e_unlock.decide(snapshot, unlock.signals_for(job.suite), unlock, policy.approval)
        else:
            decision = readiness.decide(labels, snapshot.reviews, snapshot.head_sha, snapshot.label_events)
        if not decision.allowed:
            return JobEntry(job_id, job.check, JobStatus.WAITING, decision.reason, decision.code)
    status, detail = _from_check(run)
    note = _filter_note(job, run, holds) if policy.path_filters.mode == "shadow" else ""
    return JobEntry(job_id, job.check, status, detail, note=note)


def _verdict(
    state: State,
    headline: str,
    mode: Mode,
    blockers: tuple[str, ...],
    jobs: tuple[JobEntry, ...],
    notes: tuple[str, ...] = (),
    native_approval: bool = False,
    next_override: tuple[str, str] | None = None,
) -> Verdict:
    next_action, who = next_override or _NEXT[state]
    if native_approval and state is State.AWAITING_APPROVAL and next_override is None:
        next_action, who = _NATIVE_APPROVAL_NEXT
    return Verdict(state, headline, next_action, who, mode, blockers, jobs, notes)


def _names(entries: Sequence[JobEntry]) -> str:
    return ", ".join(e.check for e in entries)


def _enqueue_gate_ok(snapshot: Snapshot, policy: Policy) -> bool:
    """Today's enqueue rule (auto-queue.sh): not a draft, and the required labels (or the native approval) are in order
    with no blocking label. It does not read any check result, which is the point: replays use it to ask whether the
    PR was *let in*, separately from whether it was ready."""
    if snapshot.is_draft:
        return False
    if missing_required(snapshot.labels, policy.merge.required_labels) or present_blocking(
        snapshot.labels, policy.merge.blocking_labels
    ):
        return False
    return policy.approval is None or approval.evaluate(snapshot, policy.approval).approved


def plan(snapshot: Snapshot, policy: Policy, mode: Mode = Mode.PR) -> Verdict:
    verdict = _plan(snapshot, policy, mode)
    return replace(verdict, label_gate_ok=True if mode is Mode.QUEUE else _enqueue_gate_ok(snapshot, policy))


def _unlock_next(held: Sequence[JobEntry], policy: Policy) -> tuple[str, str]:
    """What to do next for jobs held back by a lock about reviews and labels, and who does it."""
    if held[0].code == readiness.CODE_CHANGES_REQUESTED:
        # An approval or a CodeRabbit review cannot open anything until the change request is resolved.
        return _CHANGES_REQUESTED_NEXT, "author and the reviewer who requested changes"
    by_check = {j.check: j for j in policy.jobs.values()}
    groups = list(dict.fromkeys(locks.signals_of(policy, by_check[e.check], e.lock) for e in held))
    parts = [f"({locks.describe(g)})" if len(g) > 1 and len(groups) > 1 else locks.describe(g) for g in groups]
    return "get " + " and ".join(parts), "reviewer"


def _plan(snapshot: Snapshot, policy: Policy, mode: Mode) -> Verdict:
    checks = latest_checks(snapshot.check_runs)
    # With native approval an approved PR unlocks E2E the way the `lgtm` label does today.
    decision = approval.evaluate(snapshot, policy.approval) if policy.approval and mode is Mode.PR else None
    labels = snapshot.labels | {"lgtm"} if decision and decision.approved else snapshot.labels
    native = decision is not None
    entries = tuple(
        entry
        for job_id, job in policy.jobs.items()
        if (entry := _evaluate(job_id, job, snapshot, mode, checks, labels, policy)) is not None
    )
    notes = (decision.notes if decision else ()) + tuple(e.note for e in entries if e.note)
    kind = {job.check: job.kind for job in policy.jobs.values()}

    def where(status: JobStatus, job_kind: str | None = None) -> list[JobEntry]:
        return [e for e in entries if e.status is status and (job_kind is None or kind[e.check] == job_kind)]

    failed = where(JobStatus.FAILED)
    pending = where(JobStatus.RUNNING) + where(JobStatus.WAITING)
    held = where(JobStatus.LOCKED)  # only in pr mode: a lock is about starting a job, and the queue starts none
    # Failed first, then running, then the ones that wait or are locked in policy order (a locked job is a waiting one).
    waiting_or_locked = [e for e in entries if e.status in (JobStatus.WAITING, JobStatus.LOCKED)]
    blockers: list[str] = [f"{e.check}: {e.detail}" for e in failed + where(JobStatus.RUNNING) + waiting_or_locked]

    if mode is Mode.QUEUE:
        if failed:
            return _verdict(
                State.QUEUE_FAILED, f"queue check failed: {_names(failed)}", mode, tuple(blockers), entries, notes
            )
        if pending:
            return _verdict(
                State.QUEUE_CHECKS_RUNNING,
                f"queue checks still running: {_names(pending)}",
                mode,
                tuple(blockers),
                entries,
                notes,
            )
        return _verdict(State.QUEUE_PASSED, "all queue checks passed", mode, (), entries, notes)

    if snapshot.is_draft:
        return _verdict(
            State.DRAFT, "draft PR: not eligible for the merge queue", mode, tuple(blockers), entries, notes, native
        )

    missing = missing_required(snapshot.labels, policy.merge.required_labels)
    blocking = present_blocking(snapshot.labels, policy.merge.blocking_labels)
    label_problems = [f"missing label: {m}" for m in missing] + [f"blocking label: {b}" for b in blocking]
    if decision:
        label_problems += decision.problems
    guard = protected.evaluate(snapshot, policy.protected_paths) if policy.protected_paths else None
    if guard:
        label_problems += guard.problems
        notes = notes + guard.notes
    blockers += label_problems

    failed_cheap, failed_e2e = where(JobStatus.FAILED, "cheap"), where(JobStatus.FAILED, "e2e")
    if failed_cheap:
        return _verdict(
            State.CHECKS_FAILED,
            f"required checks failed: {_names(failed_cheap)}",
            mode,
            tuple(blockers),
            entries,
            notes,
            native,
        )
    if failed_e2e:
        return _verdict(
            State.E2E_FAILED, f"E2E failed: {_names(failed_e2e)}", mode, tuple(blockers), entries, notes, native
        )

    waiting_e2e = [e for e in where(JobStatus.WAITING, "e2e")]
    if any(e.detail == _AUTH_DETAIL for e in waiting_e2e) or any(e.code == locks.CODE_MEMBERSHIP for e in held):
        command = authorization_command(snapshot.head_sha)
        override = (
            (f"an org member comments `{command}`, which authorizes exactly this commit", "org member")
            if policy.trust.authorization == "sha-bound"
            else None
        )
        return _verdict(
            State.NEEDS_AUTHORIZATION, _AUTH_DETAIL, mode, tuple(blockers), entries, notes, native, override
        )

    pending_cheap = where(JobStatus.RUNNING, "cheap") + where(JobStatus.WAITING, "cheap")
    if pending_cheap:
        return _verdict(
            State.CHECKS_RUNNING,
            f"required checks still running: {_names(pending_cheap)}",
            mode,
            tuple(blockers),
            entries,
            notes,
            native,
        )
    if label_problems:
        override = None
        if guard and guard.problems:
            override = (
                f"get an approval from {', '.join(guard.approvers)} for the protected files",
                "protected-path approver",
            )
        return _verdict(
            State.AWAITING_APPROVAL, "; ".join(label_problems), mode, tuple(blockers), entries, notes, native, override
        )
    locked = [e for e in waiting_e2e if e.detail.startswith(("waiting:", "denied:"))]
    if locked:
        override = None
        if policy.e2e.unlock.mode == "policy" and locked[0].code == readiness.CODE_CHANGES_REQUESTED:
            # An approval or a CodeRabbit review cannot unlock anything until the change request is resolved.
            override = (_CHANGES_REQUESTED_NEXT, "author and the reviewer who requested changes")
        elif policy.e2e.unlock.mode == "policy":
            # Each locked suite needs one of its own signals, so alternatives stay grouped per suite ("or") and the
            # distinct requirements of different suites are joined with "and".
            by_check = {j.check: j for j in policy.jobs.values()}
            groups = list(dict.fromkeys(policy.e2e.unlock.signals_for(by_check[e.check].suite) for e in locked))
            override = (f"get {e2e_unlock.describe_requirements(groups)}", "reviewer")
        return _verdict(
            State.AWAITING_E2E_SIGNAL, locked[0].detail, mode, tuple(blockers), entries, notes, native, override
        )
    unlock_held = [e for e in held if e.code != locks.CODE_MEMBERSHIP]
    if unlock_held:
        return _verdict(
            State.AWAITING_UNLOCK,
            unlock_held[0].detail,
            mode,
            tuple(blockers),
            entries,
            notes,
            native,
            _unlock_next(unlock_held, policy),
        )
    if pending:
        return _verdict(
            State.E2E_RUNNING, f"E2E in progress: {_names(pending)}", mode, tuple(blockers), entries, notes, native
        )

    nothing_ran = bool(entries) and all(e.status is JobStatus.NOT_APPLICABLE for e in entries)
    suffix = " (no required job applies to the changed files)" if nothing_ran else ""
    if snapshot.in_merge_queue:
        return _verdict(State.IN_QUEUE, "in the merge queue" + suffix, mode, (), entries, notes, native)
    return _verdict(
        State.READY_TO_ENQUEUE, "all PR-time requirements are met" + suffix, mode, (), entries, notes, native
    )


def plan_or_error(snapshot: Snapshot, policy: Policy, mode: Mode = Mode.PR) -> Verdict:
    """Fail closed: any exception becomes a `planner-error` verdict with a cause and a re-run hint."""
    try:
        return plan(snapshot, policy, mode)
    except Exception as exc:  # noqa: BLE001 - the whole point is to never let a planner bug pass as success
        return error_verdict(f"{type(exc).__name__}: {exc}", mode)


def error_verdict(message: str, mode: Mode = Mode.PR) -> Verdict:
    """The verdict for anything that stops the planner from answering, including failing to read the PR."""
    # Fail closed: a planner that could not decide must never read as "the enqueue gate held" (replays use this flag).
    return replace(_verdict(State.PLANNER_ERROR, f"planner error: {message}", mode, (), ()), label_gate_ok=False)
