"""Decision-time replay: judge each merged PR as it stood when it was enqueued (or merged)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from fakes import REPO, FakeGitHub
from helpers import ROOT

from osac_ci import cli
from osac_ci.github.snapshot import queue_entry_time, queued_at_end
from osac_ci.model import State
from osac_ci.policy import load_policy
from osac_ci.replay import render, replay, to_json

pytestmark = pytest.mark.contract
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
TOY = load_policy(ROOT / "policy" / "toy.yml")  # required label: approved; checks lint and test; blocking label: wip
BASE = f"/repos/{REPO}"
ENQUEUED = "2026-10-06T10:00:00Z"
MERGED = "2026-10-06T10:30:00Z"


def ev(kind: str, at: str, label: str | None = None, actor: str = "bot") -> dict[str, Any]:
    out: dict[str, Any] = {"event": kind, "created_at": at, "actor": {"login": actor}}
    if label:
        out["label"] = {"name": label}
    return out


def run(name: str, started: str, completed: str | None, conclusion: str = "success") -> dict[str, Any]:
    return {
        "name": name,
        "status": "completed" if completed else "in_progress",
        "conclusion": conclusion if completed else None,
        "started_at": started,
        "completed_at": completed,
    }


def world(
    events: list[dict[str, Any]], runs: list[dict[str, Any]], labels: tuple[str, ...] = ("approved",)
) -> FakeGitHub:
    fake = FakeGitHub()
    sha = "5" * 40
    fake.add("POST", "/graphql", {"data": {"node": {"mergeQueueEntry": None}}})
    fake.routes[("GET", f"{BASE}/pulls")] = (200, [{"number": 5, "merged_at": MERGED, "updated_at": MERGED}])
    fake.add(
        "GET",
        f"{BASE}/pulls/5",
        {
            "number": 5, "node_id": "PR_5", "draft": False, "user": {"login": "alice"}, "author_association": "MEMBER",
            "labels": [{"name": n} for n in labels],
            "head": {"sha": sha, "repo": {"full_name": REPO, "owner": {"login": "example"}}},
        },
    )  # fmt: skip
    fake.add("GET", f"{BASE}/pulls/5/reviews", [])
    fake.add("GET", f"{BASE}/issues/5/events", events)
    fake.add("GET", f"{BASE}/pulls/5/files", [{"filename": "a.go"}])
    fake.add("GET", f"{BASE}/commits/{sha}/check-runs", {"total_count": len(runs), "check_runs": runs})
    return fake


GREEN = [
    run("lint", "2026-10-06T09:00:00Z", "2026-10-06T09:10:00Z"),
    run("test", "2026-10-06T09:00:00Z", "2026-10-06T09:20:00Z"),
]
QUEUED = [ev("added_to_merge_queue", ENQUEUED), ev("merged", MERGED)]


def one(
    events: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    at: str = "enqueue",
    labels: tuple[str, ...] = ("approved",),
):  # type: ignore[no-untyped-def]
    (row,) = replay(world(events, runs, labels), TOY, REPO, now=NOW, org="example", at=at).rows
    return row


# ---- the moment of the queue entry ---------------------------------------------------------------


def test_the_entry_time_is_when_the_surviving_queue_entry_was_created() -> None:
    assert queue_entry_time([ev("added_to_merge_queue", ENQUEUED), ev("merged", MERGED)]) == ENQUEUED


def test_the_last_entry_counts_when_a_pr_was_queued_twice() -> None:
    events = [
        ev("added_to_merge_queue", "2026-10-06T08:00:00Z"),
        ev("removed_from_merge_queue", "2026-10-06T08:30:00Z", actor="someone"),
        ev("added_to_merge_queue", ENQUEUED),
        ev("merged", MERGED),
    ]
    assert queue_entry_time(events) == ENQUEUED


def test_the_queues_own_cleanup_right_after_the_merge_does_not_cancel_the_entry() -> None:
    events = [
        ev("added_to_merge_queue", ENQUEUED),
        ev("removed_from_merge_queue", MERGED, actor="github-merge-queue[bot]"),
        ev("merged", MERGED),
    ]
    assert queue_entry_time(events) == ENQUEUED and queued_at_end(events)


@pytest.mark.parametrize(
    "events",
    [
        [ev("merged", MERGED)],  # never queued
        [
            ev("added_to_merge_queue", ENQUEUED),
            ev("removed_from_merge_queue", "2026-10-06T10:10:00Z", actor="human"),
            ev("merged", MERGED),
        ],
        [
            ev("added_to_merge_queue", ENQUEUED),
            ev("head_ref_force_pushed", "2026-10-06T10:10:00Z"),
            ev("merged", MERGED),
        ],
        [],
    ],
)
def test_no_entry_time_when_the_pr_was_not_queue_merged(events: list[dict[str, Any]]) -> None:
    assert queue_entry_time(events) == ""


# ---- judging a queue-merged PR at the moment it was enqueued -------------------------------------


def test_a_queue_merged_pr_whose_label_was_there_at_the_enqueue_agrees() -> None:
    events = [ev("labeled", "2026-10-06T09:30:00Z", "approved"), *QUEUED]
    row = one(events, GREEN)
    assert row.agrees and row.via_queue and row.decision_at == ENQUEUED and row.state is State.READY_TO_ENQUEUE


def test_a_label_applied_after_the_enqueue_is_not_credited_to_the_decision() -> None:
    events = [
        ev("added_to_merge_queue", ENQUEUED),
        ev("labeled", "2026-10-06T10:10:00Z", "approved"),
        ev("merged", MERGED),
    ]
    final = one(events, GREEN, at="final")
    at_enqueue = one(events, GREEN)
    assert final.agrees and final.state is State.READY_TO_ENQUEUE  # today's data says fine
    assert not at_enqueue.agrees and "missing label: approved" in at_enqueue.headline  # but it was not, when it counted


def test_a_blocking_label_present_at_the_enqueue_disagrees_even_if_removed_later() -> None:
    events = [
        ev("labeled", "2026-10-06T09:00:00Z", "approved"),
        ev("labeled", "2026-10-06T09:05:00Z", "wip"),
        ev("added_to_merge_queue", ENQUEUED),
        ev("unlabeled", "2026-10-06T10:10:00Z", "wip"),
        ev("merged", MERGED),
    ]
    assert not one(events, GREEN, labels=("approved",)).agrees


def test_checks_that_were_not_done_at_the_enqueue_do_not_make_it_disagree_but_are_reported() -> None:
    events = [ev("labeled", "2026-10-06T09:30:00Z", "approved"), *QUEUED]
    slow = [
        run("lint", "2026-10-06T09:00:00Z", "2026-10-06T09:10:00Z"),
        run("test", "2026-10-06T09:00:00Z", "2026-10-06T10:20:00Z"),
    ]
    row = one(events, slow)
    assert row.agrees  # today's enqueue step never reads checks
    assert row.state is State.CHECKS_RUNNING  # the planner would have held it
    final = one(events, slow, at="final")
    assert final.state is State.READY_TO_ENQUEUE  # on today's data it looks perfectly ready


def test_a_check_that_failed_at_the_enqueue_is_reported_as_failed() -> None:
    events = [ev("labeled", "2026-10-06T09:30:00Z", "approved"), *QUEUED]
    failed = [run("lint", "2026-10-06T09:00:00Z", "2026-10-06T09:10:00Z", "failure"), GREEN[1]]
    row = one(events, failed)
    assert row.agrees and row.state is State.CHECKS_FAILED


def test_a_disagreement_can_be_explained_in_the_ledger() -> None:
    events = [ev("added_to_merge_queue", ENQUEUED), ev("merged", MERGED)]
    (row,) = replay(
        world(events, GREEN), TOY, REPO, now=NOW, org="example", at="enqueue", explained={5: "hotfix, signed off"}
    ).rows
    assert not row.agrees and row.explanation == "hotfix, signed off" and not row.unexplained


# ---- a direct merge is judged at the merge -------------------------------------------------------


def test_a_direct_merge_is_judged_when_it_merged_with_the_full_verdict() -> None:
    events = [ev("labeled", "2026-10-06T09:30:00Z", "approved"), ev("merged", MERGED)]
    still_running = [
        run("lint", "2026-10-06T09:00:00Z", "2026-10-06T09:10:00Z"),
        run("test", "2026-10-06T09:00:00Z", "2026-10-06T10:45:00Z"),
    ]
    row = one(events, still_running)
    assert not row.via_queue and row.decision_at == MERGED
    assert not row.agrees and row.state is State.CHECKS_RUNNING  # it merged while a required check was still running
    assert one(events, still_running, at="final").agrees  # which today's data hides


# ---- the report and the command line -------------------------------------------------------------


def test_final_is_the_default_and_nothing_about_it_changed() -> None:
    events = [ev("labeled", "2026-10-06T09:30:00Z", "approved"), *QUEUED]
    report = replay(world(events, GREEN), TOY, REPO, now=NOW, org="example")
    assert report.at == "final" and report.rows[0].decision_at == "" and "Reads each PR's final state" in render(report)


def test_the_enqueue_report_explains_itself_and_counts_what_the_planner_would_have_held_back() -> None:
    events = [ev("labeled", "2026-10-06T09:30:00Z", "approved"), *QUEUED]
    slow = [
        run("lint", "2026-10-06T09:00:00Z", "2026-10-06T09:10:00Z"),
        run("test", "2026-10-06T09:00:00Z", "2026-10-06T10:20:00Z"),
    ]
    report = replay(world(events, slow), TOY, REPO, now=NOW, org="example", at="enqueue")
    text = render(report)
    assert "judged as it stood when it was enqueued" in text and "that step never reads check results" in text
    assert "would also have held back (checks or E2E not ready then): 1" in text
    data = json.loads(to_json(report))
    assert data["at"] == "enqueue" and data["rows"][0]["decision_at"] == ENQUEUED


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="at must be"):
        replay(world(QUEUED, GREEN), TOY, REPO, now=NOW, org="example", at="merge")


def test_cli_replay_takes_an_at_flag(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        cli, "build_client", lambda: world([ev("labeled", "2026-10-06T09:30:00Z", "approved"), *QUEUED], GREEN)
    )
    monkeypatch.setattr(cli, "datetime", type("D", (), {"now": staticmethod(lambda tz=None: NOW)}))
    code = cli.main(
        ["replay", "--policy", "policy/toy.yml", "--repo", REPO, "--org", "example", "--at", "enqueue", "--json"]
    )
    assert code == 0 and json.loads(capsys.readouterr().out)["at"] == "enqueue"
