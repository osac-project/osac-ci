"""Replay against a fake GitHub holding several closed PRs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fakes import REPO, FakeGitHub
from helpers import ROOT

from osac_ci import cli
from osac_ci.model import State
from osac_ci.policy import load_policy
from osac_ci.replay import load_explained, merged_prs, render, replay, to_json

pytestmark = pytest.mark.contract
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
TOY = load_policy(ROOT / "policy" / "toy.yml")  # required label: approved; checks: lint, test (site is docs-only)
BASE = f"/repos/{REPO}"


def add_pr(
    fake: FakeGitHub,
    listing: list[dict[str, Any]],
    number: int,
    *,
    merged: str | None,
    updated: str,
    labels: tuple[str, ...] = ("approved",),
    lint: str = "success",
) -> None:
    sha = f"{number:040x}"
    listing.append({"number": number, "merged_at": merged, "updated_at": updated, "closed_at": updated})
    fake.add(
        "GET",
        f"{BASE}/pulls/{number}",
        {
            "number": number,
            "node_id": f"PR_{number}",
            "draft": False,
            "user": {"login": "alice"},
            "author_association": "MEMBER",
            "labels": [{"name": name} for name in labels],
            "head": {"sha": sha, "repo": {"full_name": REPO, "owner": {"login": "example"}}},
        },
    )
    fake.add("GET", f"{BASE}/pulls/{number}/reviews", [])
    fake.add("GET", f"{BASE}/issues/{number}/events", [])
    fake.add("GET", f"{BASE}/pulls/{number}/files", [{"filename": "a.go"}])
    runs = [
        {"name": "lint", "status": "completed", "conclusion": lint, "started_at": "2026-01-01T00:00:01Z"},
        {"name": "test", "status": "completed", "conclusion": "success", "started_at": "2026-01-01T00:00:02Z"},
    ]
    fake.add("GET", f"{BASE}/commits/{sha}/check-runs", {"total_count": 2, "check_runs": runs})


def world() -> tuple[FakeGitHub, list[dict[str, Any]]]:
    fake, listing = FakeGitHub(), []
    fake.add("POST", "/graphql", {"data": {"node": {"mergeQueueEntry": None}}})
    fake.routes[("GET", f"{BASE}/pulls")] = (200, listing)
    return fake, listing


def test_window_only_includes_prs_merged_inside_it() -> None:
    fake, listing = world()
    add_pr(fake, listing, 30, merged="2026-10-06T10:00:00Z", updated="2026-10-06T10:00:00Z")
    add_pr(fake, listing, 29, merged=None, updated="2026-10-05T10:00:00Z")  # closed, never merged
    add_pr(fake, listing, 28, merged="2026-07-01T10:00:00Z", updated="2026-10-04T10:00:00Z")  # merged long ago
    add_pr(fake, listing, 27, merged="2026-10-01T10:00:00Z", updated="2026-10-02T10:00:00Z")
    add_pr(fake, listing, 26, merged="2026-06-01T10:00:00Z", updated="2026-06-01T10:00:00Z")  # before the cutoff
    found = merged_prs(fake, REPO, now=NOW, days=60, limit=100)
    assert [pr["number"] for pr in found] == [30, 27]


def test_scan_stops_at_the_first_pr_last_touched_before_the_cutoff() -> None:
    fake, listing = world()
    add_pr(fake, listing, 3, merged="2026-10-06T10:00:00Z", updated="2026-10-06T10:00:00Z")
    add_pr(fake, listing, 2, merged="2026-01-01T10:00:00Z", updated="2026-01-01T10:00:00Z")
    add_pr(fake, listing, 1, merged="2026-10-05T10:00:00Z", updated="2026-10-05T10:00:00Z")  # out of order on purpose
    assert [pr["number"] for pr in merged_prs(fake, REPO, now=NOW, days=60, limit=100)] == [3]


def test_limit_is_respected() -> None:
    fake, listing = world()
    for n in range(10, 0, -1):
        add_pr(fake, listing, n, merged="2026-10-06T10:00:00Z", updated="2026-10-06T10:00:00Z")
    report = replay(fake, TOY, REPO, now=NOW, limit=3, org="example", lookup_membership=False)
    assert len(report.rows) == 3


def test_all_ready_means_full_agreement() -> None:
    fake, listing = world()
    for n in (5, 4, 3):
        add_pr(fake, listing, n, merged="2026-10-06T10:00:00Z", updated="2026-10-06T10:00:00Z")
    report = replay(fake, TOY, REPO, now=NOW, org="example", lookup_membership=False)
    assert report.agreement == 1.0 and not report.disagreements and not report.unexplained
    assert all(r.state is State.READY_TO_ENQUEUE for r in report.rows)
    assert "UNEXPLAINED: 0" in render(report)


def test_a_merged_pr_the_planner_calls_blocked_is_an_unexplained_disagreement() -> None:
    fake, listing = world()
    add_pr(fake, listing, 9, merged="2026-10-06T10:00:00Z", updated="2026-10-06T10:00:00Z")
    add_pr(fake, listing, 8, merged="2026-10-06T09:00:00Z", updated="2026-10-06T09:00:00Z", labels=())
    report = replay(fake, TOY, REPO, now=NOW, org="example", lookup_membership=False)
    assert report.agreement == 0.5
    assert [r.number for r in report.unexplained] == [8]
    assert report.rows[1].state is State.AWAITING_APPROVAL
    assert "| #8 | awaiting-approval |" in render(report) and "**no**" in render(report)


def test_a_signed_off_explanation_removes_it_from_the_unexplained_list() -> None:
    fake, listing = world()
    add_pr(fake, listing, 8, merged="2026-10-06T09:00:00Z", updated="2026-10-06T09:00:00Z", lint="failure")
    report = replay(
        fake, TOY, REPO, now=NOW, org="example", lookup_membership=False, explained={8: "lint was waived, see issue"}
    )
    assert len(report.disagreements) == 1 and not report.unexplained
    assert "lint was waived" in render(report)


def test_explanations_for_prs_that_agree_are_not_counted() -> None:
    fake, listing = world()
    add_pr(fake, listing, 8, merged="2026-10-06T09:00:00Z", updated="2026-10-06T09:00:00Z")
    report = replay(fake, TOY, REPO, now=NOW, org="example", lookup_membership=False, explained={8: "stale entry"})
    assert not report.disagreements and report.rows[0].explanation is None


def test_json_output_lists_unexplained_numbers() -> None:
    fake, listing = world()
    add_pr(fake, listing, 8, merged="2026-10-06T09:00:00Z", updated="2026-10-06T09:00:00Z", labels=())
    data = json.loads(to_json(replay(fake, TOY, REPO, now=NOW, org="example", lookup_membership=False)))
    assert data["unexplained"] == [8] and data["replayed"] == 1 and data["rows"][0]["state"] == "awaiting-approval"


def test_load_explained(tmp_path: Path) -> None:
    good = tmp_path / "e.yaml"
    good.write_text("1438: waived by infra\n")
    assert load_explained(good) == {1438: "waived by infra"}
    assert load_explained(None) == {}
    empty = tmp_path / "empty.yaml"
    empty.write_text("{}\n")
    assert load_explained(empty) == {}
    for bad in ("- a list\n", "1: ''\n", "2: 5\n"):
        (tmp_path / "bad.yaml").write_text(bad)
        with pytest.raises(ValueError, match="mapping of PR number"):
            load_explained(tmp_path / "bad.yaml")


def test_the_committed_ledger_is_valid_and_empty() -> None:
    assert load_explained(ROOT / "parity" / "explained.yaml") == {}


# the command: exit 0 when everything agrees or is explained, 1 when anything is unexplained -----------------


def run_cli(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, *extra: str) -> int:
    monkeypatch.setattr(cli, "build_client", lambda: fake)
    monkeypatch.setattr(cli, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    return cli.main(
        ["replay", "--policy", str(ROOT / "policy" / "toy.yml"), "--repo", REPO, "--org", "example",
         "--no-membership-lookup", *extra]
    )  # fmt: skip


def test_cli_exits_0_when_all_agree(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake, listing = world()
    add_pr(fake, listing, 5, merged="2026-10-06T10:00:00Z", updated="2026-10-06T10:00:00Z")
    assert run_cli(monkeypatch, fake) == 0
    assert "planner agrees" in capsys.readouterr().out


def test_cli_exits_1_on_an_unexplained_disagreement_and_0_once_explained(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake, listing = world()
    add_pr(fake, listing, 8, merged="2026-10-06T09:00:00Z", updated="2026-10-06T09:00:00Z", labels=())
    assert run_cli(monkeypatch, fake) == 1
    ledger = tmp_path / "explained.yaml"
    ledger.write_text("8: approved label was applied after merge\n")
    assert run_cli(monkeypatch, fake, "--explained", str(ledger)) == 0


def test_cli_json_and_github_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake, listing = world()
    add_pr(fake, listing, 5, merged="2026-10-06T10:00:00Z", updated="2026-10-06T10:00:00Z")
    assert run_cli(monkeypatch, fake, "--json") == 0
    assert json.loads(capsys.readouterr().out)["replayed"] == 1
    fake.add("GET", f"{BASE}/pulls/5", {"message": "Bad Gateway"}, status=502)
    assert run_cli(monkeypatch, fake) == 3
    assert "replay failed" in capsys.readouterr().err
