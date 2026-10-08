"""Contract tests: the adapter turns GitHub's response shapes into a Snapshot, and fails loudly otherwise."""

from __future__ import annotations

import pytest
from fakes import REPO, SHA, FakeGitHub, check_runs, standard_fake

from osac_ci.github.api import GitHubError, check_repo, paginate
from osac_ci.github.snapshot import fetch_snapshot, is_org_member

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
