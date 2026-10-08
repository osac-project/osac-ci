"""Sha-bound authorization: the adapter reads it back, and /ok-to-test <sha> records it. Fake GitHub, no network."""

from __future__ import annotations

from typing import Any

import pytest
from fakes import REPO, SHA, FakeGitHub, check_runs, standard_fake

from osac_ci import cli
from osac_ci.authorize import authorize
from osac_ci.github.api import GitHubError
from osac_ci.github.snapshot import authorization_external_id, fetch_snapshot, find_authorizer
from osac_ci.model import CheckRun
from osac_ci.policy import Policy, Trust, parse_policy

pytestmark = pytest.mark.contract
BASE = f"/repos/{REPO}"
CHECKS = f"{BASE}/commits/{SHA}/check-runs"
POLICY = parse_policy("version: 1\nrepo: example/app\ntrust: {authorization: sha-bound}\njobs: {a: {check: check-0}}\n")
LEGACY = parse_policy("version: 1\nrepo: example/app\njobs: {a: {check: check-0}}\n")
TRUST = Trust(authorization="sha-bound")


def run(login: str = "reviewer", sha: str = SHA, **kw: Any) -> CheckRun:
    base: dict[str, Any] = {
        "name": "OSAC CI authorization", "status": "completed", "conclusion": "success", "app": "github-actions",
        "external_id": authorization_external_id(login, sha), "started_at": "2026-10-08T10:00:00Z",
    }  # fmt: skip
    base.update(kw)
    return CheckRun(**base)


def org(*members: str) -> FakeGitHub:
    fake = FakeGitHub()
    for m in members:
        fake.add("GET", f"/orgs/example/members/{m}", None, status=204)
    return fake


# ---- reading an authorization back --------------------------------------------------------------------------------


def test_a_valid_authorization_by_a_member_is_found() -> None:
    assert find_authorizer(org("reviewer"), "example", [run()], SHA, TRUST) == "reviewer"


@pytest.mark.parametrize(
    "bad",
    [
        run(name="something else"),
        run(conclusion="failure"),
        run(status="in_progress"),
        run(app="some-other-app"),  # only the workflow's built-in token counts
        run(sha="d" * 40),  # an authorization of another commit
        run(external_id="osac-ci-auth:v1:reviewer"),
        run(external_id=""),
        run(external_id=f"osac-ci-auth:v2:reviewer:{SHA}"),
        run(external_id=f"osac-ci-auth:v1:../../etc:{SHA}"),
    ],
)
def test_anything_else_is_not_an_authorization(bad: CheckRun) -> None:
    assert find_authorizer(org("reviewer"), "example", [bad], SHA, TRUST) == ""


def test_an_authorizer_who_left_the_org_no_longer_counts() -> None:
    fake = FakeGitHub()
    fake.add("GET", "/orgs/example/members/reviewer", None, status=404)
    assert find_authorizer(fake, "example", [run()], SHA, TRUST) == ""


def test_the_newest_valid_authorization_by_a_current_member_wins() -> None:
    runs = [run("old", started_at="2026-10-08T09:00:00Z"), run("new", started_at="2026-10-08T11:00:00Z")]
    assert find_authorizer(org("old", "new"), "example", runs, SHA, TRUST) == "new"
    assert find_authorizer(org("old"), "example", runs, SHA, TRUST) == "old"  # "new" is not a member: skipped


def test_a_membership_lookup_that_fails_is_an_error_not_a_no() -> None:
    fake = FakeGitHub()
    fake.add("GET", "/orgs/example/members/reviewer", {"message": "rate limited"}, status=403)
    with pytest.raises(GitHubError):
        find_authorizer(fake, "example", [run()], SHA, TRUST)


def snapshot_with_runs(
    fake: FakeGitHub, *extra: dict[str, Any], policy: Policy = POLICY, org_client: FakeGitHub | None = None
):  # type: ignore[no-untyped-def]
    data = check_runs(3)
    data["check_runs"] = [*data["check_runs"], *extra]
    fake.add("GET", CHECKS, data)
    return fetch_snapshot(fake, REPO, 7, org="example", trust=policy.trust, org_client=org_client)


def posted_auth(login: str = "reviewer") -> dict[str, Any]:
    return {
        "name": "OSAC CI authorization", "status": "completed", "conclusion": "success",
        "started_at": "2026-10-08T10:00:00Z", "app": {"slug": "github-actions"},
        "external_id": authorization_external_id(login, SHA),
    }  # fmt: skip


def test_the_snapshot_of_a_fork_pr_carries_its_authorizer() -> None:
    s = snapshot_with_runs(standard_fake(), posted_auth(), org_client=org("reviewer"))
    assert s.authorized_by == "reviewer"


def test_check_runs_keep_their_external_id_and_app() -> None:
    s = snapshot_with_runs(standard_fake(), posted_auth(), org_client=org("reviewer"))
    auth = next(r for r in s.check_runs if r.name == "OSAC CI authorization")
    assert auth.app == "github-actions" and auth.external_id.startswith("osac-ci-auth:v1:reviewer:")


def test_no_authorizer_lookup_in_label_mode_or_for_trusted_authors() -> None:
    other = org("reviewer")
    assert snapshot_with_runs(standard_fake(), posted_auth(), policy=LEGACY, org_client=other).authorized_by == ""
    member_fake = standard_fake()
    member_fake.add("GET", "/orgs/example/members/alice", None, status=204)
    assert snapshot_with_runs(member_fake, posted_auth(), org_client=org("reviewer", "alice")).authorized_by == ""
    assert not [c for c in other.calls if c[1].endswith("/reviewer")]


def test_a_same_repo_pr_needs_no_authorizer() -> None:
    fake = standard_fake(head={"sha": SHA, "repo": {"full_name": REPO, "owner": {"login": "example"}}})
    other = org("reviewer")
    assert snapshot_with_runs(fake, posted_auth(), org_client=other).authorized_by == ""
    assert not [c for c in other.calls if c[1].endswith("/reviewer")]  # only the author's own membership was asked


# ---- the /ok-to-test <sha> command -----------------------------------------------------------------------------------


def fake_for_command() -> FakeGitHub:
    fake = standard_fake()
    fake.add("POST", f"{BASE}/check-runs", {"id": 1}, status=201)
    return fake


def command(
    fake: FakeGitHub, body: str, commenter: str = "reviewer", members: tuple[str, ...] = ("reviewer",), **kw: Any
):  # type: ignore[no-untyped-def]
    return authorize(fake, org(*members), kw.pop("policy", POLICY), REPO, 7, commenter, body, org="example", **kw)


def posts(fake: FakeGitHub) -> list[dict[str, Any]]:
    return [b for m, p, b in fake.bodies if m == "POST" and p.endswith("/check-runs")]


def test_a_member_authorizes_the_exact_head() -> None:
    fake = fake_for_command()
    result = command(fake, f"/ok-to-test {SHA}")
    (body,) = posts(fake)
    assert result.handled and result.granted and result.head_sha == SHA
    assert (body["name"], body["head_sha"], body["status"], body["conclusion"]) == (
        "OSAC CI authorization",
        SHA,
        "completed",
        "success",
    )
    assert body["external_id"] == authorization_external_id("reviewer", SHA)


@pytest.mark.parametrize(
    "text", [f"/ok-to-test {SHA}", f"  /ok-to-test   {SHA.upper()}  \n", f"\n/ok-to-test\t{SHA}\n"]
)
def test_the_full_sha_is_accepted_in_either_case_and_the_spacing_is_forgiving(text: str) -> None:
    assert command(fake_for_command(), text).granted


def test_a_replaced_head_is_refused() -> None:
    fake = fake_for_command()
    result = command(fake, f"/ok-to-test {'d' * 40}")
    assert result.handled and not result.granted and posts(fake) == []
    assert f"The head is now `{SHA}`" in result.message and f"`/ok-to-test {SHA}`" in result.message


@pytest.mark.parametrize(
    "text",
    [
        "/ok-to-test",
        "/ok-to-test please",
        f"/ok-to-test {SHA} thanks",
        f"/ok-to-test {'x' * 40}",
        # a prefix is never enough, however long: a crafted commit can share it (28 bits for 7 digits)
        f"/ok-to-test {SHA[:7]}",
        f"/ok-to-test {SHA[:12]}",
        f"/ok-to-test {SHA[:39]}",
        f"/ok-to-test {SHA}0",
    ],
)
def test_a_command_without_the_full_sha_gets_the_exact_command_back_and_authorizes_nothing(text: str) -> None:
    fake = fake_for_command()
    result = command(fake, text)
    assert result.handled and not result.granted and posts(fake) == []
    assert f"`/ok-to-test {SHA}`" in result.message


@pytest.mark.parametrize("text", ["looks good", "/lgtm", "please /ok-to-test " + SHA, ""])
def test_other_comments_are_ignored(text: str) -> None:
    fake = fake_for_command()
    result = command(fake, text)
    assert not result.handled and posts(fake) == []


def test_a_commenter_who_is_not_an_org_member_is_ignored_silently() -> None:
    fake = fake_for_command()
    fake_org = FakeGitHub()
    fake_org.add("GET", "/orgs/example/members/outsider", None, status=404)
    result = authorize(fake, fake_org, POLICY, REPO, 7, "outsider", f"/ok-to-test {SHA}", org="example")
    assert not result.handled and posts(fake) == []


def test_a_closed_pr_is_ignored() -> None:
    fake = fake_for_command()
    fake.add("GET", f"{BASE}/pulls/7", {**standard_fake().routes[("GET", f"{BASE}/pulls/7")][1], "state": "closed"})
    assert not command(fake, f"/ok-to-test {SHA}").handled


def test_nothing_happens_when_the_policy_is_not_sha_bound() -> None:
    fake = fake_for_command()
    assert not command(fake, f"/ok-to-test {SHA}", policy=LEGACY).handled and posts(fake) == []


def test_dry_run_decides_without_posting() -> None:
    fake = fake_for_command()
    assert command(fake, f"/ok-to-test {SHA}", dry_run=True).granted and posts(fake) == []


def test_a_rejected_check_post_is_an_error() -> None:
    fake = standard_fake()
    fake.add("POST", f"{BASE}/check-runs", {"message": "Resource not accessible"}, status=403)
    with pytest.raises(GitHubError, match="403"):
        command(fake, f"/ok-to-test {SHA}")


def test_the_authorization_is_read_back_by_the_adapter() -> None:
    # What the command posts is exactly what find_authorizer accepts.
    fake = fake_for_command()
    command(fake, f"/ok-to-test {SHA}")
    (body,) = posts(fake)
    posted = CheckRun(
        body["name"], "completed", "success", "2026-10-08T10:00:00Z", body["external_id"], "github-actions"
    )
    assert find_authorizer(org("reviewer"), "example", [posted], SHA, TRUST) == "reviewer"


# ---- the command line --------------------------------------------------------------------------------------------


def cli_run(
    monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, body: str, *extra: str, members: tuple[str, ...] = ("reviewer",)
) -> int:
    monkeypatch.setattr(cli, "build_client", lambda: fake)
    monkeypatch.setattr(cli, "build_org_client", lambda *, required=False: org(*members))
    monkeypatch.setenv(cli.COMMENT_ENV, body)
    return cli.main(
        [
            "authorize",
            "--policy",
            "policy/osac-ci.yml",
            "--repo",
            REPO,
            "--number",
            "7",
            "--commenter",
            "reviewer",
            "--org",
            "example",
            *extra,
        ]
    )


def replies(fake: FakeGitHub) -> list[str]:
    return [b["body"] for m, p, b in fake.bodies if p.endswith("/comments")]


def test_cli_grants_and_replies(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = fake_for_command()
    fake.add("POST", f"{BASE}/issues/7/comments", {"id": 2}, status=201)
    assert cli_run(monkeypatch, fake, f"/ok-to-test {SHA}") == 0
    assert "granted" in capsys.readouterr().out and len(posts(fake)) == 1
    assert replies(fake) == [f"Authorized `{SHA[:7]}` (by `reviewer`). A new push needs a new authorization."]


def test_cli_refuses_a_stale_sha_with_a_reply_but_exit_0(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_for_command()
    fake.add("POST", f"{BASE}/issues/7/comments", {"id": 2}, status=201)
    assert cli_run(monkeypatch, fake, f"/ok-to-test {'1' * 40}") == 0 and posts(fake) == []
    assert "The head is now" in replies(fake)[0]


def test_cli_ignores_other_comments_without_replying(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = fake_for_command()
    assert cli_run(monkeypatch, fake, "thanks!") == 0
    assert "ignored" in capsys.readouterr().out and replies(fake) == [] and posts(fake) == []


def test_cli_dry_run_posts_and_replies_to_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = fake_for_command()
    assert cli_run(monkeypatch, fake, f"/ok-to-test {SHA}", "--dry-run") == 0
    assert posts(fake) == [] and replies(fake) == []


def test_cli_reports_an_api_failure_with_exit_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = standard_fake()
    fake.add("POST", f"{BASE}/check-runs", {"message": "nope"}, status=403)
    assert cli_run(monkeypatch, fake, f"/ok-to-test {SHA}") == 3
    assert "authorize failed" in capsys.readouterr().err


def test_cli_reports_a_failed_reply_with_exit_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = fake_for_command()
    fake.add("POST", f"{BASE}/issues/7/comments", {"message": "forbidden"}, status=403)
    assert cli_run(monkeypatch, fake, f"/ok-to-test {SHA}") == 3
    assert "authorize failed" in capsys.readouterr().err


def test_a_commit_that_shares_a_short_prefix_with_the_reviewed_one_is_not_authorized() -> None:
    # The attack the full SHA closes: the reviewer saw SHA, the author swaps in a commit whose SHA starts the same way.
    crafted = SHA[:7] + "e" * 33
    fake = standard_fake(head={"sha": crafted, "repo": {"full_name": "alice/app", "owner": {"login": "alice"}}})
    fake.add("POST", f"{BASE}/check-runs", {"id": 1}, status=201)
    for text in (f"/ok-to-test {SHA[:7]}", f"/ok-to-test {SHA}"):
        result = command(fake, text)
        assert result.handled and not result.granted
    assert posts(fake) == []
