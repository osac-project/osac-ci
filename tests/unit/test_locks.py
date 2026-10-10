"""Locks: any job can be held back until something says it may start, and a lock is neither a failure nor a pass."""

from __future__ import annotations

from typing import Any

import pytest
from helpers import HEAD, OTHER, snap

from osac_ci.model import CheckRun, JobStatus, LabelEvent, Mode, Review, State
from osac_ci.planner import plan
from osac_ci.policy import PolicyError, parse_policy
from osac_ci.publish import BROKEN, OUTCOME, PERSON_NEEDED
from osac_ci.render import render_markdown
from osac_ci.rules import locks as lock_rules

pytestmark = pytest.mark.unit

BASE = "version: 1\nrepo: o/r\nmerge: {required_labels: [], blocking_labels: []}\n"


def policy(extra: str = "", jobs: str = "{a: {check: a}}"):  # type: ignore[no-untyped-def]
    return parse_policy(BASE + extra + f"jobs: {jobs}\n")


MEMBERSHIP = "locks: {membership: {open_when: [org-member, trusted-bot, authorized-commit]}}\n"
REVIEW = "locks: {review: {open_when: [coderabbit-approval, lgtm-label]}}\n"
BOTH = (
    "locks:\n  membership: {open_when: [org-member, trusted-bot, authorized-commit]}\n"
    "  review: {open_when: [coderabbit-approval, lgtm-label]}\n"
)


def approved_by_coderabbit(commit: str = HEAD) -> Review:
    return Review("coderabbitai[bot]", "APPROVED", "Bot", "2026-10-01T10:00:00Z", 1, commit)


def run(policy_, files=("a.go",), **kw: Any):  # type: ignore[no-untyped-def]
    runs = kw.pop("check_runs", ())
    return plan(
        snap(policy_, changed_files=files, check_runs=runs, labels=kw.pop("labels", frozenset()), **kw), policy_
    )


# ---- the policy ---------------------------------------------------------------------------------------------------


def test_the_policy_takes_locks_defaults_and_per_job_lists() -> None:
    p = policy(
        BOTH + "default_locks: [membership]\n",
        "{a: {check: a}, b: {check: b, locks: [membership, review]}, c: {check: c, locks: []}}",
    )
    assert p.effective_locks(p.jobs["a"]) == ("membership",)
    assert p.effective_locks(p.jobs["b"]) == ("membership", "review")
    assert p.effective_locks(p.jobs["c"]) == ()  # an empty list opts out of the defaults


def test_a_job_on_the_old_readiness_gate_takes_no_default_lock() -> None:
    p = policy(
        MEMBERSHIP + "default_locks: [membership]\n",
        "{e: {kind: e2e, check: e, needs_readiness: true}, a: {check: a}}",
    )
    assert p.effective_locks(p.jobs["e"]) == () and p.effective_locks(p.jobs["a"]) == ("membership",)


@pytest.mark.parametrize(
    "extra, jobs, message",
    [
        (MEMBERSHIP, "{a: {check: a, locks: [nope]}}", "not defined"),
        (MEMBERSHIP + "default_locks: [nope]\n", "{a: {check: a}}", "not defined"),
        (MEMBERSHIP + "default_locks: [membership, membership]\n", "{a: {check: a}}", "must not repeat"),
        (MEMBERSHIP, "{a: {check: a, locks: [membership, membership]}}", "repeats a lock"),
        ("locks: {membership: {open_when: []}}\n", "{a: {check: a}}", "at least 1 item"),
        ("locks: {membership: {open_when: [wizardry]}}\n", "{a: {check: a}}", "Input should be"),
        ("locks: {membership: {open_when: [org-member, org-member]}}\n", "{a: {check: a}}", "must not repeat"),
        ("locks: {m: {open_when: [legacy-readiness, lgtm-label]}}\n", "{a: {check: a}}", "list it alone"),
        ("locks: {Bad_Name: {open_when: [org-member]}}\n", "{a: {check: a}}", "must match"),
        ("locks: {m: {open_when: [org-member], surprise: 1}}\n", "{a: {check: a}}", "Extra inputs|surprise"),
        (MEMBERSHIP, "{a: {kind: e2e, check: a, needs_readiness: true, locks: [membership]}}", "use locks"),
        (MEMBERSHIP, "{a: {check: a, lock_overrides: {membership: [lgtm-label]}}}", "does not have"),
        (MEMBERSHIP, "{a: {check: a, locks: [membership], lock_overrides: {membership: []}}}", "bad signals"),
        (
            MEMBERSHIP,
            "{a: {check: a, locks: [membership], lock_overrides: {membership: [lgtm-label, lgtm-label]}}}",
            "bad signals",
        ),
        ("locks: {r: {open_when: [human-approval]}}\n", "{a: {check: a, locks: [r]}}", "needs an approval"),
    ],
)
def test_a_bad_lock_setup_is_refused(extra: str, jobs: str, message: str) -> None:
    with pytest.raises(PolicyError, match=message):
        policy(extra, jobs)


def test_human_approval_is_allowed_with_an_approval_section() -> None:
    p = policy(
        "approval: {min_approvals: 1}\nlocks: {r: {open_when: [human-approval]}}\n", "{a: {check: a, locks: [r]}}"
    )
    assert p.locks["r"].open_when == ("human-approval",)


# ---- who is asking (membership signals) ---------------------------------------------------------------------------


def verdict_entry(policy_, **kw: Any):  # type: ignore[no-untyped-def]
    v = run(policy_, **kw)
    return v, v.jobs[0]


def test_a_pull_request_from_a_branch_of_the_repository_is_open() -> None:
    _, entry = verdict_entry(policy(MEMBERSHIP, "{a: {check: a, locks: [membership]}}"), is_fork=False)
    assert entry.status is JobStatus.WAITING and entry.detail == "not reported yet"  # not locked: just not run yet


@pytest.mark.parametrize(
    "fields",
    [
        {"is_fork": True, "author_is_org_member": True},
        {"is_fork": True, "fork_owner": "ops", "fork_owner_is_org_member": True, "author": "bot-account"},
    ],
)
def test_a_member_or_a_member_owned_fork_is_open(fields: dict[str, Any]) -> None:
    _, entry = verdict_entry(policy(MEMBERSHIP, "{a: {check: a, locks: [membership]}}"), **fields)
    assert entry.status is not JobStatus.LOCKED


def test_an_outsider_locks_the_job_and_it_is_not_a_failure() -> None:
    p = policy(MEMBERSHIP, "{a: {check: a, locks: [membership]}}")
    v, entry = verdict_entry(p, is_fork=True, author="mallory")
    assert entry.status is JobStatus.LOCKED and entry.lock == "membership" and entry.code == lock_rules.CODE_MEMBERSHIP
    assert v.state is State.NEEDS_AUTHORIZATION and v.headline == lock_rules.AUTH_DETAIL
    assert OUTCOME[v.state] == PERSON_NEEDED and OUTCOME[v.state] != BROKEN  # yellow, never red


def test_a_trusted_bot_opens_it() -> None:
    p = policy(MEMBERSHIP + "trust: {trusted_bots: ['dependabot[bot]']}\n", "{a: {check: a, locks: [membership]}}")
    assert verdict_entry(p, is_fork=True, author="dependabot[bot]")[1].status is not JobStatus.LOCKED
    assert verdict_entry(p, is_fork=True, author="other[bot]")[1].status is JobStatus.LOCKED


def test_a_label_authorizes_in_label_mode_and_only_the_commit_check_in_sha_bound_mode() -> None:
    jobs = "{a: {check: a, locks: [membership]}}"
    label_mode = policy(MEMBERSHIP, jobs)
    assert (
        verdict_entry(label_mode, is_fork=True, author="x", labels=frozenset({"ok-to-test"}))[1].status
        is not JobStatus.LOCKED
    )
    bound = policy(MEMBERSHIP + "trust: {authorization: sha-bound}\n", jobs)
    assert (
        verdict_entry(bound, is_fork=True, author="x", labels=frozenset({"ok-to-test"}))[1].status is JobStatus.LOCKED
    )
    assert verdict_entry(bound, is_fork=True, author="x", authorized_by="carol")[1].status is not JobStatus.LOCKED


def test_sha_bound_mode_tells_the_org_member_which_command_to_post() -> None:
    p = policy(MEMBERSHIP + "trust: {authorization: sha-bound}\n", "{a: {check: a, locks: [membership]}}")
    v, _ = verdict_entry(p, is_fork=True, author="x")
    assert v.next_action == f"an org member comments `/ok-to-test {HEAD}`, which authorizes exactly this commit"


def test_a_lock_may_list_only_some_of_the_membership_signals() -> None:
    p = policy("locks: {m: {open_when: [org-member]}}\n", "{a: {check: a, locks: [m]}}")
    assert verdict_entry(p, is_fork=True, author="x", authorized_by="carol")[1].status is JobStatus.LOCKED


# ---- has anyone looked at it (review and label signals) -----------------------------------------------------------


def test_a_review_lock_waits_and_names_what_would_open_it() -> None:
    p = policy(REVIEW, "{a: {check: a, locks: [review]}}")
    v, entry = verdict_entry(p)
    assert entry.status is JobStatus.LOCKED and entry.lock == "review"
    assert v.state is State.AWAITING_UNLOCK
    assert (
        v.next_action == "get a CodeRabbit approval on the current commit or the lgtm label"
        and v.who_must_act == "reviewer"
    )
    assert OUTCOME[State.AWAITING_UNLOCK] == PERSON_NEEDED


@pytest.mark.parametrize(
    "fields",
    [{"reviews": (approved_by_coderabbit(),)}, {"labels": frozenset({"lgtm"})}],
)
def test_any_one_signal_opens_the_lock(fields: dict[str, Any]) -> None:
    assert verdict_entry(policy(REVIEW, "{a: {check: a, locks: [review]}}"), **fields)[1].status is not JobStatus.LOCKED


def test_coderabbit_must_have_approved_this_commit() -> None:
    p = policy(REVIEW, "{a: {check: a, locks: [review]}}")
    v, entry = verdict_entry(p, reviews=(approved_by_coderabbit(OTHER),))
    assert entry.status is JobStatus.LOCKED and "older commit" in entry.detail


def test_a_change_request_closes_the_lock_and_says_what_to_do() -> None:
    p = policy(REVIEW, "{a: {check: a, locks: [review]}}")
    human = Review("alice", "CHANGES_REQUESTED", "User", "2026-10-01T10:00:00Z", 2, HEAD)
    v, entry = verdict_entry(p, reviews=(approved_by_coderabbit(), human))
    assert entry.status is JobStatus.LOCKED and entry.code == "changes-requested"
    assert v.who_must_act == "author and the reviewer who requested changes"
    allowed = policy(
        "locks: {review: {open_when: [coderabbit-approval], block_on_changes_requested: false}}\n",
        "{a: {check: a, locks: [review]}}",
    )
    assert verdict_entry(allowed, reviews=(approved_by_coderabbit(), human))[1].status is not JobStatus.LOCKED


def test_human_approval_uses_the_approval_policy() -> None:
    p = policy(
        "approval: {min_approvals: 1, require_code_owners: false}\nlocks: {r: {open_when: [human-approval]}}\n",
        "{a: {check: a, locks: [r]}}",
    )
    assert verdict_entry(p)[1].status is JobStatus.LOCKED
    ok = Review("bob", "APPROVED", "User", "2026-10-01T10:00:00Z", 3, HEAD)
    assert verdict_entry(p, reviews=(ok,))[1].status is not JobStatus.LOCKED


def test_the_e2e_ready_label_counts_only_from_the_trusted_actor() -> None:
    p = policy("locks: {r: {open_when: [e2e-ready-label]}}\n", "{a: {check: a, locks: [r]}}")
    labels = frozenset({"e2e-ready"})
    mine = (LabelEvent("labeled", "e2e-ready", "mallory"),)
    bot = (LabelEvent("labeled", "e2e-ready", "github-actions[bot]"),)
    assert "untrusted actor" in verdict_entry(p, labels=labels, label_events=mine)[1].detail
    assert verdict_entry(p, labels=labels, label_events=bot)[1].status is not JobStatus.LOCKED


def test_the_legacy_ladder_keeps_a_sticky_lgtm() -> None:
    p = policy("locks: {r: {open_when: [legacy-readiness]}}\n", "{a: {check: a, locks: [r]}}")
    earlier = (LabelEvent("labeled", "lgtm", "alice"),)
    assert verdict_entry(p, label_events=earlier)[1].status is not JobStatus.LOCKED  # applied once, still open
    strict = policy("locks: {r: {open_when: [lgtm-label]}}\n", "{a: {check: a, locks: [r]}}")
    assert verdict_entry(strict, label_events=earlier)[1].status is JobStatus.LOCKED  # not sticky


def test_a_mixed_lock_opens_for_either_kind_of_signal_and_names_both() -> None:
    p = policy("locks: {m: {open_when: [org-member, coderabbit-approval]}}\n", "{a: {check: a, locks: [m]}}")
    assert verdict_entry(p, is_fork=True, author_is_org_member=True)[1].status is not JobStatus.LOCKED
    assert verdict_entry(p, is_fork=True, reviews=(approved_by_coderabbit(),))[1].status is not JobStatus.LOCKED
    closed = verdict_entry(p, is_fork=True)[1]
    assert (
        closed.status is JobStatus.LOCKED
        and "an author in the organization" in closed.detail
        and "CodeRabbit" in closed.detail
    )


# ---- several locks, defaults, overrides ---------------------------------------------------------------------------


def test_every_lock_must_be_open_and_the_first_closed_one_is_reported() -> None:
    p = policy(BOTH, "{a: {check: a, locks: [membership, review]}}")
    outsider = verdict_entry(p, is_fork=True, author="x")[1]
    assert outsider.lock == "membership"  # listed first
    member = verdict_entry(p, is_fork=True, author_is_org_member=True)[1]
    assert member.lock == "review"
    assert (
        verdict_entry(p, is_fork=True, author_is_org_member=True, labels=frozenset({"lgtm"}))[1].status
        is not JobStatus.LOCKED
    )


def test_a_job_can_replace_the_signals_of_a_lock() -> None:
    p = policy(REVIEW, "{a: {check: a, locks: [review], lock_overrides: {review: [lgtm-label]}}}")
    assert (
        verdict_entry(p, reviews=(approved_by_coderabbit(),))[1].status is JobStatus.LOCKED
    )  # CodeRabbit no longer enough
    assert verdict_entry(p, labels=frozenset({"lgtm"}))[1].status is not JobStatus.LOCKED


def test_default_locks_apply_to_every_job_unless_a_job_opts_out() -> None:
    p = policy(MEMBERSHIP + "default_locks: [membership]\n", "{a: {check: a}, b: {check: b, locks: []}}")
    v = run(p, is_fork=True, author="x")
    statuses = {e.job_id: e.status for e in v.jobs}
    assert statuses == {"a": JobStatus.LOCKED, "b": JobStatus.WAITING}


def test_two_locked_suites_with_different_needs_are_joined_with_and() -> None:
    p = policy(
        REVIEW + "",
        "{a: {check: a, locks: [review]}, b: {check: b, locks: [review], lock_overrides: {review: [lgtm-label]}}}",
    )
    v = run(p)
    assert v.state is State.AWAITING_UNLOCK
    assert v.next_action == "get (a CodeRabbit approval on the current commit or the lgtm label) and the lgtm label"


# ---- a lock is neither a failure nor a pass -----------------------------------------------------------------------

JOBS = "{a: {check: a, locks: [review]}}"


def test_a_locked_job_that_skipped_does_not_pass() -> None:
    p = policy(REVIEW, JOBS)
    v, entry = verdict_entry(p, check_runs=(CheckRun("a", "completed", "skipped"),))
    assert entry.status is JobStatus.LOCKED and v.state is State.AWAITING_UNLOCK


FILTERED = "path_filters: {filters: {backend: ['server/**'], docs: ['docs/**']}}\n" + REVIEW
FILTERED_JOB = "{a: {check: a, locks: [review], filters: [backend]}}"


def test_a_skip_on_files_the_filters_call_irrelevant_stands() -> None:
    """The workflows report a job they have nothing to do for as skipped; that is not a job held back."""
    p = policy(FILTERED, FILTERED_JOB)
    skipped = (CheckRun("a", "completed", "skipped"),)
    _, entry = verdict_entry(p, files=("docs/a.md",), check_runs=skipped)
    assert entry.status is JobStatus.PASSED and entry.lock == ""


def test_a_skip_on_files_the_filters_call_relevant_is_still_locked() -> None:
    p = policy(FILTERED, FILTERED_JOB)
    skipped = (CheckRun("a", "completed", "skipped"),)
    _, entry = verdict_entry(p, files=("server/a.go",), check_runs=skipped)
    assert entry.status is JobStatus.LOCKED


def test_a_pending_job_on_irrelevant_files_is_still_locked() -> None:
    """Only a skip carries the meaning "nothing to do"; a placeholder that waits is a job held back."""
    p = policy(FILTERED, FILTERED_JOB)
    waiting = (CheckRun("a", "in_progress"),)
    assert verdict_entry(p, files=("docs/a.md",), check_runs=waiting)[1].status is JobStatus.LOCKED


@pytest.mark.parametrize("status, conclusion", [("queued", None), ("in_progress", None)])
def test_a_locked_job_that_is_pending_is_locked_not_running(status: str, conclusion: str | None) -> None:
    """The gate placeholder of the old design looks like this: pending while nobody has unlocked it."""
    p = policy(REVIEW, JOBS)
    assert verdict_entry(p, check_runs=(CheckRun("a", status, conclusion),))[1].status is JobStatus.LOCKED


@pytest.mark.parametrize(
    "conclusion, expected",
    [("success", JobStatus.PASSED), ("failure", JobStatus.FAILED), ("cancelled", JobStatus.FAILED)],
)
def test_a_job_that_really_ran_keeps_its_result_even_if_the_lock_is_closed_now(
    conclusion: str, expected: JobStatus
) -> None:
    p = policy(REVIEW, JOBS)
    assert verdict_entry(p, check_runs=(CheckRun("a", "completed", conclusion),))[1].status is expected


def test_an_open_lock_leaves_a_skip_to_the_skip_policy() -> None:
    p = policy(REVIEW, JOBS)
    skipped = (CheckRun("a", "completed", "skipped"),)
    assert verdict_entry(p, labels=frozenset({"lgtm"}), check_runs=skipped)[1].status is JobStatus.PASSED


def test_a_failed_check_is_shown_before_a_lock() -> None:
    p = policy(REVIEW, "{a: {check: a, locks: [review]}, b: {check: b}}")
    v = run(p, check_runs=(CheckRun("b", "completed", "failure"),))
    assert v.state is State.CHECKS_FAILED


def test_approval_comes_before_the_unlock_wait() -> None:
    unapproved = parse_policy(
        BASE.replace("required_labels: []", "required_labels: [jira]") + REVIEW + f"jobs: {JOBS}\n"
    )
    v = plan(snap(unapproved, labels=frozenset(), check_runs=()), unapproved)
    assert v.state is State.AWAITING_APPROVAL and "missing label: jira" in v.headline  # reported before the lock


def test_a_draft_is_a_draft() -> None:
    assert run(policy(REVIEW, JOBS), is_draft=True).state is State.DRAFT


def test_a_job_that_does_not_apply_is_never_locked() -> None:
    p = policy(REVIEW, "{a: {check: a, locks: [review], paths: ['ui/**']}}")
    _, entry = verdict_entry(p, files=("go/a.go",))
    assert entry.status is JobStatus.NOT_APPLICABLE


def test_the_merge_queue_does_not_apply_locks() -> None:
    p = policy(REVIEW, JOBS)
    v = plan(snap(p, check_runs=(CheckRun("a", "completed", "success"),)), p, Mode.QUEUE)
    assert v.state is State.QUEUE_PASSED and all(e.status is not JobStatus.LOCKED for e in v.jobs)


def test_a_pr_with_every_lock_open_and_every_job_passed_is_ready() -> None:
    p = policy(REVIEW, JOBS)
    v = run(p, labels=frozenset({"lgtm"}), check_runs=(CheckRun("a", "completed", "success"),))
    assert v.state is State.READY_TO_ENQUEUE


def test_the_rendered_verdict_names_the_lock_next_to_the_reason() -> None:
    text = render_markdown(run(policy(REVIEW, JOBS)))
    assert "| a | a | locked |" in text and "(lock: review)" in text


def test_with_native_approval_an_approved_pr_counts_as_lgtm_for_the_legacy_ladder() -> None:
    """The old gate saw `lgtm` once the approval policy was satisfied; the lock form must see the same labels."""
    p = policy(
        "approval: {min_approvals: 1, require_code_owners: false}\nlocks: {r: {open_when: [legacy-readiness]}}\n",
        "{a: {check: a, locks: [r]}}",
    )
    assert verdict_entry(p)[1].status is JobStatus.LOCKED
    ok = Review("bob", "APPROVED", "User", "2026-10-01T10:00:00Z", 3, HEAD)
    assert verdict_entry(p, reviews=(ok,))[1].status is not JobStatus.LOCKED
