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
