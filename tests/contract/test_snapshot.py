"""Contract tests: the adapter turns GitHub's response shapes into a Snapshot, and fails loudly otherwise."""

from __future__ import annotations

import pytest
from fakes import REPO, SHA, FakeGitHub, check_runs, standard_fake

from osac_ci.github.api import GitHubError, check_repo, paginate
from osac_ci.github.snapshot import fetch_snapshot, is_org_member, queued_at_end

pytestmark = pytest.mark.contract
BASE = f"/repos/{REPO}"


def snapshot(fake: FakeGitHub, **kwargs: object):  # type: ignore[no-untyped-def]
    return fetch_snapshot(fake, REPO, 7, org="example", **kwargs)  # type: ignore[arg-type]


def test_maps_a_fork_pr() -> None:
    s = snapshot(standard_fake())
    assert (s.repo, s.number, s.head_sha) == (REPO, 7, SHA)
    assert s.is_fork and s.fork_owner == "alice" and s.author == "alice"
    assert s.labels == frozenset({"lgtm", "approved"})
    assert s.changed_files == ("a.go", "docs/b.md")
    assert not s.is_draft and not s.in_merge_queue
    assert not s.author_is_org_member and not s.fork_owner_is_org_member


def test_same_repo_pr_is_not_a_fork() -> None:
    fake = standard_fake(head={"sha": SHA, "repo": {"full_name": REPO, "owner": {"login": "example"}}})
    s = snapshot(fake)
    assert not s.is_fork and s.fork_owner == ""


def test_deleted_fork_counts_as_a_fork() -> None:
    s = snapshot(standard_fake(head={"sha": SHA, "repo": None}))
    assert s.is_fork and s.fork_owner == ""


def test_draft_flag() -> None:
    assert snapshot(standard_fake(draft=True)).is_draft


def test_more_than_one_page_of_check_runs_is_read_completely() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/commits/{SHA}/check-runs", check_runs(110))
    s = snapshot(fake)
    assert len(s.check_runs) == 110 and s.check_runs[-1].name == "check-109"
    pages = [c for c in fake.calls if c[1].endswith("/check-runs")]
    assert len(pages) == 2  # 100 + 10


def test_exact_page_boundary_does_not_loop_forever() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/commits/{SHA}/check-runs", check_runs(100))
    assert len(snapshot(fake).check_runs) == 100


def test_request_budget_for_a_110_check_pr_stays_small() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/commits/{SHA}/check-runs", check_runs(110))
    snapshot(fake)
    assert len(fake.calls) <= 10, fake.calls


def test_reviews_without_a_user_are_dropped_and_bot_type_kept() -> None:
    fake = standard_fake()
    fake.add(
        "GET",
        f"{BASE}/pulls/7/reviews",
        [
            {"id": 1, "user": None, "state": "APPROVED"},
            {
                "id": 2,
                "user": {"login": "coderabbitai[bot]", "type": "Bot"},
                "state": "APPROVED",
                "submitted_at": "2026-01-01T00:00:01Z",
                "commit_id": SHA,
            },
        ],
    )
    reviews = snapshot(fake).reviews
    assert len(reviews) == 1 and reviews[0].user == "coderabbitai[bot]" and reviews[0].user_type == "Bot"
    assert reviews[0].commit_id == SHA


def test_events_keep_only_label_events_and_tolerate_a_missing_actor() -> None:
    fake = standard_fake()
    fake.add(
        "GET",
        f"{BASE}/issues/7/events",
        [
            {"event": "labeled", "label": {"name": "lgtm"}, "actor": {"login": "github-actions[bot]"}},
            {"event": "labeled", "label": {"name": "e2e-ready"}, "actor": None},
            {"event": "closed", "actor": {"login": "alice"}},
        ],
    )
    events = snapshot(fake).label_events
    assert [(e.label, e.actor) for e in events] == [("lgtm", "github-actions[bot]"), ("e2e-ready", "")]


def test_merge_queue_entry_is_detected() -> None:
    fake = standard_fake()
    fake.add("POST", "/graphql", {"data": {"node": {"mergeQueueEntry": {"id": "MQE_1"}}}})
    assert snapshot(fake).in_merge_queue


def test_graphql_errors_fail_instead_of_guessing() -> None:
    fake = standard_fake()
    fake.add("POST", "/graphql", {"errors": [{"message": "boom"}]})
    with pytest.raises(GitHubError):
        snapshot(fake)


@pytest.mark.parametrize(("status", "expected"), [(204, True), (404, False)])
def test_org_membership(status: int, expected: bool) -> None:
    fake = FakeGitHub()
    fake.add("GET", "/orgs/example/members/alice", None, status=status)
    assert is_org_member(fake, "example", "alice") is expected


def test_org_membership_unexpected_status_is_an_error_not_a_no() -> None:
    fake = FakeGitHub()
    fake.add("GET", "/orgs/example/members/alice", {"message": "rate limited"}, status=403)
    with pytest.raises(GitHubError) as err:
        is_org_member(fake, "example", "alice")
    assert err.value.status == 403


def test_bot_logins_are_url_encoded_in_the_membership_path() -> None:
    fake = FakeGitHub()
    fake.add("GET", "/orgs/example/members/some-bot%5Bbot%5D", None, status=404)
    assert is_org_member(fake, "example", "some-bot[bot]") is False


def test_membership_of_author_and_distinct_fork_owner_is_looked_up() -> None:
    fake = standard_fake(
        user={"login": "app-bot[bot]"},
        head={"sha": SHA, "repo": {"full_name": "dev-bot/app", "owner": {"login": "dev-bot"}}},
    )
    fake.add("GET", "/orgs/example/members/app-bot%5Bbot%5D", None, status=404)
    fake.add("GET", "/orgs/example/members/dev-bot", None, status=204)
    s = snapshot(fake)
    assert not s.author_is_org_member and s.fork_owner_is_org_member and s.fork_owner == "dev-bot"


def test_without_membership_lookup_the_association_is_used() -> None:
    s = snapshot(standard_fake(author_association="MEMBER"), lookup_membership=False)
    assert s.author_is_org_member and not s.fork_owner_is_org_member
    assert (
        snapshot(standard_fake(author_association="CONTRIBUTOR"), lookup_membership=False).author_is_org_member is False
    )


def test_http_failure_surfaces_with_its_status() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/pulls/7/files", {"message": "Bad Gateway"}, status=502)
    with pytest.raises(GitHubError) as err:
        snapshot(fake)
    assert err.value.status == 502


def test_runaway_pagination_is_capped() -> None:
    fake = FakeGitHub()
    fake.add("GET", "/endless", [{"x": 1}] * 10_000)  # every page is full, so the loop never ends by itself
    with pytest.raises(GitHubError, match="too many pages"):
        list(paginate(fake, "/endless", per_page=1))


@pytest.mark.parametrize("bad", ["", "no-slash", "a/b/c", "a/b c", "a/../b", "https://evil/x", "a/b?x=1"])
def test_bad_repo_names_are_rejected_before_any_request(bad: str) -> None:
    fake = FakeGitHub()
    with pytest.raises(ValueError, match="invalid repo"):
        fetch_snapshot(fake, bad, 7, org="example")
    assert fake.calls == []


def test_bad_pr_number_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid PR number"):
        fetch_snapshot(FakeGitHub(), REPO, 0, org="example")


def test_check_repo_accepts_normal_names() -> None:
    assert check_repo("osac-project/osac-ci") == "osac-project/osac-ci"


def ev(kind: str, at: str = "2026-10-07T10:00:00Z", actor: str = "someone") -> dict[str, object]:
    return {"event": kind, "created_at": at, "actor": {"login": actor}}


BOT = "github-merge-queue[bot]"
T = "2026-10-07T10:00:00Z"
T_PLUS_1S = "2026-10-07T10:00:01Z"
T_MINUS_38S = "2026-10-07T09:59:22Z"


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        ([], False),
        ([ev("labeled"), ev("merged")], False),
        ([ev("added_to_merge_queue")], True),
        ([ev("added_to_merge_queue"), ev("removed_from_merge_queue")], False),
        ([ev("added_to_merge_queue"), ev("head_ref_force_pushed")], False),
        ([ev("added_to_merge_queue"), ev("removed_from_merge_queue"), ev("added_to_merge_queue")], True),
        ([ev("auto_merge_enabled"), ev("added_to_merge_queue"), ev("merged")], True),
        # PR 1472: the queue merges, then the queue bot removes the PR in the same second
        ([ev("added_to_merge_queue"), ev("merged", T), ev("removed_from_merge_queue", T, BOT)], True),
        # the API can list that same-second cleanup BEFORE the merge: it is still a queue merge
        ([ev("added_to_merge_queue"), ev("removed_from_merge_queue", T, BOT), ev("merged", T)], True),
        ([ev("added_to_merge_queue"), ev("removed_from_merge_queue", T_PLUS_1S, BOT), ev("merged", T)], True),
        # PR 1481: a human dequeues the PR and merges it by hand 38 seconds later
        (
            [
                ev("added_to_merge_queue"),
                ev("removed_from_merge_queue", T_MINUS_38S, "alice"),
                ev("merged", T, "alice"),
            ],
            False,
        ),
        # a human dequeuing in the same second as the merge is still a human dequeue
        ([ev("added_to_merge_queue"), ev("removed_from_merge_queue", T, "alice"), ev("merged", T, "alice")], False),
        # the queue bot ejecting the PR long before a later manual merge (PR 1449 shape)
        (
            [
                ev("added_to_merge_queue", "2026-10-06T14:12:00Z"),
                ev("removed_from_merge_queue", "2026-10-06T14:21:00Z", BOT),
                ev("added_to_merge_queue", "2026-10-06T14:22:00Z"),
                ev("head_ref_force_pushed", "2026-10-06T15:15:00Z"),
                ev("removed_from_merge_queue", "2026-10-06T15:15:30Z", "osac-ci-bot"),
                ev("merged", "2026-10-07T08:02:00Z", "bob"),
            ],
            False,
        ),
    ],
)  # fmt: skip
def test_queued_at_end_replays_the_event_sequence(events: list[dict[str, object]], expected: bool) -> None:
    assert queued_at_end(events) is expected


def test_events_without_timestamps_never_count_as_cleanup() -> None:
    bare = [{"event": "added_to_merge_queue"}, {"event": "removed_from_merge_queue"}, {"event": "merged"}]
    assert queued_at_end(bare) is False


def test_snapshot_marks_a_pr_whose_last_queue_event_is_an_add() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/issues/7/events", [ev("added_to_merge_queue"), ev("merged")])
    assert snapshot(fake).queued_per_events is True
    assert snapshot(standard_fake()).queued_per_events is False


def test_the_id_of_the_app_that_posted_a_check_is_kept_and_odd_values_become_zero() -> None:
    fake = standard_fake()
    runs = [
        {
            "name": "lint",
            "status": "completed",
            "conclusion": "success",
            "app": {"id": 15368, "slug": "github-actions"},
        },
        {"name": "test", "status": "completed", "conclusion": "success", "app": {"id": "15368"}},
        {"name": "odd", "status": "completed", "conclusion": "success", "app": {"id": False}},
        {"name": "bare", "status": "completed", "conclusion": "success"},
    ]
    fake.add("GET", f"{BASE}/commits/{SHA}/check-runs", {"total_count": 4, "check_runs": runs})
    assert [c.app_id for c in snapshot(fake).check_runs] == [15368, 0, 0, 0]
