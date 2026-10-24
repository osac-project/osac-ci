"""The auto-merge action: when the app turns auto-merge on, and when a person's "no" is respected. Its shell runs with
a stand-in `gh`."""

import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from helpers import ROOT

pytestmark = pytest.mark.unit
ACTION = ROOT / ".github" / "actions" / "auto-merge" / "action.yml"
DECIDE = ROOT / ".github" / "workflows" / "e2e-pr-decide.yml"
HEAD = "a" * 40
OLD = "b" * 40
APPROVED_AT = "2026-10-20T10:00:00Z"
BEFORE = "2026-10-20T09:00:00Z"
AFTER = "2026-10-20T11:00:00Z"

# A stand-in for `gh` that logs every call and answers from the environment: GH_VIEW (the pull request), GH_REVIEWS (its
# reviews, as `--slurp` gives them: a list of pages), GH_TIMELINE (the GraphQL answer) and GH_MERGE_EXIT.
GH = r"""#!/bin/bash
args="$*"
echo "${args//$'\n'/ }" >> "$GH_LOG"
case "$args" in
  "pr view "*) printf '%s' "$GH_VIEW" ;;
  *"/reviews"*) printf '%s' "$GH_REVIEWS" ;;
  "api graphql "*) printf '%s' "$GH_TIMELINE" ;;
  "pr merge "*) exit "${GH_MERGE_EXIT:-0}" ;;
  *) exit 1 ;;
esac
"""


def load() -> dict:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def script() -> str:
    return next(s["run"] for s in load()["runs"]["steps"] if s["name"] == "Enable auto-merge")


def view(**changes: Any) -> str:
    return json.dumps({"autoMergeRequest": None, "isDraft": False, "reviewDecision": "APPROVED", **changes})


def reviews(*items: tuple[str, str, str]) -> str:
    return json.dumps([[{"state": s, "commit_id": c, "submitted_at": at} for s, c, at in items]])


def timeline(*events: tuple[str, str, str]) -> str:
    nodes = [{"__typename": t, "createdAt": at, "reason": reason} for t, at, reason in events]
    return json.dumps({"data": {"repository": {"pullRequest": {"timelineItems": {"nodes": nodes}}}}})


def removed(at: str, reason: str) -> tuple[str, str, str]:
    return ("RemovedFromMergeQueueEvent", at, reason)


def disabled(at: str, reason: str) -> tuple[str, str, str]:
    return ("AutoMergeDisabledEvent", at, reason)


def run(tmp_path: Path, **given: str):  # type: ignore[no-untyped-def]
    """Run the step; returns (exit code, the calls made to gh, the output)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_LOG": str(tmp_path / "calls"), "GH_TOKEN": "t", "REPO": "o/r",
        "NUMBER": "7", "HEAD_SHA": HEAD, "GH_VIEW": view(), "GH_REVIEWS": reviews(("APPROVED", HEAD, APPROVED_AT)),
        "GH_TIMELINE": timeline(),
    }  # fmt: skip
    env.update(given)
    done = subprocess.run(["bash", "-c", script()], env=env, capture_output=True, text=True, check=False)
    log = tmp_path / "calls"
    return done.returncode, (log.read_text().splitlines() if log.exists() else []), done.stdout + done.stderr


def merged(calls: list[str]) -> bool:
    return any(c.startswith("pr merge ") for c in calls)


def test_every_input_used_is_declared_and_every_input_declared_is_used() -> None:
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", ACTION.read_text(encoding="utf-8")))
    assert used == set(load()["inputs"])


def test_nothing_is_expanded_into_the_script_and_nothing_is_checked_out() -> None:
    for step in load()["runs"]["steps"]:
        assert "${{" not in step.get("run", "")
        assert "checkout" not in step.get("uses", "")


def test_an_approved_pull_request_that_nobody_said_no_to_gets_auto_merge_for_the_decided_commit(tmp_path: Path) -> None:
    code, calls, _ = run(tmp_path)
    assert code == 0
    assert f"pr merge 7 --repo o/r --auto --match-head-commit {HEAD}" in calls


def test_the_question_asks_for_both_kinds_of_event_of_this_pull_request(tmp_path: Path) -> None:
    _, calls, _ = run(tmp_path)
    graphql = next(c for c in calls if c.startswith("api graphql "))
    assert "AUTO_MERGE_DISABLED_EVENT" in graphql and "REMOVED_FROM_MERGE_QUEUE_EVENT" in graphql
    assert "-f owner=o -f name=r -F number=7" in graphql


@pytest.mark.parametrize(
    ("given", "why"),
    [
        ({"GH_VIEW": view(autoMergeRequest={"mergeMethod": "SQUASH"})}, "has auto-merge already"),
        ({"GH_VIEW": view(isDraft=True)}, "or is a draft"),
        ({"GH_VIEW": view(reviewDecision="CHANGES_REQUESTED")}, "Changes are requested"),
    ],
)
def test_nothing_is_turned_on_when_it_is_on_already_a_draft_or_changes_are_requested(
    tmp_path: Path, given: dict[str, str], why: str
) -> None:
    code, calls, out = run(tmp_path, **given)
    assert code == 0 and why in out and not merged(calls)


@pytest.mark.parametrize(
    "event",
    [
        removed(AFTER, "manual"),
        disabled(AFTER, "Manually disabled by user"),
        disabled(AFTER, "something GitHub says in the future"),
    ],
)
def test_a_person_s_no_after_the_approval_is_respected(tmp_path: Path, event: tuple[str, str, str]) -> None:
    code, calls, out = run(tmp_path, GH_TIMELINE=timeline(event))
    assert code == 0 and "leaving it off" in out and not merged(calls)


@pytest.mark.parametrize(
    "event",
    [
        removed(AFTER, "manual"),  # taken out of the queue
        disabled(AFTER, "Manually disabled by user"),
    ],
)
def test_a_no_from_before_the_approval_is_not_held_against_the_new_approval(
    tmp_path: Path, event: tuple[str, str, str]
) -> None:
    before = (event[0], BEFORE, event[2])
    _, calls, _ = run(tmp_path, GH_TIMELINE=timeline(before))
    assert merged(calls)


@pytest.mark.parametrize(
    "event",
    [
        disabled(AFTER, "Pull request was closed"),
        disabled(AFTER, "Head branch was pushed to by a user without write access"),
        removed(AFTER, "merged"),
        removed(AFTER, "failed_checks"),
        removed(AFTER, "checks_timed_out"),
        removed(AFTER, "merge_conflict"),
    ],
)
def test_what_github_did_on_its_own_is_not_a_no(tmp_path: Path, event: tuple[str, str, str]) -> None:
    _, calls, _ = run(tmp_path, GH_TIMELINE=timeline(event))
    assert merged(calls)


def test_the_newest_no_and_the_newest_approval_of_the_head_commit_decide(tmp_path: Path) -> None:
    twice = timeline(removed(BEFORE, "manual"), disabled(AFTER, "Manually disabled by user"))
    assert not merged(run(tmp_path, GH_TIMELINE=twice)[1])
    other = tmp_path / "other"
    other.mkdir()
    newer = reviews(
        ("APPROVED", HEAD, APPROVED_AT),
        ("APPROVED", HEAD, "2026-10-20T12:00:00Z"),
        ("APPROVED", OLD, "2026-10-20T13:00:00Z"),
    )
    assert merged(run(other, GH_TIMELINE=twice, GH_REVIEWS=newer)[1]), "a newer approval of the head commit"


@pytest.mark.parametrize(
    "given",
    [
        {"GH_REVIEWS": reviews()},
        {"GH_REVIEWS": reviews(("APPROVED", OLD, "2026-10-20T13:00:00Z"))},  # an approval of another commit is none
        {"GH_REVIEWS": reviews(("CHANGES_REQUESTED", HEAD, APPROVED_AT), ("COMMENTED", HEAD, APPROVED_AT))},
        {"GH_REVIEWS": reviews(("DISMISSED", HEAD, APPROVED_AT))},
    ],
)
def test_without_an_approval_of_the_head_commit_nothing_is_turned_on_and_the_timeline_is_not_read(
    tmp_path: Path, given: dict[str, str]
) -> None:
    code, calls, out = run(tmp_path, **given)
    assert code == 0 and f"has no approval of {HEAD}" in out
    assert not merged(calls) and not any(c.startswith("api graphql ") for c in calls)


def test_a_failed_merge_is_a_warning_and_not_an_error(tmp_path: Path) -> None:
    code, calls, out = run(tmp_path, GH_MERGE_EXIT="1")
    assert code == 0 and merged(calls) and "::warning::Could not enable auto-merge on PR #7" in out


@pytest.mark.parametrize(
    "given", [{"NUMBER": "7; id"}, {"NUMBER": ""}, {"HEAD_SHA": "xyz"}, {"HEAD_SHA": HEAD.upper()}, {"HEAD_SHA": ""}]
)
def test_a_value_that_is_not_what_it_should_be_stops_the_step_before_any_call(
    tmp_path: Path, given: dict[str, str]
) -> None:
    code, calls, _ = run(tmp_path, **given)
    assert code == 1 and calls == []


def test_the_decision_workflow_uses_the_action_right_after_the_app_token() -> None:
    job = yaml.safe_load(DECIDE.read_text(encoding="utf-8"))["jobs"]["auto-merge"]
    token, enable = job["steps"][:2]
    assert token["id"] == "app-token" and job["if"] == "needs.decide.outputs.approved == 'true'"
    assert enable["uses"] == "$/.github/actions/auto-merge" and "run" not in enable
    assert enable["with"] == {
        "github-token": "${{ steps.app-token.outputs.token }}",
        "repo": "${{ github.repository }}",
        "number": "${{ needs.decide.outputs.approved-number }}",
        "head-sha": "${{ needs.decide.outputs.approved-sha }}",
    }
    assert job["permissions"] == {"pull-requests": "write"}, "only for the quiet edit of the comment"
