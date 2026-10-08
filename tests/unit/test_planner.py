import pytest
from helpers import HEAD, OK_LABELS, snap, with_check

from osac_ci.model import CheckRun, JobStatus, LabelEvent, Mode, Review, Snapshot, State
from osac_ci.planner import _NEXT, latest_checks, plan, plan_or_error

pytestmark = pytest.mark.unit

GATE = "e2e-vmaas-gate"


def done(name: str, conclusion: str, at: str | None = None) -> CheckRun:
    return CheckRun(name, "completed", conclusion, at)


def test_every_state_has_a_next_action_and_an_actor() -> None:
    assert set(_NEXT) == set(State)
    assert all(action and who for action, who in _NEXT.values())


def test_all_green_with_labels_is_ready(osac_policy) -> None:  # type: ignore[no-untyped-def]
    v = plan(snap(osac_policy), osac_policy)
    assert v.state is State.READY_TO_ENQUEUE and v.blockers == ()


def test_in_merge_queue(osac_policy) -> None:  # type: ignore[no-untyped-def]
    assert plan(snap(osac_policy, in_merge_queue=True), osac_policy).state is State.IN_QUEUE


def test_draft_wins(osac_policy) -> None:  # type: ignore[no-untyped-def]
    assert plan(snap(osac_policy, is_draft=True), osac_policy).state is State.DRAFT


def test_failed_cheap_check(osac_policy) -> None:  # type: ignore[no-untyped-def]
    runs = with_check(osac_policy, "pre-commit", done("pre-commit", "failure"))
    v = plan(snap(osac_policy, check_runs=runs), osac_policy)
    assert v.state is State.CHECKS_FAILED and "pre-commit" in v.headline


def test_unreported_cheap_check_is_running_not_green(osac_policy) -> None:  # type: ignore[no-untyped-def]
    runs = with_check(osac_policy, "pre-commit", None)
    v = plan(snap(osac_policy, check_runs=runs), osac_policy)
    assert v.state is State.CHECKS_RUNNING
    assert any("pre-commit: not reported yet" in b for b in v.blockers)


def test_skipped_counts_as_passing_and_says_so(osac_policy) -> None:  # type: ignore[no-untyped-def]
    runs = with_check(osac_policy, "pre-commit", done("pre-commit", "skipped"))
    v = plan(snap(osac_policy, check_runs=runs), osac_policy)
    assert v.state is State.READY_TO_ENQUEUE
    entry = next(e for e in v.jobs if e.check == "pre-commit")
    assert entry.status is JobStatus.PASSED and "skipped" in entry.detail


def test_missing_lgtm_is_awaiting_approval(osac_policy) -> None:  # type: ignore[no-untyped-def]
    v = plan(snap(osac_policy, labels=OK_LABELS - {"lgtm"}), osac_policy)
    assert v.state is State.AWAITING_APPROVAL and "missing label: lgtm" in v.headline


def test_blocking_label_is_awaiting_approval(osac_policy) -> None:  # type: ignore[no-untyped-def]
    v = plan(snap(osac_policy, labels=OK_LABELS | {"needs-rebase"}), osac_policy)
    assert v.state is State.AWAITING_APPROVAL and "blocking label: needs-rebase" in v.headline


def test_e2e_in_progress_after_unlock(osac_policy) -> None:  # type: ignore[no-untyped-def]
    runs = with_check(osac_policy, GATE, CheckRun(GATE, "in_progress"))
    v = plan(snap(osac_policy, check_runs=runs), osac_policy)
    assert v.state is State.E2E_RUNNING


def test_e2e_failed(osac_policy) -> None:  # type: ignore[no-untyped-def]
    runs = with_check(osac_policy, GATE, done(GATE, "failure"))
    assert plan(snap(osac_policy, check_runs=runs), osac_policy).state is State.E2E_FAILED


def test_locked_e2e_waits_for_a_signal_and_names_the_reason(osac_policy) -> None:  # type: ignore[no-untyped-def]
    # OSAC requires lgtm for merging and lgtm unlocks E2E, so model a policy that does not require it.
    policy = osac_policy.model_copy(
        update={"merge": osac_policy.merge.model_copy(update={"required_labels": ("approved",)})}
    )
    runs = with_check(policy, GATE, None)
    v = plan(snap(policy, labels=frozenset({"approved"}), check_runs=runs), policy)
    assert v.state is State.AWAITING_E2E_SIGNAL
    assert v.headline == "waiting: no CR APPROVED on this SHA"


def test_in_progress_gate_placeholder_while_locked_is_waiting_not_running(osac_policy) -> None:  # type: ignore[no-untyped-def]
    policy = osac_policy.model_copy(
        update={"merge": osac_policy.merge.model_copy(update={"required_labels": ("approved",)})}
    )
    runs = with_check(policy, GATE, CheckRun(GATE, "in_progress"))
    v = plan(snap(policy, labels=frozenset({"approved"}), check_runs=runs), policy)
    assert v.state is State.AWAITING_E2E_SIGNAL


def test_coderabbit_approval_unlocks(osac_policy) -> None:  # type: ignore[no-untyped-def]
    policy = osac_policy.model_copy(
        update={"merge": osac_policy.merge.model_copy(update={"required_labels": ("approved",)})}
    )
    runs = with_check(policy, GATE, CheckRun(GATE, "in_progress"))
    review = Review("coderabbitai[bot]", "APPROVED", "Bot", "2026-01-01T00:00:01Z", 1, HEAD)
    v = plan(snap(policy, labels=frozenset({"approved"}), check_runs=runs, reviews=(review,)), policy)
    assert v.state is State.E2E_RUNNING


def test_unauthorized_fork_needs_authorization(osac_policy) -> None:  # type: ignore[no-untyped-def]
    runs = with_check(osac_policy, GATE, None)
    v = plan(snap(osac_policy, is_fork=True, author="alice", check_runs=runs), osac_policy)
    assert v.state is State.NEEDS_AUTHORIZATION


def test_ok_to_test_authorizes_the_fork(osac_policy) -> None:  # type: ignore[no-untyped-def]
    runs = with_check(osac_policy, GATE, CheckRun(GATE, "in_progress"))
    labels = OK_LABELS | {"ok-to-test"}
    v = plan(snap(osac_policy, is_fork=True, author="alice", labels=labels, check_runs=runs), osac_policy)
    assert v.state is State.E2E_RUNNING


def test_finished_gate_is_not_blocked_by_fork_authorization(osac_policy) -> None:  # type: ignore[no-untyped-def]
    v = plan(snap(osac_policy, is_fork=True, author="alice"), osac_policy)
    assert v.state is State.READY_TO_ENQUEUE


def test_latest_run_per_check_name_wins(osac_policy) -> None:  # type: ignore[no-untyped-def]
    old_fail, new_pass = (
        done("pre-commit", "failure", "2026-01-01T00:00:01Z"),
        done("pre-commit", "success", "2026-01-01T00:00:09Z"),
    )
    runs = with_check(osac_policy, "pre-commit", None) + (new_pass, old_fail)  # order must not matter
    assert latest_checks(runs)["pre-commit"] is new_pass
    assert plan(snap(osac_policy, check_runs=runs), osac_policy).state is State.READY_TO_ENQUEUE


def test_failure_is_reported_before_missing_labels(osac_policy) -> None:  # type: ignore[no-untyped-def]
    runs = with_check(osac_policy, "pre-commit", done("pre-commit", "failure"))
    v = plan(snap(osac_policy, check_runs=runs, labels=frozenset()), osac_policy)
    assert v.state is State.CHECKS_FAILED
    assert any("missing label: lgtm" in b for b in v.blockers)  # still listed


# queue mode ---------------------------------------------------------------------------------------------


def test_queue_mode_passed_ignores_labels(osac_policy) -> None:  # type: ignore[no-untyped-def]
    v = plan(snap(osac_policy, labels=frozenset()), osac_policy, Mode.QUEUE)
    assert v.state is State.QUEUE_PASSED


def test_queue_mode_failed_and_running(osac_policy) -> None:  # type: ignore[no-untyped-def]
    failed = with_check(osac_policy, GATE, done(GATE, "failure"))
    running = with_check(osac_policy, GATE, CheckRun(GATE, "queued"))
    assert plan(snap(osac_policy, check_runs=failed), osac_policy, Mode.QUEUE).state is State.QUEUE_FAILED
    assert plan(snap(osac_policy, check_runs=running), osac_policy, Mode.QUEUE).state is State.QUEUE_CHECKS_RUNNING


def test_queue_mode_does_not_apply_pr_readiness_gate(osac_policy) -> None:  # type: ignore[no-untyped-def]
    runs = with_check(osac_policy, GATE, CheckRun(GATE, "in_progress"))
    v = plan(snap(osac_policy, labels=frozenset(), check_runs=runs), osac_policy, Mode.QUEUE)
    assert v.state is State.QUEUE_CHECKS_RUNNING


def test_queue_only_jobs_are_ignored_in_pr_mode(toy_policy) -> None:  # type: ignore[no-untyped-def]
    # toy `site` is required_at [pr] only: absent in queue mode.
    entries = plan(
        snap(toy_policy, labels=frozenset({"approved"}), changed_files=("docs/x.md",)), toy_policy, Mode.QUEUE
    ).jobs
    assert "site" not in {e.check for e in entries}


# a different repo -----------------------------------------------------------------------------------------


def test_toy_policy_uses_its_own_labels_and_paths(toy_policy) -> None:  # type: ignore[no-untyped-def]
    ok = snap(toy_policy, labels=frozenset({"approved"}), changed_files=("src/x.py",))
    v = plan(ok, toy_policy)
    assert v.state is State.READY_TO_ENQUEUE
    site = next(e for e in v.jobs if e.check == "site")
    assert site.status is JobStatus.NOT_APPLICABLE
    wip = snap(toy_policy, labels=frozenset({"approved", "wip"}), changed_files=("src/x.py",))
    assert plan(wip, toy_policy).state is State.AWAITING_APPROVAL


def test_nothing_applicable_is_said_out_loud(toy_policy) -> None:  # type: ignore[no-untyped-def]
    only_site = toy_policy.model_copy(update={"jobs": {"site": toy_policy.jobs["docs-only-site"]}})
    v = plan(snap(only_site, labels=frozenset({"approved"}), changed_files=("src/x.py",)), only_site)
    assert v.state is State.READY_TO_ENQUEUE and "no required job applies" in v.headline


# fail closed -------------------------------------------------------------------------------------------


def test_planner_errors_become_a_planner_error_verdict(toy_policy) -> None:  # type: ignore[no-untyped-def]
    broken = Snapshot(repo="o/r", number=1, head_sha="x", changed_files=None)  # type: ignore[arg-type]
    v = plan_or_error(broken, toy_policy)
    assert v.state is State.PLANNER_ERROR
    assert v.headline.startswith("planner error:") and "re-run" in v.next_action


def test_label_event_fixture_types(osac_policy) -> None:  # type: ignore[no-untyped-def]
    events = (LabelEvent("labeled", "lgtm", "alice"),)
    runs = with_check(osac_policy, GATE, CheckRun(GATE, "in_progress"))
    v = plan(
        snap(osac_policy, labels=OK_LABELS - {"lgtm"} | {"approved"}, label_events=events, check_runs=runs), osac_policy
    )
    assert v.state is State.AWAITING_APPROVAL  # lgtm label itself is still a merge requirement
