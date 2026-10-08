"""Properties that must hold for ANY snapshot, not just the hand-picked ones."""

from __future__ import annotations

from typing import Any

import pytest
from helpers import HEAD, OTHER, ROOT
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from osac_ci.model import CheckRun, JobStatus, LabelEvent, Mode, Review, Snapshot, State
from osac_ci.planner import plan, plan_or_error
from osac_ci.policy import load_policy
from osac_ci.rules.labels import queue_labels_ok

pytestmark = pytest.mark.property
POLICY = load_policy(ROOT / "policy" / "osac.yml")
CHECKS = [job.check for job in POLICY.jobs.values()]
LABELS = ["lgtm", "approved", "jira/valid-reference", "do-not-merge/hold", "needs-rebase", "ok-to-test", "e2e-ready"]
READY = {State.READY_TO_ENQUEUE, State.IN_QUEUE}
settings.register_profile("ci", max_examples=150, deadline=None, suppress_health_check=list(HealthCheck))
settings.load_profile("ci")


@st.composite
def check_run(draw: st.DrawFn, name: str) -> CheckRun | None:
    kind = draw(st.sampled_from(["absent", "queued", "in_progress", "success", "failure", "skipped", "cancelled"]))
    if kind == "absent":
        return None
    if kind in {"queued", "in_progress"}:
        return CheckRun(name, kind)
    return CheckRun(name, "completed", kind)


@st.composite
def reviews(draw: st.DrawFn) -> tuple[Review, ...]:
    n = draw(st.integers(0, 4))
    return tuple(
        Review(
            user=draw(st.sampled_from(["alice", "bob", "coderabbitai[bot]"])),
            state=draw(st.sampled_from(["APPROVED", "CHANGES_REQUESTED", "DISMISSED", "COMMENTED"])),
            user_type="Bot" if draw(st.booleans()) else "User",
            submitted_at=draw(st.sampled_from(["2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z", None])),
            id=i + 1,
            commit_id=draw(st.sampled_from([HEAD, OTHER, None])),
        )
        for i in range(n)
    )


@st.composite
def snapshots(draw: st.DrawFn) -> Snapshot:
    runs = [r for name in CHECKS if (r := draw(check_run(name))) is not None]
    events = tuple(
        LabelEvent(
            "labeled",
            draw(st.sampled_from(["lgtm", "e2e-ready"])),
            draw(st.sampled_from(["github-actions[bot]", "alice"])),
        )
        for _ in range(draw(st.integers(0, 2)))
    )
    return Snapshot(
        repo=POLICY.repo,
        number=1,
        head_sha=HEAD,
        is_draft=draw(st.booleans()),
        is_fork=draw(st.booleans()),
        author="alice",
        author_is_org_member=draw(st.booleans()),
        labels=frozenset(draw(st.lists(st.sampled_from(LABELS), unique=True))),
        reviews=draw(reviews()),
        label_events=events,
        check_runs=tuple(runs),
        changed_files=("a.go",),
        in_merge_queue=draw(st.booleans()),
    )


@given(snapshots(), st.sampled_from(list(Mode)))
def test_deterministic_and_never_raises(s: Snapshot, mode: Mode) -> None:
    assert plan_or_error(s, POLICY, mode) == plan_or_error(s, POLICY, mode)


@given(snapshots(), st.sampled_from(list(Mode)))
def test_every_verdict_explains_itself(s: Snapshot, mode: Mode) -> None:
    v = plan(s, POLICY, mode)
    assert v.headline.strip() and v.next_action.strip() and v.who_must_act.strip()


@given(snapshots())
def test_ready_means_really_ready(s: Snapshot) -> None:
    v = plan(s, POLICY, Mode.PR)
    if v.state in READY:
        assert not s.is_draft
        assert queue_labels_ok(s.labels)
        assert all(e.status in {JobStatus.PASSED, JobStatus.NOT_APPLICABLE} for e in v.jobs)
        assert v.blockers == ()


@given(snapshots(), st.data())
def test_a_new_failure_never_makes_a_pr_ready(s: Snapshot, data: st.DataObject) -> None:
    before = plan(s, POLICY, Mode.PR)
    if before.state not in READY or not s.check_runs:
        return
    i = data.draw(st.integers(0, len(s.check_runs) - 1))
    broken = list(s.check_runs)
    broken[i] = CheckRun(broken[i].name, "completed", "failure")
    after = plan(Snapshot(**{**s.__dict__, "check_runs": tuple(broken)}), POLICY, Mode.PR)
    assert after.state not in READY


@given(snapshots(), st.data())
def test_check_order_does_not_matter(s: Snapshot, data: st.DataObject) -> None:
    shuffled = tuple(data.draw(st.permutations(list(s.check_runs))))
    other: Any = Snapshot(**{**s.__dict__, "check_runs": shuffled})
    assert plan(s, POLICY, Mode.PR) == plan(other, POLICY, Mode.PR)


@given(snapshots(), st.lists(st.sampled_from(LABELS), unique=True))
def test_queue_mode_ignores_labels(s: Snapshot, labels: list[str]) -> None:
    other = Snapshot(**{**s.__dict__, "labels": frozenset(labels)})
    assert plan(s, POLICY, Mode.QUEUE) == plan(other, POLICY, Mode.QUEUE)
