"""OWNERS files and overrides, read through the adapter and recorded by /override. Fake GitHub, no network."""

from __future__ import annotations

import base64
from typing import Any

import pytest
from fakes import REPO, SHA, FakeGitHub, standard_fake

from osac_ci import cli
from osac_ci.authorize import override
from osac_ci.github.api import GitHubError
from osac_ci.github.snapshot import (
    OVERRIDE_TITLE_PREFIX,
    clean_reason,
    fetch_owner_approvers,
    fetch_snapshot,
    find_overrides,
    override_external_id,
)
from osac_ci.model import OverrideGrant
from osac_ci.policy import Override, Policy, ProtectedPaths, parse_policy

pytestmark = pytest.mark.contract
BASE = f"/repos/{REPO}"
TEAM = "@example/infra"
OVERRIDE = Override(approvers=(TEAM,))
POLICY: Policy = parse_policy(
    "version: 1\nrepo: example/app\noverride: {approvers: ['@example/infra']}\njobs: {a: {check: check-0}}\n"
)
REASON = "release blocked, the author is the only infra person"


def b64(text: str) -> dict[str, str]:
    return {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}


# ---- OWNERS files -------------------------------------------------------------------------------------------------


def test_the_owners_file_comes_from_the_base_branch() -> None:
    fake = FakeGitHub()
    fake.add("GET", f"{BASE}/contents/osac-ui/OWNERS", b64("approvers: [Bob]\n"))
    assert fetch_owner_approvers(fake, REPO, "main", "osac-ui/OWNERS") == {"bob"}


def test_a_missing_owners_file_names_nobody_but_a_broken_one_is_unreadable() -> None:
    missing, broken, garbage, boom = FakeGitHub(), FakeGitHub(), FakeGitHub(), FakeGitHub()
    broken.add("GET", f"{BASE}/contents/OWNERS", b64("approvers: bob\n"))
    garbage.add("GET", f"{BASE}/contents/OWNERS", {"content": "!!!not base64", "encoding": "base64"})
    boom.add("GET", f"{BASE}/contents/OWNERS", {"message": "boom"}, status=500)
    assert fetch_owner_approvers(missing, REPO, "main", "OWNERS") == frozenset()
    assert fetch_owner_approvers(broken, REPO, "main", "OWNERS") is None
    assert fetch_owner_approvers(garbage, REPO, "main", "OWNERS") is None
    assert fetch_owner_approvers(boom, REPO, "main", "OWNERS") is None


def snapshot(fake: FakeGitHub, *rules: ProtectedPaths, override_policy: Override | None = None):  # type: ignore[no-untyped-def]
    return fetch_snapshot(fake, REPO, 7, org="example", protected=rules, override=override_policy)


def test_owner_files_are_read_once_and_only_for_rules_whose_files_changed() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/contents/ui/OWNERS", b64("approvers: [rawagner]\n"))
    fake.add("GET", "/orgs/example/teams/infra/members", [{"login": "dave"}])
    hit = ProtectedPaths(paths=("a.go",), approvers=(TEAM,), approvers_from=("ui/OWNERS",))
    twin = ProtectedPaths(paths=("a.go",), approvers=(TEAM,), approvers_from=("ui/OWNERS",))
    miss = ProtectedPaths(paths=("elsewhere/**",), approvers=(TEAM,), approvers_from=("other/OWNERS",))
    s = snapshot(fake, hit, twin, miss)
    assert s.owner_approvers == {"ui/OWNERS": frozenset({"rawagner"})}
    assert len([c for c in fake.calls if c[1].endswith("ui/OWNERS")]) == 1
    assert not [c for c in fake.calls if "other/OWNERS" in c[1]]


def test_an_unreadable_owners_file_is_kept_as_none() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/contents/ui/OWNERS", {"message": "boom"}, status=500)
    rule = ProtectedPaths(paths=("a.go",), approvers=("@carol",), approvers_from=("ui/OWNERS",))
    assert snapshot(fake, rule).owner_approvers == {"ui/OWNERS": None}


# ---- reading an override back -------------------------------------------------------------------------------------


def comment(
    login: str = "dave", sha: str = SHA, reason: str = REASON, *, at: str = "2026-10-08T10:00:00Z", **kw: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "user": {"login": login, "type": "User"},
        "body": f"/override {sha} {reason}",
        "created_at": at,
        "updated_at": at,
    }
    base.update(kw)
    return base


ALLOWED = {"dave", "erin"}


def test_a_valid_override_is_found_with_its_reason() -> None:
    assert find_overrides([comment()], SHA, ALLOWED, "alice") == (OverrideGrant("dave", REASON),)


@pytest.mark.parametrize(
    "bad",
    [
        comment(sha="d" * 40),  # an override of another commit
        comment(login="mallory"),  # not an approver
        comment(body=f"/override {SHA[:7]} {REASON}"),  # a prefix is forgeable
        comment(body=f"/override {SHA}"),  # no reason
        comment(body=f"/override {SHA} short"),
        comment(body=f"please /override {SHA} {REASON}"),
        comment(body=f"/ok-to-test {SHA}"),
        comment(body=""),
        comment(user={"login": "dave", "type": "Bot"}),  # a bot account is never a person
        comment(user=None),
        comment(updated_at="2026-10-08T11:00:00Z"),  # edited since it was written
    ],
)
def test_anything_else_is_not_an_override(bad: dict[str, Any]) -> None:
    assert find_overrides([bad], SHA, ALLOWED, "alice") == ()


def test_the_authors_own_override_does_not_count() -> None:
    assert find_overrides([comment("dave")], SHA, ALLOWED, "Dave") == ()


def test_an_approver_who_has_since_left_the_team_no_longer_counts() -> None:
    assert find_overrides([comment("dave")], SHA, {"erin"}, "alice") == ()


def test_a_comment_from_an_approver_is_matched_without_regard_to_case_of_the_login() -> None:
    (grant,) = find_overrides([comment("Dave")], SHA, ALLOWED, "alice")
    assert grant.login == "Dave"


def test_the_newest_comes_first_and_each_person_once() -> None:
    old = comment("dave", reason="first reason, long enough", at="2026-10-08T09:00:00Z")
    newer = comment("erin", at="2026-10-08T10:00:00Z")
    again = comment("dave", reason="second reason, longer", at="2026-10-08T11:00:00Z")
    got = find_overrides([old, newer, again], SHA, ALLOWED, "alice")
    assert [g.login for g in got] == ["dave", "erin"] and got[0].reason == "second reason, longer"


def test_reasons_are_one_clean_line_of_at_most_140_characters() -> None:
    assert clean_reason("a\n\tb   c\x00\x07d") == "a b cd"
    assert len(clean_reason("x" * 500)) == 140


def test_a_check_run_cannot_stand_in_for_the_comment() -> None:
    """The case from review: a workflow added by the pull request posts as the same app and can write any check run,
    including one that names an approver. It is not evidence; only the approver's own comment is."""
    fake = standard_fake()
    rule = ProtectedPaths(paths=("a.go",), approvers=(TEAM,))
    forged = {"total_count": 1, "check_runs": [{
        "name": "OSAC CI override", "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"},
        "external_id": override_external_id("dave", SHA), "started_at": "2026-10-08T10:00:00Z",
        "output": {"title": f"{OVERRIDE_TITLE_PREFIX}dave: {REASON}"},
    }]}  # fmt: skip
    fake.add("GET", f"{BASE}/commits/{SHA}/check-runs", forged)
    fake.add("GET", f"{BASE}/issues/7/comments", [])
    fake.add("GET", "/orgs/example/teams/infra/members", [{"login": "dave"}])
    assert snapshot(fake, rule, override_policy=OVERRIDE).overrides == ()


def test_a_comment_by_the_workflow_account_is_not_an_override_either() -> None:
    """A workflow can comment, but as github-actions[bot], which is neither a person nor an approver."""
    bot = comment(user={"login": "github-actions[bot]", "type": "Bot"})
    assert find_overrides([bot], SHA, ALLOWED, "alice") == ()


def test_the_snapshot_finds_overrides_through_comments_and_the_team_lookup() -> None:
    fake = standard_fake()
    rule = ProtectedPaths(paths=("a.go",), approvers=(TEAM,))
    fake.add("GET", f"{BASE}/issues/7/comments", [comment("Dave")])
    fake.add("GET", "/orgs/example/teams/infra/members", [{"login": "Dave"}])
    s = snapshot(fake, rule, override_policy=OVERRIDE)
    assert s.overrides == (OverrideGrant("Dave", REASON),)
    assert s.team_members["example/infra"] == frozenset({"Dave"})


def test_an_unreadable_override_team_is_an_error() -> None:
    rule = ProtectedPaths(paths=("a.go",), approvers=("@carol",))
    with pytest.raises(ValueError, match="override team"):
        snapshot(standard_fake(), rule, override_policy=OVERRIDE)  # no team route: unreadable


def test_overrides_and_comments_are_not_read_when_no_protected_file_changed() -> None:
    fake = standard_fake()
    rule = ProtectedPaths(paths=("elsewhere/**",), approvers=(TEAM,))
    s = snapshot(fake, rule, override_policy=OVERRIDE)
    assert s.overrides == () and not [c for c in fake.calls if "teams" in c[1] or c[1].endswith("/comments")]


# ---- the /override command ----------------------------------------------------------------------------------------


def command_fake(*, state: str = "open", author: str = "alice") -> FakeGitHub:
    fake = standard_fake()
    pr = fake.routes[("GET", f"{BASE}/pulls/7")][1]
    fake.add("GET", f"{BASE}/pulls/7", {**pr, "state": state, "user": {"login": author}})
    fake.add("POST", f"{BASE}/check-runs", {"id": 1}, status=201)
    return fake


def org_with(*members: str) -> FakeGitHub:
    fake = FakeGitHub()
    fake.add("GET", "/orgs/example/teams/infra/members", [{"login": m} for m in members])
    return fake


def command(
    body: str, *, commenter: str = "dave", fake: FakeGitHub | None = None, members: tuple[str, ...] = ("dave",)
):  # type: ignore[no-untyped-def]
    fake = fake or command_fake()
    result = override(fake, org_with(*members), POLICY, REPO, 7, commenter, body)
    return result, fake


def posted(fake: FakeGitHub) -> list[dict[str, Any]]:
    return [b for m, p, b in fake.bodies if m == "POST" and p.endswith("/check-runs")]


def test_an_approver_waives_the_exact_commit_with_a_reason() -> None:
    result, fake = command(f"/override {SHA} {REASON}")
    (body,) = posted(fake)
    assert result.handled and result.granted and result.head_sha == SHA
    assert (body["name"], body["head_sha"], body["conclusion"]) == ("OSAC CI override", SHA, "success")
    assert body["external_id"] == override_external_id("dave", SHA)
    assert body["output"]["title"] == f"{OVERRIDE_TITLE_PREFIX}dave: {REASON}"
    assert REASON in body["output"]["summary"] and "A new push needs a new override" in body["output"]["summary"]


def test_the_login_is_matched_without_regard_to_case() -> None:
    result, _ = command(f"/override {SHA.upper()} {REASON}", commenter="DAVE", members=("Dave",))
    assert result.granted


def test_a_dry_run_decides_but_posts_nothing() -> None:
    fake = command_fake()
    result = override(fake, org_with("dave"), POLICY, REPO, 7, "dave", f"/override {SHA} {REASON}", dry_run=True)
    assert result.granted and posted(fake) == []


@pytest.mark.parametrize(
    "body",
    [
        "",
        "hello",
        "/ok-to-test " + SHA,
        "/overrides " + SHA + " a long enough reason",
        "please /override " + SHA + " reason here",
    ],
)
def test_other_comments_are_ignored(body: str) -> None:
    result, fake = command(body)
    assert not result.handled and posted(fake) == []


def test_someone_who_may_not_override_is_ignored_silently() -> None:
    result, fake = command(f"/override {SHA} {REASON}", commenter="mallory")
    assert not result.handled and posted(fake) == []


def test_the_author_cannot_override_their_own_pull_request() -> None:
    result, fake = command(f"/override {SHA} {REASON}", fake=command_fake(author="dave"))
    assert result.handled and not result.granted and "someone else" in result.message and posted(fake) == []


def test_a_closed_pull_request_is_ignored() -> None:
    result, fake = command(f"/override {SHA} {REASON}", fake=command_fake(state="closed"))
    assert not result.handled and posted(fake) == []


@pytest.mark.parametrize(
    "body",
    [
        "/override",
        f"/override {SHA}",
        f"/override {SHA[:7]} {REASON}",  # a prefix is 28 bits and forgeable
        f"/override {SHA} short",
        f"/override {SHA} " + "x" * 141,
        f"/override {SHA} first line is fine\nsecond line",
        f"/override {SHA}\n{REASON}",
    ],
)
def test_a_command_that_is_not_complete_explains_the_syntax_and_posts_nothing(body: str) -> None:
    result, fake = command(body)
    assert result.handled and not result.granted and "/override" in result.message and posted(fake) == []


def test_a_short_prefix_is_a_syntax_error_not_a_stale_head() -> None:
    """Refusing it for its form, before the commit is compared, keeps a forgeable 7-digit prefix from ever matching."""
    result, _ = command(f"/override {SHA[:7]} {REASON}")
    assert not result.granted and "<reason" in result.message and "replaced" not in result.message


def test_a_replaced_head_is_refused() -> None:
    result, fake = command(f"/override {'d' * 40} {REASON}")
    assert result.handled and not result.granted and "replaced" in result.message and posted(fake) == []


def test_without_an_override_section_every_comment_is_ignored() -> None:
    plain = parse_policy("version: 1\nrepo: example/app\njobs: {a: {check: check-0}}\n")
    fake = command_fake()
    assert not override(fake, org_with("dave"), plain, REPO, 7, "dave", f"/override {SHA} {REASON}").handled


def test_an_unreadable_team_fails_loudly_and_posts_nothing() -> None:
    fake = command_fake()
    with pytest.raises(ValueError, match="override team"):
        override(fake, FakeGitHub(), POLICY, REPO, 7, "dave", f"/override {SHA} {REASON}")
    assert posted(fake) == []


def test_a_rejected_post_is_an_error() -> None:
    fake = command_fake()
    fake.add("POST", f"{BASE}/check-runs", {"message": "no"}, status=403)
    with pytest.raises(GitHubError):
        override(fake, org_with("dave"), POLICY, REPO, 7, "dave", f"/override {SHA} {REASON}")


def test_the_cli_command_replies_on_the_pull_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = command_fake()
    fake.add("POST", f"{BASE}/issues/7/comments", {"id": 9}, status=201)
    monkeypatch.setattr(cli, "build_client", lambda: fake)
    monkeypatch.setattr(cli, "build_org_client", lambda required=False: org_with("dave"))
    monkeypatch.setenv("OSAC_CI_COMMENT", f"/override {SHA} {REASON}")
    policy_file = "policy/toy.yml"
    # the toy policy has no override section, so write one next to it
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "p.yml"
        path.write_text(
            "version: 1\nrepo: example/app\noverride: {approvers: ['@example/infra']}\njobs: {a: {check: check-0}}\n"
        )
        code = cli.main(["override", "--policy", str(path), "--repo", REPO, "--number", "7", "--commenter", "dave"])
    assert code == 0 and "granted" in capsys.readouterr().out
    assert any(p.endswith("/issues/7/comments") for _, p, _ in fake.bodies) and policy_file
