"""The planner with an `approval:` policy: native reviews and CODEOWNERS instead of the lgtm/approved labels."""

import pytest
from helpers import HEAD, snap, with_check

from osac_ci.model import CheckRun, Mode, Review, State
from osac_ci.planner import plan, plan_or_error
from osac_ci.policy import Approval, Merge, Policy, parse_policy
from osac_ci.render import render_markdown

pytestmark = pytest.mark.unit

OLD = "c" * 40
JIRA = frozenset({"jira/valid-reference"})


@pytest.fixture
def native(osac_policy: Policy) -> Policy:
    return osac_policy.model_copy(
        update={"approval": Approval(), "merge": Merge(required_labels=("jira/valid-reference",))}
    )


def approved_by(user: str, commit: str = HEAD) -> Review:
    return Review(user=user, state="APPROVED", submitted_at="2026-10-01T10:00:00Z", commit_id=commit, id=1)


def test_policy_parses_an_approval_section() -> None:
    p = parse_policy("version: 1\nrepo: o/r\napproval: {min_approvals: 2, carry_over: never}\njobs: {a: {check: a}}\n")
    assert p.approval == Approval(min_approvals=2, require_code_owners=True, carry_over="never")


def test_policy_rejects_unknown_approval_keys_and_values() -> None:
    from osac_ci.policy import PolicyError

    for bad in ("approval: {min_approvals: 0}", "approval: {carry_over: always}", "approval: {surprise: 1}"):
        with pytest.raises(PolicyError):
            parse_policy(f"version: 1\nrepo: o/r\n{bad}\njobs: {{a: {{check: a}}}}\n")


def test_without_an_approval_section_nothing_changes(osac_policy: Policy) -> None:
    assert osac_policy.approval is None
    v = plan(snap(osac_policy), osac_policy)
    assert v.state is State.READY_TO_ENQUEUE and v.notes == ()


def test_unapproved_pr_awaits_a_code_owner_not_labels(native: Policy) -> None:
    v = plan(snap(native, labels=JIRA), native)
    assert v.state is State.AWAITING_APPROVAL
    assert "approving review" in v.headline and "lgtm" not in v.headline
    assert (v.next_action, v.who_must_act) == (
        "get a code owner to approve the current changes and clear any blocking label",
        "code owner",
    )


def test_approved_pr_is_ready_without_the_lgtm_and_approved_labels(native: Policy) -> None:
    v = plan(snap(native, labels=JIRA, reviews=(approved_by("bob"),)), native)
    assert v.state is State.READY_TO_ENQUEUE and v.blockers == ()


def test_the_other_label_requirements_still_apply(native: Policy) -> None:
    v = plan(snap(native, labels=frozenset(), reviews=(approved_by("bob"),)), native)
    assert v.state is State.AWAITING_APPROVAL and "missing label: jira/valid-reference" in v.headline


def test_blocking_labels_still_block_an_approved_pr(native: Policy) -> None:
    v = plan(snap(native, labels=JIRA | {"do-not-merge/hold"}, reviews=(approved_by("bob"),)), native)
    assert v.state is State.AWAITING_APPROVAL and "do-not-merge/hold" in v.headline


def test_rebased_pr_keeps_its_approval_and_says_so(native: Policy) -> None:
    s = snap(native, labels=JIRA, reviews=(approved_by("bob", OLD),), change_fingerprints={HEAD: "same", OLD: "same"})
    v = plan(s, native)
    assert v.state is State.READY_TO_ENQUEUE
    assert v.notes and "carried over" in v.notes[0]
    assert "**Notes:**" in render_markdown(v)


def test_pr_with_changed_code_loses_the_approval(native: Policy) -> None:
    s = snap(native, labels=JIRA, reviews=(approved_by("bob", OLD),), change_fingerprints={HEAD: "new", OLD: "old"})
    v = plan(s, native)
    assert v.state is State.AWAITING_APPROVAL and "out of date" in v.headline


def test_failing_checks_are_reported_before_the_approval(native: Policy) -> None:
    cheap = next(j.check for j in native.jobs.values() if j.kind == "cheap")
    runs = with_check(native, cheap, CheckRun(cheap, "completed", "failure"))
    v = plan(snap(native, labels=JIRA, check_runs=runs), native)
    assert v.state is State.CHECKS_FAILED


def test_native_approval_unlocks_e2e_like_lgtm_does(native: Policy) -> None:
    gate = next(j.check for j in native.jobs.values() if j.needs_readiness)
    runs = with_check(native, gate, None)
    waiting = plan(snap(native, labels=JIRA, check_runs=runs), native)
    unlocked = plan(snap(native, labels=JIRA, check_runs=runs, reviews=(approved_by("bob"),)), native)
    assert waiting.state is State.AWAITING_APPROVAL
    assert unlocked.state is State.E2E_RUNNING and not any("waiting:" in b for b in unlocked.blockers)


def test_unreadable_owner_team_is_a_planner_error_not_a_pass(native: Policy) -> None:
    s = snap(
        native, labels=JIRA, reviews=(approved_by("bob"),), codeowners="* @org/team\n", team_members={"org/team": None}
    )
    v = plan_or_error(s, native)
    assert v.state is State.PLANNER_ERROR and "@org/team" in v.headline


def test_queue_mode_ignores_approval(native: Policy) -> None:
    v = plan(snap(native, labels=frozenset()), native, Mode.QUEUE)
    assert v.state is State.QUEUE_PASSED
