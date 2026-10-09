"""Properties of the protected-path rule and of the skipped-check rule, for any reviews and any check outcomes."""

from __future__ import annotations

import pytest
from helpers import HEAD, OTHER, ROOT, snap
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from osac_ci.model import CheckRun, JobStatus, Review, State
from osac_ci.planner import plan
from osac_ci.policy import ProtectedPaths, load_policy

pytestmark = pytest.mark.property
settings.register_profile("rules", max_examples=150, deadline=None, suppress_health_check=list(HealthCheck))
settings.load_profile("rules")

BASE = load_policy(ROOT / "policy" / "osac.yml")
TEAM = "@osac-project/wg-infra"
MEMBERS = {"osac-project/wg-infra": frozenset({"infra-bob", "infra-eve"})}
GUARDED = BASE.model_copy(update={"protected_paths": (ProtectedPaths(paths=(".github/**",), approvers=(TEAM,)),)})
READY = {State.READY_TO_ENQUEUE, State.IN_QUEUE}
PROTECTED_FILES = [".github/workflows/a.yml", ".github/filters/f.yml"]
OTHER_FILES = ["fulfillment-service/a.go", "docs/readme.md", "osac-ui/src/a.ts"]


@st.composite
def review_sets(draw: st.DrawFn) -> tuple[Review, ...]:
    users = ["infra-bob", "infra-eve", "somebody", "alice"]  # alice is the author
    return tuple(
        Review(
            draw(st.sampled_from(users)),
            draw(st.sampled_from(["APPROVED", "CHANGES_REQUESTED", "DISMISSED", "COMMENTED"])),
            "Bot" if draw(st.booleans()) and draw(st.booleans()) else "User",
            f"2026-10-01T10:{i:02d}:00Z",
            i + 1,
            draw(st.sampled_from([HEAD, OTHER])),
        )
        for i in range(draw(st.integers(0, 5)))
    )


def judged(policy, files: list[str], reviews: tuple[Review, ...]):  # type: ignore[no-untyped-def]
    return plan(snap(policy, author="alice", changed_files=tuple(files), team_members=MEMBERS, reviews=reviews), policy)


def valid_approval(reviews: tuple[Review, ...]) -> bool:
    """An independent restatement of the rule: the latest review of a team member is an approval of this commit."""
    latest: dict[str, Review] = {}
    for r in sorted(reviews, key=lambda r: (r.submitted_at or "", r.id or 0)):
        if r.user_type != "User" or r.user == "alice" or r.state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        if r.state == "DISMISSED":
            latest.pop(r.user, None)
        else:
            latest[r.user] = r
    return any(r.state == "APPROVED" and r.commit_id == HEAD for u, r in latest.items() if u in MEMBERS[TEAM[1:]])


@given(review_sets(), st.lists(st.sampled_from(PROTECTED_FILES + OTHER_FILES), min_size=1, unique=True))
def test_a_protected_change_is_only_ever_ready_with_an_approval_from_the_named_people(
    reviews: tuple[Review, ...], files: list[str]
) -> None:
    v = judged(GUARDED, files, reviews)
    touches = any(f in PROTECTED_FILES for f in files)
    if v.state in READY and touches:
        assert valid_approval(reviews)
    if touches and valid_approval(reviews):
        assert not any("protected files" in b for b in v.blockers)
    if touches and not valid_approval(reviews):
        assert any("protected files" in b for b in v.blockers)


@given(review_sets(), st.lists(st.sampled_from(OTHER_FILES), min_size=1, unique=True))
def test_the_rule_changes_nothing_for_a_pr_that_touches_no_protected_file(
    reviews: tuple[Review, ...], files: list[str]
) -> None:
    assert judged(GUARDED, files, reviews) == judged(BASE, files, reviews)


@given(review_sets(), st.lists(st.sampled_from(PROTECTED_FILES + OTHER_FILES), min_size=1, unique=True))
def test_an_approval_from_a_named_person_never_adds_a_blocker(reviews: tuple[Review, ...], files: list[str]) -> None:
    before = judged(GUARDED, files, reviews)
    approval = Review("infra-eve", "APPROVED", "User", "2026-10-02T10:00:00Z", 99, HEAD)
    after = judged(GUARDED, files, (*reviews, approval))
    assert not [
        b for b in after.blockers if "protected files" in b and "protected files" not in " ".join(before.blockers)
    ]
    if before.state in READY:
        assert after.state in READY


# ---- skipped_applicable --------------------------------------------------------------------------------------------

STRICT = BASE.model_copy(
    update={"path_filters": BASE.path_filters.model_copy(update={"mode": "enforce", "skipped_applicable": "fail"})}
)
FILE_SETS = [
    ("fulfillment-service/a.go",),
    ("osac-ui/src/a.ts",),
    ("docs/readme.md",),
    ("osac-aap/roles/x/tasks/main.yml",),
]


@given(st.sampled_from(FILE_SETS), st.lists(st.sampled_from([j.check for j in BASE.jobs.values()]), unique=True))
def test_a_pr_is_never_ready_while_an_applicable_job_was_skipped(files: tuple[str, ...], skipped: list[str]) -> None:
    runs = tuple(
        CheckRun(j.check, "completed", "skipped" if j.check in skipped else "success") for j in STRICT.jobs.values()
    )
    v = plan(snap(STRICT, changed_files=files, check_runs=runs), STRICT)
    gates = {j.check for j in STRICT.jobs.values() if j.needs_readiness}
    offenders = [
        e for e in v.jobs if e.check in skipped and e.check not in gates and e.status is not JobStatus.NOT_APPLICABLE
    ]
    if offenders:
        assert v.state not in READY
        assert all(e.status is JobStatus.FAILED for e in offenders)


@given(st.sampled_from(FILE_SETS), st.lists(st.sampled_from([j.check for j in BASE.jobs.values()]), unique=True))
def test_the_default_still_counts_a_skip_as_a_pass(files: tuple[str, ...], skipped: list[str]) -> None:
    lenient = STRICT.model_copy(
        update={"path_filters": STRICT.path_filters.model_copy(update={"skipped_applicable": "pass"})}
    )
    runs = tuple(
        CheckRun(j.check, "completed", "skipped" if j.check in skipped else "success") for j in lenient.jobs.values()
    )
    v = plan(snap(lenient, changed_files=files, check_runs=runs), lenient)
    assert not [e for e in v.jobs if e.status is JobStatus.FAILED]
