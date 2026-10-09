"""osac-ui checks are mandatory exactly when osac-ui changes, and never otherwise."""

import pytest
from helpers import snap, with_check

from osac_ci.model import CheckRun, JobStatus, Mode, State
from osac_ci.planner import plan
from osac_ci.policy import Policy

pytestmark = pytest.mark.unit

UI_CHECKS = ("Lint", "Lint proxy (Go)", "Typecheck", "Run unit tests (osac-ui)")
UI_FILE = "osac-ui/src/App.tsx"


def entries(policy: Policy, files: tuple[str, ...], runs: tuple[CheckRun, ...], mode: Mode = Mode.PR):  # type: ignore[no-untyped-def]
    verdict = plan(snap(policy, changed_files=files, check_runs=runs), policy, mode)
    return verdict, {e.check: e for e in verdict.jobs}


def without(policy: Policy, *checks: str) -> tuple[CheckRun, ...]:
    return tuple(r for r in snap(policy).check_runs if r.name not in checks)


def test_a_pr_that_changes_osac_ui_waits_for_its_checks(osac_policy: Policy) -> None:
    verdict, jobs = entries(osac_policy, (UI_FILE,), without(osac_policy, *UI_CHECKS))
    assert all(jobs[c].status is JobStatus.WAITING for c in UI_CHECKS)
    assert verdict.state is State.CHECKS_RUNNING


def test_a_failed_osac_ui_check_blocks_the_pr(osac_policy: Policy) -> None:
    runs = with_check(osac_policy, "Typecheck", CheckRun("Typecheck", "completed", "failure"))
    verdict, jobs = entries(osac_policy, (UI_FILE, "docs/a.md"), runs)
    assert jobs["Typecheck"].status is JobStatus.FAILED and verdict.state is State.CHECKS_FAILED


def test_a_green_osac_ui_pr_is_ready(osac_policy: Policy) -> None:
    verdict, jobs = entries(osac_policy, (UI_FILE,), snap(osac_policy).check_runs)
    assert all(jobs[c].status is JobStatus.PASSED for c in UI_CHECKS) and verdict.state is State.READY_TO_ENQUEUE


def test_a_pr_that_does_not_touch_osac_ui_never_waits_for_them(osac_policy: Policy) -> None:
    verdict, jobs = entries(osac_policy, ("fulfillment-service/a.go",), without(osac_policy, *UI_CHECKS))
    assert all(jobs[c].status is JobStatus.NOT_APPLICABLE for c in UI_CHECKS)
    assert verdict.state is State.READY_TO_ENQUEUE


def test_the_merge_queue_does_not_wait_for_workflows_that_never_run_there(osac_policy: Policy) -> None:
    verdict, jobs = entries(osac_policy, (UI_FILE,), without(osac_policy, *UI_CHECKS), Mode.QUEUE)
    assert not set(UI_CHECKS) & set(jobs) and verdict.state is State.QUEUE_PASSED
