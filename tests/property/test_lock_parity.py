"""The lock form reproduces today's E2E gate exactly.

The goal is to turn the new mechanism on without changing what any pull request is told. So a policy written with
``locks`` must give the same answer as the same policy written with ``needs_readiness`` and ``e2e.unlock``, for any
snapshot. The one deliberate difference is a gate that reports ``skipped`` while nobody has unlocked it: the old form
counts that as a pass, the lock form does not (and the test below says so)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from helpers import HEAD, ROOT
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_planner_properties import snapshots

from osac_ci.model import CheckRun, JobStatus, Mode, Snapshot, State, Verdict
from osac_ci.planner import plan
from osac_ci.policy import Approval, E2EUnlock, Lock, Policy, load_policy

pytestmark = pytest.mark.property
settings.register_profile("parity", max_examples=300, deadline=None, suppress_health_check=list(HealthCheck))
settings.load_profile("parity")

LEGACY = load_policy(ROOT / "policy" / "osac.yml")
SHA_BOUND = LEGACY.model_copy(update={"trust": LEGACY.trust.model_copy(update={"authorization": "sha-bound"})})
POLICY_MODE = LEGACY.model_copy(
    update={
        "approval": Approval(min_approvals=1, require_code_owners=False),
        "e2e": LEGACY.e2e.model_copy(
            update={
                "unlock": E2EUnlock(
                    mode="policy",
                    any_of=("human-approval", "coderabbit-approval"),
                    per_suite={"bmaas/sanity": ("human-approval",)},
                )
            }
        ),
    }
)
LEGACY_NATIVE = LEGACY.model_copy(  # the label ladder, with approval from reviews: an approved PR counts as lgtm
    update={
        "approval": Approval(min_approvals=1, require_code_owners=False),
        "merge": LEGACY.merge.model_copy(update={"required_labels": ("jira/valid-reference",)}),
    }
)
E2E_CHECKS = {job.check for job in LEGACY.jobs.values() if job.kind == "e2e"}


def with_locks(policy: Policy) -> Policy:
    """The same policy, with every needs_readiness job rewritten to use the lock form."""
    unlock = policy.e2e.unlock
    cost = (
        Lock(open_when=("legacy-readiness",))
        if unlock.mode == "legacy"
        else Lock(open_when=unlock.any_of, block_on_changes_requested=unlock.block_on_changes_requested)
    )
    jobs = {}
    for job_id, job in policy.jobs.items():
        if not job.needs_readiness:
            jobs[job_id] = job
            continue
        overrides = {}
        if unlock.mode == "policy" and job.suite in unlock.per_suite:
            overrides = {"cost": unlock.per_suite[job.suite]}
        jobs[job_id] = job.model_copy(
            update={"needs_readiness": False, "locks": ("membership", "cost"), "lock_overrides": overrides}
        )
    locks = {"membership": Lock(open_when=("org-member", "trusted-bot", "authorized-commit")), "cost": cost}
    return policy.model_copy(update={"jobs": jobs, "locks": locks})


def no_skipped_gates(s: Snapshot) -> Snapshot:
    """A gate that reports skipped is the one place the two forms differ on purpose; keep it out of the comparison."""
    runs = tuple(
        CheckRun(r.name, "completed", "success") if r.name in E2E_CHECKS and r.conclusion == "skipped" else r
        for r in s.check_runs
    )
    return replace(s, check_runs=runs)


@st.composite
def varied(draw: st.DrawFn) -> Snapshot:
    s = draw(snapshots())
    return replace(
        s,
        fork_owner_is_org_member=draw(st.booleans()),
        fork_owner=draw(st.sampled_from(["", "ops", s.author])),
        authorized_by=draw(st.sampled_from(["", "carol"])),
        author=draw(st.sampled_from(["alice", "dependabot[bot]"])),
        is_fork=s.is_fork,
    )


def normalize(v: Verdict, *, wording: bool) -> dict[str, Any]:
    state = State.AWAITING_E2E_SIGNAL if v.state is State.AWAITING_UNLOCK else v.state
    jobs = [
        (
            e.job_id,
            e.check,
            JobStatus.WAITING if e.status is JobStatus.LOCKED else e.status,
            e.detail,
            "" if e.code == "membership" else e.code,  # the old form never named this kind of wait
        )
        for e in v.jobs
    ]
    out: dict[str, Any] = {
        "state": state,
        "headline": v.headline,
        "blockers": v.blockers,
        "jobs": jobs,
        "notes": v.notes,
        "label_gate_ok": v.label_gate_ok,
        "who": v.who_must_act,
    }
    if wording:
        out["next"] = v.next_action
    return out


@pytest.mark.parametrize(
    "policy",
    [LEGACY, SHA_BOUND, POLICY_MODE, LEGACY_NATIVE],
    ids=["labels", "sha-bound", "policy-mode", "labels-with-native-approval"],
)
def test_the_lock_form_gives_the_same_verdict_as_the_old_gate(policy: Policy) -> None:
    converted = with_locks(policy)
    # The legacy ladder words its next step differently ("get an lgtm, ..." against "get /lgtm, ..."): compare
    # everything else there, and the words too where the old form generates them from the same signals.
    same_words = policy.e2e.unlock.mode == "policy"

    @given(varied(), st.sampled_from(list(Mode)))
    def check(s: Snapshot, mode: Mode) -> None:
        s = no_skipped_gates(s)
        old, new = plan(s, policy, mode), plan(s, converted, mode)
        awaiting = old.state is State.AWAITING_E2E_SIGNAL
        assert normalize(old, wording=same_words or not awaiting) == normalize(new, wording=same_words or not awaiting)

    check()


def gate_entry(policy: Policy, files: tuple[str, ...], conclusion: str) -> JobStatus:
    from helpers import snap

    gate = next(j for j in LEGACY.jobs.values() if j.needs_readiness)
    runs = [CheckRun(j.check, "completed", "success") for j in LEGACY.jobs.values() if j.check != gate.check]
    runs.append(CheckRun(gate.check, "completed", conclusion))
    s = snap(policy, labels=frozenset({"jira/valid-reference"}), changed_files=files, check_runs=tuple(runs))
    return next(e for e in plan(s, policy).jobs if e.check == gate.check).status


def test_a_gate_that_skipped_on_files_it_does_not_apply_to_is_a_pass_in_both_forms() -> None:
    """What real pull requests show: the workflow reports the gate as skipped when its own path filter says E2E is not
    needed. The lock form must not turn that into a hold."""
    for policy in (LEGACY, with_locks(LEGACY)):
        assert gate_entry(policy, ("fulfillment-service/internal/servers/a_test.go",), "skipped") is JobStatus.PASSED


def test_the_only_difference_is_a_gate_that_skipped_on_relevant_files_while_locked() -> None:
    from helpers import snap

    relevant = ("osac-operator/a.go",)
    assert gate_entry(LEGACY, relevant, "skipped") is JobStatus.PASSED
    assert gate_entry(with_locks(LEGACY), relevant, "skipped") is JobStatus.LOCKED
    assert snap(LEGACY).head_sha == HEAD
