"""The adapter reads the native-approval inputs: CODEOWNERS from the base branch, owner teams, change fingerprints."""

from __future__ import annotations

import base64

import pytest
from fakes import REPO, SHA, FakeGitHub, standard_fake

from osac_ci.github.api import GitHubError
from osac_ci.github.snapshot import fetch_change_fingerprint, fetch_snapshot
from osac_ci.policy import Approval

pytestmark = pytest.mark.contract
BASE = f"/repos/{REPO}"
OLD = "d" * 40


def b64(text: str) -> dict[str, str]:
    return {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}


def compare(patch: str, sha: str = "x") -> dict[str, object]:
    return {"files": [{"filename": "a.go", "status": "modified", "patch": patch, "sha": sha}]}


def with_approval_routes(fake: FakeGitHub, owners: str = "* @alice @example/data\n") -> FakeGitHub:
    fake.add("GET", f"{BASE}/contents/.github/CODEOWNERS", b64(owners))
    fake.add("GET", "/orgs/example/teams/data/members", [{"login": "erin"}])
    fake.add("GET", f"{BASE}/compare/main...{SHA}", compare("@@ -1 +1 @@\n-a\n+b"))
    return fake


def snapshot(fake: FakeGitHub, approval: Approval | None = None, *, enabled: bool = True):  # type: ignore[no-untyped-def]
    return fetch_snapshot(fake, REPO, 7, org="example", approval=(approval or Approval()) if enabled else None)


def test_nothing_extra_is_read_without_an_approval_policy() -> None:
    fake = standard_fake()
    s = snapshot(fake, enabled=False)
    assert s.codeowners is None and s.team_members == {} and s.change_fingerprints == {}
    assert not [c for c in fake.calls if "contents" in c[1] or "compare" in c[1] or "teams" in c[1]]


def test_reads_codeowners_teams_and_the_head_fingerprint() -> None:
    s = snapshot(with_approval_routes(standard_fake()))
    assert s.base_ref == "main"
    assert s.codeowners == "* @alice @example/data\n"
    assert s.team_members == {"example/data": frozenset({"erin"})}
    assert set(s.change_fingerprints) == {SHA}


def test_codeowners_comes_from_the_base_branch_and_the_documented_lookup_order() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/contents/CODEOWNERS", b64("* @root\n"))
    fake.add("GET", f"{BASE}/compare/main...{SHA}", compare("@@\n+x"))
    s = snapshot(fake)
    assert s.codeowners == "* @root\n"
    assert [c[1] for c in fake.calls if "contents" in c[1]] == [
        f"{BASE}/contents/.github/CODEOWNERS",
        f"{BASE}/contents/CODEOWNERS",
    ]


def test_no_codeowners_file_is_none_not_an_error() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/compare/main...{SHA}", compare("@@\n+x"))
    assert snapshot(fake).codeowners is None


def test_unreadable_codeowners_is_an_error() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/contents/.github/CODEOWNERS", {"message": "boom"}, status=500)
    with pytest.raises(GitHubError):
        snapshot(fake)


def test_a_team_the_credential_cannot_read_is_recorded_as_unreadable() -> None:
    fake = with_approval_routes(standard_fake())
    fake.add("GET", "/orgs/example/teams/data/members", {"message": "Not Found"}, status=404)
    assert snapshot(fake).team_members == {"example/data": None}


def test_only_teams_that_own_a_changed_file_are_looked_up() -> None:
    owned = with_approval_routes(standard_fake(), owners="* @alice\n/docs/ @example/data\n")
    assert snapshot(owned).team_members == {"example/data": frozenset({"erin"})}  # docs/b.md is changed
    unrelated = with_approval_routes(standard_fake(), owners="* @alice\n/other/ @example/data\n")
    assert snapshot(unrelated).team_members == {}
    assert "/orgs/example/teams/data/members" not in [c[1] for c in unrelated.calls]


def test_approved_commits_get_a_fingerprint_so_a_rebase_can_be_recognised() -> None:
    fake = with_approval_routes(standard_fake())
    fake.add(
        "GET",
        f"{BASE}/pulls/7/reviews",
        [{"id": 1, "state": "APPROVED", "user": {"login": "erin", "type": "User"}, "commit_id": OLD}],
    )
    fake.add("GET", f"{BASE}/compare/main...{OLD}", compare("@@ -9 +9 @@\n-a\n+b"))
    s = snapshot(fake)
    assert set(s.change_fingerprints) == {SHA, OLD}
    assert s.change_fingerprints[SHA] == s.change_fingerprints[OLD]  # same added and removed lines, different position


def test_a_commit_that_cannot_be_compared_has_no_fingerprint() -> None:
    fake = with_approval_routes(standard_fake())
    fake.add("GET", f"{BASE}/compare/main...{SHA}", {"message": "gone"}, status=404)
    assert snapshot(fake).change_fingerprints == {}


def test_a_comparison_at_the_file_cap_is_not_trusted() -> None:
    fake = standard_fake()
    fake.add(
        "GET", f"{BASE}/compare/main...{SHA}", {"files": [{"filename": f"f{i}", "patch": "@@\n+x"} for i in range(300)]}
    )
    assert fetch_change_fingerprint(fake, REPO, "main", SHA) is None


def test_binary_files_fall_back_to_the_blob_id() -> None:
    def body(blob: str) -> dict[str, object]:
        return {"files": [{"filename": "i.png", "status": "modified", "sha": blob}]}

    fake = standard_fake()
    fake.add("GET", f"{BASE}/compare/main...{SHA}", body("111"))
    fake.add("GET", f"{BASE}/compare/main...{OLD}", body("222"))
    assert fetch_change_fingerprint(fake, REPO, "main", SHA) != fetch_change_fingerprint(fake, REPO, "main", OLD)


def test_codeowners_that_is_not_text_is_an_error() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/contents/.github/CODEOWNERS", {"content": base64.b64encode(b"\xff\xfe").decode()})
    with pytest.raises(GitHubError, match="not readable text"):
        snapshot(fake)


def test_a_team_name_without_a_slug_is_unreadable_not_guessed() -> None:
    from osac_ci.github.snapshot import fetch_team_members

    assert fetch_team_members(standard_fake(), "justorg") is None


def test_a_pr_without_a_base_branch_cannot_be_checked() -> None:
    with pytest.raises(ValueError, match="no base branch"):
        snapshot(standard_fake(base={}))


def test_organization_lookups_use_the_org_client_and_everything_else_the_main_one() -> None:
    main = with_approval_routes(standard_fake(), owners="* @example/data\n")
    org = FakeGitHub()
    org.add("GET", "/orgs/example/members/alice", None, status=204)
    org.add("GET", "/orgs/example/teams/data/members", [{"login": "erin"}])
    s = fetch_snapshot(main, REPO, 7, org="example", approval=Approval(), org_client=org)
    assert s.author_is_org_member and s.team_members == {"example/data": frozenset({"erin"})}
    assert not [c for c in main.calls if c[1].startswith("/orgs/")]  # the PR token never asks about the org
    assert {c[1] for c in org.calls} == {"/orgs/example/members/alice", "/orgs/example/teams/data/members"}
    assert not [c for c in org.calls if c[1].startswith("/repos/")]  # the org token never touches the repository


# ---- dismissals ------------------------------------------------------------------------------------------------------


def review(id_: int, state: str, user: str = "bob") -> dict[str, object]:
    return {
        "id": id_,
        "state": state,
        "user": {"login": user, "type": "User"},
        "commit_id": SHA,
        "submitted_at": "2026-10-08T10:00:00Z",
    }


def dismissal(review_id: int, before: str, at: str = "2026-10-08T14:00:00Z") -> dict[str, object]:
    return {
        "event": "review_dismissed",
        "created_at": at,
        "dismissed_review": {"review_id": review_id, "state": before},
    }


def test_a_dismissed_review_gets_its_dismissal_time_and_prior_state_from_the_event() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/pulls/7/reviews", [review(7, "DISMISSED"), review(8, "APPROVED", "carol")])
    fake.add("GET", f"{BASE}/issues/7/events", [dismissal(7, "approved")])
    by_id = {r.id: r for r in fetch_snapshot(fake, REPO, 7, org="example").reviews}
    assert (by_id[7].state, by_id[7].dismissed_at, by_id[7].state_before_dismissal) == (
        "DISMISSED",
        "2026-10-08T14:00:00Z",
        "APPROVED",
    )
    assert (by_id[8].dismissed_at, by_id[8].state_before_dismissal) == ("", "")  # an ordinary review is untouched


def test_a_change_request_dismissal_keeps_its_prior_state() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/pulls/7/reviews", [review(7, "DISMISSED")])
    fake.add("GET", f"{BASE}/issues/7/events", [dismissal(7, "changes_requested")])
    (r,) = fetch_snapshot(fake, REPO, 7, org="example").reviews
    assert r.state_before_dismissal == "CHANGES_REQUESTED"


@pytest.mark.parametrize(
    "event",
    [
        {"event": "review_dismissed", "created_at": "t", "dismissed_review": {"state": "approved"}},  # no review id
        {
            "event": "review_dismissed",
            "created_at": "t",
            "dismissed_review": {"review_id": 99, "state": "approved"},
        },  # another review
        {"event": "review_dismissed", "created_at": "t", "dismissed_review": {"review_id": 7}},  # no prior state
        {"event": "review_dismissed", "created_at": "t"},
        {"event": "labeled", "created_at": "t", "label": {"name": "x"}},
    ],
)
def test_events_that_do_not_identify_the_review_change_nothing(event: dict[str, object]) -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/pulls/7/reviews", [review(7, "DISMISSED")])
    fake.add("GET", f"{BASE}/issues/7/events", [event])
    (r,) = fetch_snapshot(fake, REPO, 7, org="example").reviews
    assert (r.dismissed_at, r.state_before_dismissal) == ("", "")


def test_a_review_that_is_not_dismissed_ignores_a_stray_event() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/pulls/7/reviews", [review(7, "APPROVED")])
    fake.add("GET", f"{BASE}/issues/7/events", [dismissal(7, "approved")])
    (r,) = fetch_snapshot(fake, REPO, 7, org="example").reviews
    assert (r.state, r.dismissed_at) == ("APPROVED", "")


def test_protected_paths_read_the_approver_teams_only_when_their_files_changed() -> None:
    from osac_ci.policy import ProtectedPaths

    rule = ProtectedPaths(paths=("docs/**",), approvers=("@example/infra", "@carol"), carry_over="trivial-rebase")
    fake = standard_fake()
    fake.add("GET", "/orgs/example/teams/infra/members", [{"login": "dave"}])
    fake.add("GET", f"{BASE}/compare/main...{SHA}", compare("@@ -1 +1 @@\n-a\n+b"))
    s = fetch_snapshot(fake, REPO, 7, org="example", protected=(rule,))
    assert s.team_members == {"example/infra": frozenset({"dave"})}  # a login needs no team lookup
    assert set(s.change_fingerprints) == {SHA} and s.codeowners is None

    other = ProtectedPaths(paths=("tools/**",), approvers=("@example/infra",))
    quiet = standard_fake()
    s = fetch_snapshot(quiet, REPO, 7, org="example", protected=(other,))
    assert s.team_members == {} and s.change_fingerprints == {}
    assert not [c for c in quiet.calls if "teams" in c[1] or "compare" in c[1]]


def test_an_unreadable_approver_team_is_kept_as_unknown_not_empty() -> None:
    from osac_ci.policy import ProtectedPaths

    rule = ProtectedPaths(paths=("docs/**",), approvers=("@example/infra",))
    s = fetch_snapshot(standard_fake(), REPO, 7, org="example", protected=(rule,))  # no route: the team is unreadable
    assert s.team_members == {"example/infra": None}


def test_a_team_slug_that_is_a_path_segment_is_unreadable_not_looked_up() -> None:
    from osac_ci.github.snapshot import fetch_team_members

    fake = FakeGitHub()
    fake.add("GET", "/orgs/example/members", [{"login": "everyone"}])
    for slug in ("..", ".", "a/b", ""):
        assert fetch_team_members(fake, f"example/{slug}") is None
    assert fake.calls == []


def test_a_renamed_file_lists_both_names_so_moving_a_file_out_of_a_folder_is_seen() -> None:
    fake = standard_fake()
    fake.add(
        "GET",
        f"{BASE}/pulls/7/files",
        [
            {"filename": "tools/ci.yml", "previous_filename": ".github/workflows/ci.yml", "status": "renamed"},
            {"filename": "a.go"},
            {"filename": ".github/workflows/ci.yml", "status": "removed"},  # listed twice: kept once
        ],
    )
    s = fetch_snapshot(fake, REPO, 7, org="example")
    assert s.changed_files == ("tools/ci.yml", ".github/workflows/ci.yml", "a.go")


def test_a_queue_commit_lists_both_names_of_a_rename_too() -> None:
    from osac_ci.github.snapshot import fetch_queue_snapshot

    fake = FakeGitHub()
    sha = "e" * 40
    fake.add("GET", f"{BASE}/commits/{sha}/check-runs", {"check_runs": [], "total_count": 0})
    fake.add(
        "GET",
        f"{BASE}/compare/main...{sha}",
        {"files": [{"filename": "new/a.yml", "previous_filename": ".github/a.yml"}, {"filename": "b.go"}]},
    )
    s = fetch_queue_snapshot(fake, REPO, sha, "main")
    assert s.changed_files == ("new/a.yml", ".github/a.yml", "b.go") and s.changed_files_known


def test_no_team_is_read_when_only_excluded_files_changed() -> None:
    from osac_ci.policy import ProtectedPaths

    rule = ProtectedPaths(paths=("docs/**",), exclude_paths=("**/*.md",), approvers=("@example/infra",))
    fake = standard_fake()
    fake.add("GET", f"{BASE}/pulls/7/files", [{"filename": "docs/readme.md"}])
    s = fetch_snapshot(fake, REPO, 7, org="example", protected=(rule,))
    assert s.team_members == {} and not [c for c in fake.calls if "teams" in c[1]]
