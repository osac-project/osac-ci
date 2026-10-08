"""Publishing the verdict as a check run, against the fake GitHub (no network)."""

from __future__ import annotations

from typing import Any

import pytest
from fakes import REPO, SHA, FakeGitHub, check_runs, standard_fake

from osac_ci import cli
from osac_ci.github.api import GitHubError
from osac_ci.policy import Policy, parse_policy
from osac_ci.publish import describe, publish_pr, sweep

pytestmark = pytest.mark.contract
BASE = f"/repos/{REPO}"
CHECKS = f"{BASE}/commits/{SHA}/check-runs"

# The fake PR carries lgtm and approved and three green checks named check-0 .. check-2.
READY = parse_policy(
    "version: 1\nrepo: example/app\nmerge: {required_labels: [lgtm, approved], blocking_labels: []}\n"
    "jobs: {a: {check: check-0}, b: {check: check-1}, c: {check: check-2}}\n"
)
WAITING = parse_policy(
    "version: 1\nrepo: example/app\nmerge: {required_labels: [lgtm, approved], blocking_labels: []}\n"
    "jobs: {a: {check: check-0}, z: {check: never-reported}}\n"
)


def fake_with_publish_routes() -> FakeGitHub:
    fake = standard_fake()
    fake.add("POST", f"{BASE}/check-runs", {"id": 99}, status=201)
    return fake


def with_existing(fake: FakeGitHub, *runs: dict[str, Any]) -> FakeGitHub:
    """Serve the three green checks plus already-posted OSAC CI runs (one route answers both lookups)."""
    data = check_runs(3)
    data["check_runs"] = [
        *data["check_runs"],
        *({"name": "OSAC CI", "status": "completed", "conclusion": "success", **r} for r in runs),
    ]
    fake.add("GET", CHECKS, data)
    return fake


def posted(fake: FakeGitHub) -> list[dict[str, Any]]:
    return [b for m, p, b in fake.bodies if m == "POST" and p.endswith("/check-runs")]


def publish(fake: FakeGitHub, policy: Policy = READY, **kw: Any):  # type: ignore[no-untyped-def]
    return publish_pr(fake, policy, REPO, 7, org="example", **kw)


def test_a_ready_pr_gets_a_success_check_on_its_head_commit() -> None:
    fake = fake_with_publish_routes()
    out = publish(fake)
    (body,) = posted(fake)
    assert (out.action, out.head_sha) == ("created", SHA)
    assert (body["name"], body["head_sha"], body["status"], body["conclusion"]) == (
        "OSAC CI",
        SHA,
        "completed",
        "success",
    )


def test_a_waiting_pr_is_in_progress_with_no_conclusion() -> None:
    fake = fake_with_publish_routes()
    publish(fake, WAITING)
    (body,) = posted(fake)
    assert body["status"] == "in_progress" and "conclusion" not in body


def test_an_unchanged_verdict_is_not_posted_again() -> None:
    first = fake_with_publish_routes()
    publish(first)
    (body,) = posted(first)
    again = with_existing(fake_with_publish_routes(), {"id": 5, "external_id": body["external_id"]})
    out = publish(again)
    assert out.action == "unchanged" and posted(again) == []


def test_a_changed_verdict_is_posted_even_if_an_older_run_exists() -> None:
    fake = with_existing(fake_with_publish_routes(), {"id": 5, "external_id": "osac-ci:old"})
    assert publish(fake).action == "created"


def test_only_the_newest_existing_run_is_compared() -> None:
    first = fake_with_publish_routes()
    publish(first)
    current = posted(first)[0]["external_id"]
    fake = with_existing(
        fake_with_publish_routes(),
        {"id": 9, "external_id": "osac-ci:newer-but-different"},
        {"id": 3, "external_id": current},
    )
    assert publish(fake).action == "created"


def test_dry_run_posts_nothing_and_asks_nothing_about_existing_runs() -> None:
    fake = fake_with_publish_routes()
    out = publish(fake, dry_run=True)
    assert out.action == "dry-run" and posted(fake) == []


def test_a_pr_that_cannot_be_read_fully_gets_a_visible_failure_not_a_pass() -> None:
    fake = fake_with_publish_routes()
    fake.add("GET", f"{BASE}/pulls/7/reviews", {"message": "boom"}, status=500)
    out = publish(fake)
    (body,) = posted(fake)
    assert out.state.value == "planner-error" and body["conclusion"] == "failure"
    assert "could not read PR #7" in body["output"]["summary"]


def test_a_rejected_post_is_an_error_not_silently_ignored() -> None:
    fake = standard_fake()
    fake.add("POST", f"{BASE}/check-runs", {"message": "Resource not accessible"}, status=403)
    with pytest.raises(GitHubError, match="403"):
        publish(fake)


def test_a_missing_pr_is_an_error() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/pulls/7", {"message": "Not Found"}, status=404)
    with pytest.raises(GitHubError):
        publish(fake)


def test_the_sweep_publishes_for_every_open_pr_and_survives_one_failing() -> None:
    fake = fake_with_publish_routes()
    fake.add(
        "GET",
        f"{BASE}/pulls",
        [{"number": 7, "draft": False, "head": {"sha": SHA}}, {"number": 8, "head": {"sha": "e" * 40}}],
    )
    outcomes = sweep(fake, READY, REPO, org="example")
    assert {(o.pr, o.action) for o in outcomes} == {(7, "created"), (8, "failed")}
    text = describe(outcomes)
    assert "PR #7: created success (ready-to-enqueue)" in text and "2 PRs, 1 failed" in text


def test_the_sweep_includes_drafts_so_they_show_what_they_wait_for() -> None:
    fake = fake_with_publish_routes()
    fake.add("GET", f"{BASE}/pulls", [{"number": 7, "draft": True, "head": {"sha": SHA}}])
    assert [o.pr for o in sweep(fake, READY, REPO, org="example", dry_run=True)] == [7]


def test_the_sweep_respects_a_limit() -> None:
    fake = fake_with_publish_routes()
    fake.add("GET", f"{BASE}/pulls", [{"number": 7, "head": {"sha": SHA}}, {"number": 8, "head": {"sha": SHA}}])
    assert [o.pr for o in sweep(fake, READY, REPO, org="example", dry_run=True, limit=1)] == [7]


# ---- the command line -----------------------------------------------------------------------------------------

POLICY_ARGS = ["--policy", "policy/toy.yml", "--repo", REPO, "--org", "example"]


def run_cli(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, *args: str) -> int:
    monkeypatch.setattr(cli, "build_client", lambda: fake)
    return cli.main(["publish", *POLICY_ARGS, *args])


def test_cli_publishes_one_pr(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = fake_with_publish_routes()
    assert run_cli(monkeypatch, fake, "--number", "7", "--note", "Informational") == 0
    assert "PR #7: created" in capsys.readouterr().out
    assert posted(fake)[0]["output"]["summary"].startswith("Informational")


def test_cli_dry_run_posts_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = fake_with_publish_routes()
    assert run_cli(monkeypatch, fake, "--number", "7", "--dry-run") == 0
    assert posted(fake) == [] and "dry-run" in capsys.readouterr().out


def test_cli_sweep_exits_1_when_any_pr_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = fake_with_publish_routes()
    fake.add("GET", f"{BASE}/pulls", [{"number": 7, "head": {"sha": SHA}}, {"number": 8, "head": {"sha": SHA}}])
    assert run_cli(monkeypatch, fake, "--all") == 1
    assert "1 failed" in capsys.readouterr().out


def test_cli_reports_an_api_error_with_exit_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/pulls/7", {"message": "Not Found"}, status=404)
    assert run_cli(monkeypatch, fake, "--number", "7") == 3
    assert "publish failed" in capsys.readouterr().err


def test_cli_needs_a_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "build_client", lambda: standard_fake())
    with pytest.raises(SystemExit):
        cli.main(["publish", *POLICY_ARGS])


def test_a_missing_org_token_that_is_required_becomes_a_visible_failure_not_a_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(cli.ORG_TOKEN_ENV, raising=False)
    fake = fake_with_publish_routes()
    code = run_cli(monkeypatch, fake, "--number", "7", "--lookup-membership", "--require-org-token")
    (body,) = posted(fake)
    assert code == 0 and "planner-error" in capsys.readouterr().out
    assert body["conclusion"] == "failure" and "OSAC_CI_ORG_TOKEN is not available" in body["output"]["summary"]
    assert not [c for c in fake.calls if c[1].startswith("/orgs/")]  # the main token never asked about the org


def test_without_the_require_flag_the_main_client_is_used_for_org_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cli.ORG_TOKEN_ENV, raising=False)
    assert cli.build_org_client() is None
    assert isinstance(cli.build_org_client(required=True), cli._NoOrgToken)  # type: ignore[attr-defined]
    monkeypatch.setenv(cli.ORG_TOKEN_ENV, "x")
    assert isinstance(cli.build_org_client(required=True), cli.HttpClient)
