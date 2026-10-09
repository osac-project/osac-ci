"""Merge-queue commits: the snapshot, the branch name, and publishing the verdict on the queue commit."""

from __future__ import annotations

from typing import Any

import pytest
from fakes import REPO, FakeGitHub

from osac_ci import cli
from osac_ci.github.api import GitHubError
from osac_ci.github.snapshot import fetch_queue_snapshot, parse_queue_branch
from osac_ci.policy import Policy, parse_policy
from osac_ci.publish import publish_queue

pytestmark = pytest.mark.contract
BASE = f"/repos/{REPO}"
QSHA = "ab" * 20
BRANCH = f"gh-readonly-queue/main/pr-12-{QSHA}"
POLICY = parse_policy(
    "version: 1\nrepo: example/app\nmerge: {required_labels: [], blocking_labels: []}\n"
    "jobs:\n  lint: {check: lint}\n  test: {check: test}\n  site: {check: site, paths: ['docs/**']}\n"
    "  pr-only: {check: pr-only, required_at: [pr]}\n"
)


def check(name: str, conclusion: str | None = "success", status: str = "completed") -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion, "started_at": "2026-10-08T10:00:00Z"}


def queue_fake(runs: list[dict[str, Any]], files: list[str] | None = None, compare_status: int = 200) -> FakeGitHub:
    fake = FakeGitHub()
    fake.add("GET", f"{BASE}/commits/{QSHA}/check-runs", {"total_count": len(runs), "check_runs": runs})
    body = {"files": [{"filename": f} for f in (files if files is not None else ["a.go"])]}
    fake.add("GET", f"{BASE}/compare/main...{QSHA}", body, status=compare_status)
    fake.add("POST", f"{BASE}/check-runs", {"id": 1}, status=201)
    return fake


GREEN = [check("lint"), check("test")]


def posted(fake: FakeGitHub) -> list[dict[str, Any]]:
    return [b for m, p, b in fake.bodies if m == "POST" and p.endswith("/check-runs")]


def publish(fake: FakeGitHub, policy: Policy = POLICY, **kw: Any):  # type: ignore[no-untyped-def]
    return publish_queue(fake, policy, REPO, QSHA, "main", **kw)


# ---- the queue branch name -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "base"),
    [
        (f"gh-readonly-queue/main/pr-12-{QSHA}", "main"),
        (f"gh-readonly-queue/release/1.0/pr-3-{QSHA}", "release/1.0"),
        (f"gh-readonly-queue/feature/a/b/pr-100-{QSHA}", "feature/a/b"),
    ],
)
def test_the_base_branch_comes_from_the_queue_branch_name(name: str, base: str) -> None:
    assert parse_queue_branch(name) == base


@pytest.mark.parametrize(
    "name",
    [
        "main",
        "refs/heads/main",
        f"refs/heads/gh-readonly-queue/main/pr-12-{QSHA}",
        f"gh-readonly-queue/main/pr-12-{QSHA[:39]}",
        f"gh-readonly-queue/main/pr-12-{QSHA}0",
        f"gh-readonly-queue/main/pr-x-{QSHA}",
        f"gh-readonly-queue//pr-12-{QSHA}",
        f"gh-readonly-queue/main/pr-12-{QSHA.upper()}",
        f"gh-readonly-queue/main/pr-12-{QSHA}\nmore",
    ],
)
def test_anything_else_is_not_a_queue_branch(name: str) -> None:
    assert parse_queue_branch(name) is None


# ---- the snapshot ----------------------------------------------------------------------------------------------------


def test_the_snapshot_holds_the_checks_and_the_files_of_the_whole_queue_group() -> None:
    s = fetch_queue_snapshot(queue_fake(GREEN, ["a.go", "docs/x.md"]), REPO, QSHA, "main")
    assert (s.number, s.head_sha, s.base_ref) == (0, QSHA, "main")
    assert [c.name for c in s.check_runs] == ["lint", "test"]
    assert s.changed_files == ("a.go", "docs/x.md") and s.changed_files_known


def test_a_compare_at_the_file_cap_is_marked_unknown_not_empty() -> None:
    s = fetch_queue_snapshot(queue_fake(GREEN, [f"f{i}.go" for i in range(300)]), REPO, QSHA, "main")
    assert s.changed_files == () and not s.changed_files_known


def test_a_failed_compare_is_unknown_not_a_reason_to_skip() -> None:
    s = fetch_queue_snapshot(queue_fake(GREEN, compare_status=404), REPO, QSHA, "main")
    assert s.changed_files == () and not s.changed_files_known


@pytest.mark.parametrize("sha", ["", "abc", "g" * 40, QSHA.upper(), QSHA + "0", "../" + QSHA])
def test_a_commit_that_is_not_40_hex_digits_is_refused(sha: str) -> None:
    with pytest.raises(ValueError, match="invalid commit sha"):
        fetch_queue_snapshot(queue_fake(GREEN), REPO, sha, "main")


def test_the_branch_name_with_odd_characters_is_quoted_in_the_compare_path() -> None:
    fake = queue_fake(GREEN)
    fake.add("GET", f"{BASE}/compare/release/1.0...{QSHA}", {"files": []})
    assert fetch_queue_snapshot(fake, REPO, QSHA, "release/1.0").changed_files_known


# ---- publishing ------------------------------------------------------------------------------------------------------


def test_all_required_checks_passed_is_a_success_on_the_queue_commit() -> None:
    fake = queue_fake(GREEN)
    out = publish(fake)
    (body,) = posted(fake)
    assert out.action == "created" and out.state.value == "queue-passed"
    assert (body["name"], body["head_sha"], body["status"], body["conclusion"]) == (
        "OSAC CI",
        QSHA,
        "completed",
        "success",
    )


def test_a_pending_required_check_keeps_the_queue_check_in_progress() -> None:
    fake = queue_fake([check("lint"), check("test", None, "in_progress")])
    publish(fake)
    (body,) = posted(fake)
    assert body["status"] == "in_progress" and "conclusion" not in body


def test_a_required_check_that_has_not_reported_yet_is_pending_not_passed() -> None:
    fake = queue_fake([check("lint")])
    publish(fake)
    assert posted(fake)[0]["status"] == "in_progress"


def test_a_failed_required_check_is_red_so_the_queue_ejects_the_entry() -> None:
    fake = queue_fake([check("lint"), check("test", "failure")])
    out = publish(fake)
    (body,) = posted(fake)
    assert out.state.value == "queue-failed" and body["conclusion"] == "failure"
    assert "queue check failed: test" in body["output"]["title"]


def test_jobs_required_only_on_the_pr_are_ignored_in_the_queue() -> None:
    fake = queue_fake([*GREEN, check("pr-only", "failure")])
    publish(fake)
    assert posted(fake)[0]["conclusion"] == "success"


def test_the_checks_own_name_is_not_a_job_so_it_never_counts_itself() -> None:
    fake = queue_fake([*GREEN, check("OSAC CI", "failure")])
    publish(fake)
    assert posted(fake)[0]["conclusion"] == "success"


def test_path_gated_jobs_follow_the_files_of_the_whole_queue_group() -> None:
    not_docs = queue_fake(GREEN, ["a.go"])  # `site` needs docs/**: not applicable, so it need not report
    publish(not_docs)
    assert posted(not_docs)[0]["conclusion"] == "success"
    docs = queue_fake(GREEN, ["docs/a.md"])  # applicable, and site has not reported
    publish(docs)
    assert posted(docs)[0]["status"] == "in_progress"


def test_unknown_files_never_skip_a_path_gated_job() -> None:
    fake = queue_fake(GREEN, [f"f{i}.go" for i in range(300)])  # over the cap: the list is unknown
    publish(fake)
    assert posted(fake)[0]["status"] == "in_progress"  # site could apply, so it still counts and has not reported


def test_an_unchanged_verdict_is_not_posted_again() -> None:
    first = queue_fake(GREEN)
    publish(first)
    external_id = posted(first)[0]["external_id"]
    again = queue_fake([*GREEN, {**check("OSAC CI"), "id": 5, "external_id": external_id}])
    again.add("GET", f"{BASE}/commits/{QSHA}/check-runs", again.routes[("GET", f"{BASE}/commits/{QSHA}/check-runs")][1])
    assert publish(again).action == "unchanged" and posted(again) == []


def test_a_commit_that_cannot_be_read_is_a_visible_failure_not_a_pass() -> None:
    fake = queue_fake(GREEN)
    fake.add("GET", f"{BASE}/commits/{QSHA}/check-runs", {"message": "boom"}, status=500)
    out = publish(fake)
    (body,) = posted(fake)
    assert out.state.value == "planner-error" and body["conclusion"] == "failure"
    assert "could not read queue commit abababa" in body["output"]["summary"]


def test_dry_run_posts_nothing() -> None:
    fake = queue_fake(GREEN)
    assert publish(fake, dry_run=True).action == "dry-run" and posted(fake) == []


def test_a_rejected_post_is_an_error() -> None:
    fake = queue_fake(GREEN)
    fake.add("POST", f"{BASE}/check-runs", {"message": "Resource not accessible"}, status=403)
    with pytest.raises(GitHubError, match="403"):
        publish(fake)


# ---- the command line ------------------------------------------------------------------------------------------------


def cli_run(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, *args: str) -> int:
    monkeypatch.setattr(cli, "build_client", lambda: fake)
    return cli.main(["publish-queue", "--policy", "policy/toy.yml", "--repo", REPO, *args])


def test_cli_publishes_a_queue_commit(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = queue_fake([check("lint"), check("test"), check("site")])
    assert cli_run(monkeypatch, fake, "--sha", QSHA, "--branch", BRANCH) == 0
    assert "created" in capsys.readouterr().out and len(posted(fake)) == 1


def test_cli_refuses_a_branch_that_is_not_a_queue_branch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = queue_fake(GREEN)
    assert cli_run(monkeypatch, fake, "--sha", QSHA, "--branch", "main") == 2
    assert "not a merge-queue branch" in capsys.readouterr().err and fake.calls == []


def test_cli_refuses_a_bad_sha_before_any_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = queue_fake(GREEN)
    assert cli_run(monkeypatch, fake, "--sha", "nothex", "--branch", BRANCH) == 3
    assert "invalid commit sha" in capsys.readouterr().err and fake.calls == []


def test_cli_reports_a_rejected_post_with_exit_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = queue_fake(GREEN)
    fake.add("POST", f"{BASE}/check-runs", {"message": "nope"}, status=403)
    assert cli_run(monkeypatch, fake, "--sha", QSHA, "--branch", BRANCH) == 3
    assert "publish-queue failed" in capsys.readouterr().err
