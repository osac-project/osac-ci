"""enforce mode with skipped_applicable: fail: a skipped check of an applicable job did not do its work."""

import pytest
from helpers import snap, with_check

from osac_ci.model import CheckRun, JobStatus, State
from osac_ci.planner import plan
from osac_ci.policy import PathFilters, Policy, PolicyError, parse_policy

pytestmark = pytest.mark.unit

SKIPPED = CheckRun("Run unit tests", "completed", "skipped")


def strict(policy: Policy, skipped: str = "fail", mode: str = "enforce") -> Policy:
    settings = policy.path_filters.model_copy(update={"mode": mode, "skipped_applicable": skipped})
    return policy.model_copy(update={"path_filters": settings})


def test_default_keeps_the_github_meaning_of_a_skip(osac_policy: Policy) -> None:
    policy = strict(osac_policy, "pass")
    s = snap(
        policy, changed_files=("fulfillment-service/a.go",), check_runs=with_check(policy, "Run unit tests", SKIPPED)
    )
    assert plan(s, policy).state is State.READY_TO_ENQUEUE


def test_a_skip_of_an_applicable_job_fails_it(osac_policy: Policy) -> None:
    policy = strict(osac_policy)
    s = snap(
        policy, changed_files=("fulfillment-service/a.go",), check_runs=with_check(policy, "Run unit tests", SKIPPED)
    )
    v = plan(s, policy)
    assert v.state is State.CHECKS_FAILED and "Run unit tests" in v.headline
    entry = next(e for e in v.jobs if e.check == "Run unit tests")
    assert entry.status is JobStatus.FAILED and "skipped, but the path rules" in entry.detail


def test_a_skip_of_a_job_that_does_not_apply_is_fine(osac_policy: Policy) -> None:
    policy = strict(osac_policy)
    s = snap(policy, changed_files=("osac-ui/src/a.ts",), check_runs=with_check(policy, "Run unit tests", SKIPPED))
    entry = next(e for e in plan(s, policy).jobs if e.check == "Run unit tests")
    assert entry.status is JobStatus.NOT_APPLICABLE


def test_no_file_list_means_no_verdict_from_the_filters(osac_policy: Policy) -> None:
    policy = strict(osac_policy)
    s = snap(policy, changed_files=(), check_runs=with_check(policy, "Run unit tests", SKIPPED))
    entry = next(e for e in plan(s, policy).jobs if e.check == "Run unit tests")
    assert entry.status is JobStatus.PASSED


def test_a_readiness_gated_job_is_exempt(osac_policy: Policy) -> None:
    policy = strict(osac_policy)
    gate = CheckRun("e2e-vmaas-gate", "completed", "skipped")
    s = snap(policy, changed_files=("osac-operator/a.go",), check_runs=with_check(policy, "e2e-vmaas-gate", gate))
    entry = next(e for e in plan(s, policy).jobs if e.check == "e2e-vmaas-gate")
    assert entry.status is not JobStatus.FAILED


def test_a_job_with_globs_is_judged_the_same_way() -> None:
    policy = parse_policy(
        "version: 1\nrepo: o/r\npath_filters: {mode: enforce, skipped_applicable: fail}\n"
        "merge: {required_labels: []}\njobs: {ui: {check: ui, paths: ['ui/**']}}\n"
    )
    skipped = (CheckRun("ui", "completed", "skipped"),)
    assert plan(snap(policy, changed_files=("ui/a.ts",), check_runs=skipped), policy).state is State.CHECKS_FAILED
    assert plan(snap(policy, changed_files=("go/a.go",), check_runs=skipped), policy).state is State.READY_TO_ENQUEUE


def test_the_option_needs_enforce_mode() -> None:
    with pytest.raises(PolicyError, match="needs path_filters.mode: enforce"):
        parse_policy("version: 1\nrepo: o/r\npath_filters: {skipped_applicable: fail}\njobs: {a: {check: a}}\n")
    assert PathFilters().skipped_applicable == "pass"


def test_shadow_mode_never_acts_on_the_option(osac_policy: Policy) -> None:
    """A policy file cannot combine them (validated), but the planner must not rely on that."""
    policy = strict(osac_policy, "fail", mode="shadow")
    s = snap(
        policy, changed_files=("fulfillment-service/a.go",), check_runs=with_check(policy, "Run unit tests", SKIPPED)
    )
    assert plan(s, policy).state is State.READY_TO_ENQUEUE


def test_a_job_that_runs_on_every_pr_and_was_skipped_fails(osac_policy: Policy) -> None:
    """pre-commit has no filter at all: nothing narrows it, so a skip is a workflow that did not do its work."""
    policy = strict(osac_policy)
    skipped = CheckRun("pre-commit", "completed", "skipped")
    s = snap(policy, changed_files=("docs/readme.md",), check_runs=with_check(policy, "pre-commit", skipped))
    v = plan(s, policy)
    assert v.state is State.CHECKS_FAILED and "pre-commit" in v.headline


def test_a_filtered_job_with_no_file_list_keeps_its_skip(osac_policy: Policy) -> None:
    policy = strict(osac_policy)
    s = snap(policy, changed_files=(), check_runs=with_check(policy, "Run unit tests", SKIPPED))
    assert plan(s, policy).state is State.READY_TO_ENQUEUE


def test_a_glob_job_with_an_unreadable_file_list_keeps_its_skip() -> None:
    policy = parse_policy(
        "version: 1\nrepo: o/r\npath_filters: {mode: enforce, skipped_applicable: fail}\n"
        "merge: {required_labels: []}\njobs: {ui: {check: ui, paths: ['ui/**']}}\n"
    )
    skipped = (CheckRun("ui", "completed", "skipped"),)
    s = snap(policy, changed_files=(), changed_files_known=False, check_runs=skipped)
    assert plan(s, policy).state is State.READY_TO_ENQUEUE


def test_a_queue_commit_is_judged_the_same_way(osac_policy: Policy) -> None:
    from osac_ci.model import Mode

    policy = strict(osac_policy)
    skipped = CheckRun("pre-commit", "completed", "skipped")
    s = snap(policy, changed_files=("a.go",), check_runs=with_check(policy, "pre-commit", skipped))
    assert plan(s, policy, Mode.QUEUE).state is State.QUEUE_FAILED
