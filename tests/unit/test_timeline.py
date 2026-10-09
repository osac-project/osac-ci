"""Rebuilding a PR as it stood at an earlier moment."""

import pytest
from helpers import snap

from osac_ci.model import CheckRun, LabelEvent, Review
from osac_ci.timeline import state_at

pytestmark = pytest.mark.unit
T = "2026-10-05T12:00:00Z"


def label(event: str, name: str, at: str, actor: str = "bot") -> LabelEvent:
    return LabelEvent(event, name, actor, at)


def test_labels_are_replayed_up_to_the_moment(toy_policy) -> None:  # type: ignore[no-untyped-def]
    events = (
        label("labeled", "lgtm", "2026-10-05T10:00:00Z"),
        label("labeled", "approved", "2026-10-05T11:00:00Z"),
        label("unlabeled", "lgtm", "2026-10-05T11:30:00Z"),
        label("labeled", "lgtm", "2026-10-05T13:00:00Z"),  # after the moment: not yet
    )
    got = state_at(snap(toy_policy, labels=frozenset({"lgtm", "approved"}), label_events=events), T)
    assert got.labels == frozenset({"approved"}) and len(got.label_events) == 3


def test_a_label_applied_after_the_moment_is_not_there_yet(toy_policy) -> None:  # type: ignore[no-untyped-def]
    events = (label("labeled", "approved", "2026-10-05T12:00:01Z"),)
    assert state_at(snap(toy_policy, labels=frozenset({"approved"}), label_events=events), T).labels == frozenset()


def test_a_label_applied_exactly_at_the_moment_counts(toy_policy) -> None:  # type: ignore[no-untyped-def]
    events = (label("labeled", "approved", T),)
    assert state_at(snap(toy_policy, labels=frozenset(), label_events=events), T).labels == frozenset({"approved"})


def test_events_are_ordered_by_time_not_by_the_order_given(toy_policy) -> None:  # type: ignore[no-untyped-def]
    events = (label("unlabeled", "x", "2026-10-05T11:00:00Z"), label("labeled", "x", "2026-10-05T10:00:00Z"))
    assert state_at(snap(toy_policy, label_events=events), T).labels == frozenset()


def test_reviews_after_the_moment_are_dropped_and_undated_ones_kept(toy_policy) -> None:  # type: ignore[no-untyped-def]
    reviews = (
        Review("a", "APPROVED", "User", "2026-10-05T11:00:00Z", 1, "c1"),
        Review("b", "APPROVED", "User", "2026-10-05T13:00:00Z", 2, "c1"),
        Review("c", "APPROVED", "User", None, 3, "c1"),
    )
    assert [r.user for r in state_at(snap(toy_policy, reviews=reviews), T).reviews] == ["a", "c"]


def test_a_dismissal_after_the_moment_leaves_the_approval_standing(toy_policy) -> None:  # type: ignore[no-untyped-def]
    reviews = (
        Review("a", "APPROVED", "User", "2026-10-05T10:00:00Z", 1, "c1"),
        Review("a", "DISMISSED", "User", "2026-10-05T14:00:00Z", 2, "c1"),
    )
    assert [r.state for r in state_at(snap(toy_policy, reviews=reviews), T).reviews] == ["APPROVED"]


def run(name: str, started: str | None, completed: str | None, conclusion: str | None = "success") -> CheckRun:
    return CheckRun(
        name,
        "completed" if completed else "in_progress",
        conclusion if completed else None,
        started,
        completed_at=completed,
    )


def test_a_check_that_had_not_started_did_not_exist(toy_policy) -> None:  # type: ignore[no-untyped-def]
    got = state_at(snap(toy_policy, check_runs=(run("lint", "2026-10-05T12:30:00Z", "2026-10-05T12:40:00Z"),)), T)
    assert got.check_runs == ()


def test_a_check_still_running_at_the_moment_is_in_progress(toy_policy) -> None:  # type: ignore[no-untyped-def]
    final = run("lint", "2026-10-05T11:00:00Z", "2026-10-05T12:30:00Z", "failure")
    (got,) = state_at(snap(toy_policy, check_runs=(final,)), T).check_runs
    assert (got.status, got.conclusion, got.completed_at) == ("in_progress", None, None)


def test_a_check_already_finished_keeps_its_conclusion(toy_policy) -> None:  # type: ignore[no-untyped-def]
    done = run("lint", "2026-10-05T10:00:00Z", "2026-10-05T11:00:00Z", "failure")
    (got,) = state_at(snap(toy_policy, check_runs=(done,)), T).check_runs
    assert (got.status, got.conclusion) == ("completed", "failure")


def test_a_rerun_after_the_moment_does_not_hide_the_result_at_the_moment(toy_policy) -> None:  # type: ignore[no-untyped-def]
    first = run("lint", "2026-10-05T10:00:00Z", "2026-10-05T10:30:00Z", "failure")
    rerun = run("lint", "2026-10-05T13:00:00Z", "2026-10-05T13:10:00Z", "success")
    (got,) = state_at(snap(toy_policy, check_runs=(first, rerun)), T).check_runs
    assert got.conclusion == "failure"


def test_checks_without_timestamps_are_kept(toy_policy) -> None:  # type: ignore[no-untyped-def]
    bare = CheckRun("lint", "completed", "success")
    assert state_at(snap(toy_policy, check_runs=(bare,)), T).check_runs == (bare,)


def test_the_pr_is_not_in_the_queue_at_the_moment_it_is_judged(toy_policy) -> None:  # type: ignore[no-untyped-def]
    assert not state_at(snap(toy_policy, in_merge_queue=True), T).in_merge_queue


def test_everything_else_is_left_alone(toy_policy) -> None:  # type: ignore[no-untyped-def]
    s = snap(toy_policy, changed_files=("a.go", "b.go"), author="alice", is_fork=True)
    got = state_at(s, T)
    assert (got.changed_files, got.author, got.is_fork, got.head_sha) == (s.changed_files, "alice", True, s.head_sha)


# ---- sha-bound authorization is a check run: it exists only from when that run completed -------------------------------


def auth_run(login: str, sha: str, completed: str | None, started: str = "2026-10-05T11:00:00Z") -> CheckRun:
    from osac_ci.github.snapshot import authorization_external_id

    return CheckRun(
        "OSAC CI authorization",
        "completed" if completed else "in_progress",
        "success" if completed else None,
        started,
        authorization_external_id(login, sha),
        "github-actions",
        completed,
    )


def authorized(toy_policy, completed: str | None, started: str = "2026-10-05T11:00:00Z", **kw):  # type: ignore[no-untyped-def]
    sha = "a" * 40
    run = auth_run("reviewer", sha, completed, started)
    return snap(toy_policy, head_sha=sha, authorized_by="reviewer", check_runs=(run,), is_fork=True, **kw)


def test_an_authorization_that_completed_after_the_moment_does_not_exist_yet(toy_policy) -> None:  # type: ignore[no-untyped-def]
    got = state_at(authorized(toy_policy, completed="2026-10-05T12:30:00Z"), T)
    assert got.authorized_by == "" and got.check_runs[0].status == "in_progress"


def test_an_authorization_that_had_not_even_started_is_gone(toy_policy) -> None:  # type: ignore[no-untyped-def]
    assert (
        state_at(
            authorized(toy_policy, completed="2026-10-05T12:40:00Z", started="2026-10-05T12:30:00Z"), T
        ).authorized_by
        == ""
    )


def test_an_authorization_completed_before_the_moment_stands(toy_policy) -> None:  # type: ignore[no-untyped-def]
    assert state_at(authorized(toy_policy, completed="2026-10-05T11:30:00Z"), T).authorized_by == "reviewer"


def test_an_authorization_without_a_timestamp_cannot_be_placed_so_it_stands(toy_policy) -> None:  # type: ignore[no-untyped-def]
    sha = "a" * 40
    bare = CheckRun(
        "OSAC CI authorization", "completed", "success", None, f"osac-ci-auth:v1:reviewer:{sha}", "github-actions"
    )
    assert (
        state_at(snap(toy_policy, head_sha=sha, authorized_by="reviewer", check_runs=(bare,)), T).authorized_by
        == "reviewer"
    )


def test_no_authorizer_stays_no_authorizer(toy_policy) -> None:  # type: ignore[no-untyped-def]
    assert state_at(snap(toy_policy, authorized_by="", check_runs=()), T).authorized_by == ""


def test_another_persons_authorization_run_does_not_keep_this_one(toy_policy) -> None:  # type: ignore[no-untyped-def]
    sha = "a" * 40
    other = auth_run("someone-else", sha, "2026-10-05T11:30:00Z")
    got = state_at(snap(toy_policy, head_sha=sha, authorized_by="reviewer", check_runs=(other,)), T)
    assert got.authorized_by == ""


def test_a_fork_is_not_trusted_at_the_moment_before_its_authorization_completed(toy_policy) -> None:  # type: ignore[no-untyped-def]
    from osac_ci.policy import Trust
    from osac_ci.rules.fork import fork_secrets_authorized

    final = authorized(toy_policy, completed="2026-10-05T12:30:00Z", author="outsider", fork_owner="outsider")
    sha_bound = Trust(authorization="sha-bound")
    assert fork_secrets_authorized(final, sha_bound)  # on today's data it looks authorized
    assert not fork_secrets_authorized(state_at(final, T), sha_bound)  # but it was not, when it counted
