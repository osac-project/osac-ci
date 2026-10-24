"""Cancelling the E2E run of an older commit of a pull request, found by the ``E2E`` line of that commit."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from helpers import ROOT

from osac_ci import cancel_older, cli
from osac_ci.github.api import GitHubError, Response

pytestmark = pytest.mark.unit
REPO = "o/r"
BASE = f"/repos/{REPO}"
HEAD = "a" * 40
C1 = "1" * 40
C2 = "2" * 40
C3 = "3" * 40
ACTION = ROOT / ".github" / "actions" / "cancel-older" / "action.yml"
DECIDE = ROOT / ".github" / "workflows" / "e2e-pr-decide.yml"


class Hub:
    """GitHub with the commits of one pull request, the E2E line of each commit and the other pull requests."""

    def __init__(self, commits: list[str]) -> None:
        self.commits = commits
        self.lines: dict[str, list[dict[str, Any]]] = {}  # commit -> its E2E check runs
        self.pulls: dict[str, list[dict[str, Any]]] = {}  # commit -> pull requests that have it
        self.cancel_answer: dict[int, int] = {}
        self.cancelled: list[str] = []
        self.dispatched: list[dict[str, Any]] = []  # runs started by workflow_dispatch: id, title, status

    def line(self, sha: str, run: int | None, description: str = "Running: vmaas", status: str = "in_progress",
             app: str = "osac-ci") -> None:  # fmt: skip
        url = f"https://github.com/o/r/actions/runs/{run}" if run is not None else ""
        self.lines.setdefault(sha, []).append(
            {
                "id": len(self.lines.get(sha, [])) + 1,
                "name": "E2E",
                "status": status,
                "conclusion": "success" if status == "completed" else None,
                "details_url": url,
                "output": {"title": description},
                "app": {"slug": app},
            }  # fmt: skip
        )

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        page, per = int((params or {}).get("page", 1)), int((params or {}).get("per_page", 100))
        if method == "GET" and path == f"{BASE}/pulls/7/commits":
            items = [{"sha": s} for s in self.commits]
            return Response(200, items[(page - 1) * per : page * per])
        if method == "GET" and (m := re.fullmatch(rf"{BASE}/commits/([0-9a-f]{{40}})/check-runs", path)):
            return Response(200, {"check_runs": self.lines.get(m.group(1), [])})
        if method == "GET" and (m := re.fullmatch(rf"{BASE}/commits/([0-9a-f]{{40}})/pulls", path)):
            return Response(200, self.pulls.get(m.group(1), []))
        if method == "GET" and path == f"{BASE}/actions/runs":
            assert (params or {}).get("event") == "workflow_dispatch"
            mine = [r for r in self.dispatched if r["status"] == (params or {}).get("status")]
            return Response(200, {"workflow_runs": mine[(page - 1) * per : page * per]})
        if method == "POST" and (m := re.fullmatch(rf"{BASE}/actions/runs/(\d+)/cancel", path)):
            run = int(m.group(1))
            self.cancelled.append(m.group(1))
            return Response(self.cancel_answer.get(run, 202), {})
        return Response(404, {"message": f"no route for {method} {path}"})


def go(hub: Hub, head: str = HEAD) -> list[tuple[str, int]]:
    return cancel_older.cancel_older(hub, REPO, 7, head)  # type: ignore[arg-type]


def test_the_run_that_is_running_the_e2e_of_an_older_commit_is_cancelled() -> None:
    hub = Hub([C1, C2, HEAD])
    hub.line(C1, 11)
    hub.line(HEAD, 99)  # the head's own run is never looked at
    assert go(hub) == [(C1, 11)]
    assert hub.cancelled == ["11"]


def test_every_older_commit_with_a_run_is_cancelled_newest_first() -> None:
    hub = Hub([C1, C2, C3, HEAD])
    hub.line(C1, 11)
    hub.line(C3, 13)
    assert go(hub) == [(C3, 13), (C1, 11)]


@pytest.mark.parametrize(
    ("description", "status"),
    [
        ("Waiting: approval needed", "in_progress"),  # nothing runs: a short decision run at most
        ("Running: vmaas", "completed"),  # a finished line is never a run to cancel, whatever it says
        ("Passed: vmaas", "completed"),
        ("Failed: vmaas", "completed"),
        ("Not affected by this change", "completed"),
    ],
)
def test_a_commit_whose_e2e_is_not_running_has_nothing_to_cancel(description: str, status: str) -> None:
    hub = Hub([C1, HEAD])
    hub.line(C1, 11, description, status)
    assert go(hub) == [] and hub.cancelled == []


def test_a_commit_with_no_e2e_line_or_no_run_in_its_link_is_left_alone() -> None:
    hub = Hub([C1, C2, HEAD])
    hub.line(C2, None)  # a line with no link
    assert go(hub) == [] and hub.cancelled == []


def test_only_the_line_of_the_app_counts_and_the_newest_of_a_commit_wins() -> None:
    hub = Hub([C1, C2, HEAD])
    hub.line(C1, 11, app="someone-else")  # anyone can write a check of that name
    hub.line(C2, 12)
    hub.line(C2, 22, "Passed: vmaas", "completed")  # newer: the E2E of C2 is over
    assert go(hub) == [] and hub.cancelled == []


def test_a_commit_that_belongs_to_another_open_pull_request_is_never_cancelled() -> None:
    hub = Hub([C1, C2, HEAD])
    hub.line(C1, 11)
    hub.line(C2, 12)
    hub.pulls[C1] = [{"number": 7, "state": "open"}, {"number": 8, "state": "open"}]
    hub.pulls[C2] = [{"number": 7, "state": "open"}, {"number": 9, "state": "closed"}]  # a closed one does not count
    assert go(hub) == [(C2, 12)]


def test_a_pull_request_that_has_only_the_head_commit_asks_for_nothing_else() -> None:
    hub = Hub([HEAD])
    assert go(hub) == [] and hub.cancelled == []


def test_only_the_last_older_commits_are_looked_at() -> None:
    many = [f"{i:040x}" for i in range(1, 16)]
    hub = Hub([*many, HEAD])
    for sha in many:
        hub.line(sha, int(sha, 16) + 100)
    done = go(hub)
    assert len(done) == cancel_older.MAX_OLDER
    assert [s for s, _ in done][0] == many[-1] and many[0] not in [s for s, _ in done]


@pytest.mark.parametrize("answer", [202, 404, 409])
def test_a_run_that_is_gone_or_already_finished_is_not_an_error(answer: int) -> None:
    hub = Hub([C1, HEAD])
    hub.line(C1, 11)
    hub.cancel_answer[11] = answer
    assert go(hub) == [(C1, 11)]


def test_any_other_answer_to_the_cancel_is_an_error() -> None:
    hub = Hub([C1, HEAD])
    hub.line(C1, 11)
    hub.cancel_answer[11] = 403
    with pytest.raises(GitHubError):
        go(hub)


def test_a_bad_repository_or_commit_is_refused_before_any_call() -> None:
    hub = Hub([C1, HEAD])
    with pytest.raises(ValueError):
        cancel_older.cancel_older(hub, "not a repo", 7, HEAD)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        cancel_older.cancel_older(hub, REPO, 7, "xyz")  # type: ignore[arg-type]


def test_the_command_says_what_it_cancelled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = Hub([C1, HEAD])
    hub.line(C1, 11)
    monkeypatch.setattr(cli, "build_client", lambda **_: hub)
    argv = ["cancel-older", "--repo", REPO, "--number", "7", "--head-sha", HEAD]
    assert cli.main(argv) == 0
    assert f"Cancelling run 11, which is running the E2E of the older commit {C1[:7]}" in capsys.readouterr().out
    quiet = Hub([HEAD])
    monkeypatch.setattr(cli, "build_client", lambda **_: quiet)
    assert cli.main(argv) == 0 and "No older commit has an E2E running." in capsys.readouterr().out


def test_the_command_fails_with_3_and_says_why(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = Hub([C1, HEAD])
    hub.line(C1, 11)
    hub.cancel_answer[11] = 500
    monkeypatch.setattr(cli, "build_client", lambda **_: hub)
    assert cli.main(["cancel-older", "--repo", REPO, "--number", "7", "--head-sha", HEAD]) == 3
    assert "cancel-older failed" in capsys.readouterr().err
    assert cli.main(["cancel-older", "--repo", REPO, "--number", "7", "--head-sha", "xyz"]) == 3


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_every_input_used_is_declared_and_every_input_declared_is_used() -> None:
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", ACTION.read_text(encoding="utf-8")))
    assert used == set(load(ACTION)["inputs"])


def test_nothing_is_expanded_into_a_script_and_nothing_is_checked_out() -> None:
    for step in load(ACTION)["runs"]["steps"]:
        assert "${{" not in step.get("run", "")
        assert "checkout" not in step.get("uses", "")


def test_the_action_runs_the_locked_project_of_the_pinned_commit_with_a_number_of_digits_only() -> None:
    script = next(s["run"] for s in load(ACTION)["runs"]["steps"] if s["name"] == "Cancel")
    assert '"" | *[!0-9]*)' in script and "exit 1" in script
    assert (
        'uv run --locked --project "$root" osac-ci cancel-older --repo "$REPO" --number "$NUMBER" --head-sha' in script
    )


def test_the_decision_calls_it_with_the_head_commit_it_decided_on() -> None:
    steps = load(DECIDE)["jobs"]["decide"]["steps"]
    step = next(s for s in steps if s.get("uses") == "$/.github/actions/cancel-older")
    assert step["if"] == "steps.report.outputs.number != '' && steps.by-hand.outputs.manual != 'true'"
    assert step["with"] == {
        "github-token": "${{ github.token }}",
        "repo": "${{ github.repository }}",
        "number": "${{ steps.report.outputs.number }}",
        "head-sha": "${{ steps.report.outputs.sha }}",
    }


def test_a_commit_pushed_while_the_decision_ran_is_a_newer_head_and_its_run_is_not_cancelled() -> None:
    hub = Hub([C1, HEAD, C3])  # C3 landed after this decision looked at the pull request
    hub.line(C1, 11)
    hub.line(C3, 13)  # the decision of C3 has already started its E2E
    assert go(hub) == [(C1, 11)] and hub.cancelled == ["11"]


def test_a_head_that_is_no_longer_a_commit_of_the_pull_request_cancels_nothing() -> None:
    hub = Hub([C1, C2, C3])  # a force push replaced the history
    hub.line(C1, 11)
    hub.line(C3, 13)
    assert go(hub) == [] and hub.cancelled == []


def suite_run(hub: Hub, run: int, sha: str, number: int = 7, suite: str = "vmaas", status: str = "in_progress") -> None:
    hub.dispatched.append(
        {"id": run, "display_title": f"E2E suite · {suite} · #{number} · {sha}", "status": status}
    )  # fmt: skip


def test_the_suites_started_as_runs_of_their_own_for_an_older_commit_are_cancelled_while_they_have_not_ended() -> None:
    hub = Hub([C1, C2, HEAD])
    suite_run(hub, 31, C1)
    suite_run(hub, 32, C2, suite="caas", status="queued")
    suite_run(hub, 33, HEAD)  # the head's own suite is never looked at
    assert sorted(go(hub)) == sorted([(C1, 31), (C2, 32)]) and sorted(hub.cancelled) == ["31", "32"]


def test_a_suite_run_of_another_pull_request_or_another_title_is_left_alone() -> None:
    hub = Hub([C1, HEAD])
    suite_run(hub, 41, C1, number=8)  # the same commit but another pull request's run
    hub.dispatched.append({"id": 42, "display_title": f"Some run · {C1}", "status": "in_progress"})
    hub.dispatched.append({"id": 43, "display_title": f"E2E suite · vmaas · #7 · {'f' * 40}", "status": "in_progress"})
    assert go(hub) == [] and hub.cancelled == []


def test_a_suite_run_of_a_commit_that_belongs_to_another_open_pull_request_is_not_cancelled() -> None:
    hub = Hub([C1, HEAD])
    suite_run(hub, 51, C1)
    hub.pulls[C1] = [{"number": 7, "state": "open"}, {"number": 8, "state": "open"}]
    assert go(hub) == [] and hub.cancelled == []


def test_a_suite_run_that_finished_by_the_time_it_is_cancelled_is_not_an_error() -> None:
    hub = Hub([C1, HEAD])
    suite_run(hub, 61, C1)
    hub.cancel_answer[61] = 409
    assert go(hub) == [(C1, 61)]
