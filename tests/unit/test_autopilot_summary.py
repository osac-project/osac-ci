"""The CI Autopilot check (the app's own summary of every line), and the removal of the entry runs that add rows."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from osac_ci import autopilot_summary, clean_runs, cli
from osac_ci.github.api import Response
from osac_ci.policy import parse_policy

pytestmark = pytest.mark.unit
HEAD = "a" * 40
NOW = datetime(2026, 10, 28, 12, 0, 5, tzinfo=UTC)
POLICY = parse_policy("""
version: 1
repo: o/r
path_filters: {mode: enforce, filters: {code: ["**/*.go"]}}
jobs:
  lint: {kind: cheap, check: "CI Autopilot / CI / Lint", workflow: ci-lint.yml}
  unit: {kind: cheap, check: "CI Autopilot / CI / Unit", workflow: ci-unit.yml}
  e2e-alpha: {kind: e2e, check: "CI Autopilot / E2E / Alpha Stub", suite: alpha, workflow: e2e-alpha.yml}
  e2e-gamma: {kind: e2e, check: "CI Autopilot / E2E / Gamma Stub", suite: gamma, manual_only: true}
""")


class Gh:
    def __init__(self, state: str = "open") -> None:
        self.state = state
        self.checks: list[dict[str, Any]] = []
        self.writes: list[tuple[str, str, Any]] = []

    def line(self, name: str, status: str, conclusion: str | None, title: str, app: str = "osac-ci") -> None:
        self.checks.append(
            {"id": len(self.checks) + 1, "name": name, "status": status, "conclusion": conclusion, "app": {"slug": app},
             "started_at": f"2026-10-28T11:{len(self.checks):02d}:00Z", "output": {"title": title, "text": ""},
             "details_url": f"https://github.com/o/r/actions/runs/{len(self.checks) + 1}/job/1"}
        )  # fmt: skip

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path == "/repos/o/r/pulls/7":
            return Response(200, {"state": self.state, "head": {"sha": HEAD}}, {})
        if method == "GET" and path.endswith("/check-runs"):
            name = (params or {}).get("check_name")
            return Response(200, {"check_runs": [c for c in self.checks if name in (None, c["name"])]}, {})
        if method == "GET" and path.endswith("/statuses"):
            return Response(200, [], {})
        if method in ("POST", "PATCH") and "/check-runs" in path:
            self.writes.append((method, path, body))
            if method == "POST":
                self.checks.append({**body, "id": len(self.checks) + 1, "app": {"slug": "osac-ci"}})
            else:
                target = next(c for c in self.checks if path.endswith(f"/{c['id']}"))
                target.update({k: v for k, v in body.items() if k != "output"})
                target["output"] = body["output"]
            return Response(200 if method == "PATCH" else 201, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")

    def mine(self) -> dict[str, Any]:
        return [c for c in self.checks if c["name"] == autopilot_summary.NAME][-1]


def update(gh: Gh, event: str, now: datetime = NOW) -> str:
    return autopilot_summary.update(gh, "o/r", 7, POLICY, event, "https://x/run", now)  # type: ignore[arg-type]


def test_the_first_event_creates_the_check_with_the_state_of_every_line_of_the_policy() -> None:
    gh = Gh()
    gh.line("CI Autopilot / CI / Lint", "completed", "success", "Passed")
    gh.line("CI Autopilot / CI / Unit", "in_progress", None, "Running")
    assert update(gh, "started lint, unit") == "created"
    check = gh.mine()
    assert check["status"] == "in_progress" and "conclusion" not in check
    assert (
        "CI Autopilot / CI / Lint" in check["output"]["summary"]
        and "CI Autopilot / E2E / Alpha Stub" in check["output"]["summary"]
    )
    assert "Gamma" not in check["output"]["summary"], "a suite that is only started by hand is not a line of the table"
    assert check["output"]["text"].splitlines()[2] == "- 12:00:05 UTC · started lint, unit"
    assert check["details_url"] == "https://x/run"


def test_a_later_event_updates_the_same_check_and_keeps_the_events_newest_first() -> None:
    gh = Gh()
    update(gh, "started lint, unit", NOW)
    update(gh, "unit failed", NOW.replace(minute=1))
    update(gh, "unit failed", NOW.replace(minute=1))  # the same event again adds nothing
    assert [w[0] for w in gh.writes] == ["POST", "PATCH", "PATCH"]
    events = gh.mine()["output"]["text"].splitlines()[2:]
    assert events == ["- 12:01:05 UTC · unit failed", "- 12:00:05 UTC · started lint, unit"]
    assert len([c for c in gh.checks if c["name"] == autopilot_summary.NAME]) == 1


def test_the_events_are_cut_to_the_last_twelve() -> None:
    gh = Gh()
    for i in range(15):
        update(gh, f"event {i}", NOW.replace(second=i))
    events = gh.mine()["output"]["text"].splitlines()[2:]
    assert len(events) == autopilot_summary.EVENTS_KEPT and events[0].endswith("event 14")


PASSED = [("Lint", "completed", "success"), ("Unit", "completed", "success")]
CASES = [
    (PASSED, ("success", "Passed"), ("success", "Passed: alpha"), "completed", "success"),
    ([PASSED[0], ("Unit", "completed", "failure")], ("failure", "Failed: unit"), None, "completed", "failure"),
    ([PASSED[0], ("Unit", "in_progress", None)], ("in_progress", "Running: unit"), None, "in_progress", None),
    (PASSED, ("success", "Passed"), ("queued", "Waiting for an approval"), "completed", "neutral"),
]


@pytest.mark.parametrize(("lines", "ci", "e2e", "status", "conclusion"), CASES)
def test_the_check_is_running_while_a_job_goes_failed_when_one_failed_and_neutral_while_it_waits(
    lines: list[tuple[str, str, str | None]],
    ci: tuple[str, str],
    e2e: tuple[str, str] | None,
    status: str,
    conclusion: str | None,
) -> None:
    gh = Gh()
    for name, st, concl in lines:
        gh.line(f"CI Autopilot / CI / {name}", st, concl, concl or "Running")
    gh.line(
        "CI", "completed" if ci[0] != "in_progress" else "in_progress", ci[0] if ci[0] != "in_progress" else None, ci[1]
    )
    if e2e:
        gh.line(
            "E2E", "completed" if e2e[0] == "success" else "queued", e2e[0] if e2e[0] == "success" else None, e2e[1]
        )
    update(gh, "x")
    check = gh.mine()
    assert check["status"] == status and check.get("conclusion") == conclusion
    assert ci[1] in check["output"]["title"]


def test_a_closed_pull_request_gets_nothing() -> None:
    gh = Gh(state="closed")
    assert "not open" in update(gh, "x") and gh.writes == []


def test_another_apps_check_of_the_same_name_is_not_the_summary() -> None:
    gh = Gh()
    gh.line(autopilot_summary.NAME, "completed", "success", "x", app="github-actions")
    assert update(gh, "x") == "created", "the app writes its own check, whatever another app named its row"


# the entry runs ------------------------------------------------------------------------------------------------------


class Runs:
    def __init__(self, runs: list[dict[str, Any]]) -> None:
        self.runs, self.deleted = runs, []

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET":
            assert (params or {}).get("event") == "pull_request_target" and (params or {}).get("status") == "completed"
            return Response(200, {"workflow_runs": self.runs}, {})
        self.deleted.append(int(path.rsplit("/", 1)[1]))
        return Response(204, {}, {})


def run(id: int, title: str, conclusion: str = "success") -> dict[str, Any]:
    return {"id": id, "display_title": title, "conclusion": conclusion, "status": "completed"}


def test_the_finished_entry_runs_of_the_pull_request_are_deleted_and_nothing_else() -> None:
    fake = Runs([
        run(1, "Autopilot #7 (synchronize) · b"), run(2, "Autopilot #7 (opened) · b", "skipped"),
        run(3, "Autopilot #8 (synchronize) · b"),  # another pull request
        run(4, "Autopilot #7 (closed) · b", "failure"),  # evidence stays
        run(5, "E2E #7 · b"),  # not an entry run of the pull request event
        run(6, "Autopilot #77 (opened) · b"),  # #77 is not #7
    ])  # fmt: skip
    assert clean_runs.clean_start_runs(fake, "o/r", "autopilot.yml", 7) == [1, 2]  # type: ignore[arg-type]
    assert fake.deleted == [1, 2]


@pytest.mark.parametrize("bad", ["../x.yml", "a b.yml", "x"])
def test_the_entry_workflow_must_be_a_file_name(bad: str) -> None:
    with pytest.raises(ValueError, match="workflow file"):
        clean_runs.clean_start_runs(Runs([]), "o/r", bad, 7)  # type: ignore[arg-type]


def test_the_commands_report_what_they_did(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = Gh()
    monkeypatch.setattr(cli, "build_client", lambda **_: gh)
    monkeypatch.setattr(cli, "load_policy", lambda _p: POLICY)
    argv = ["autopilot-summary", "--policy", "p", "--repo", "o/r", "--number", "7", "--event", "started"]
    assert cli.main(argv) == 0 and "CI Autopilot: created" in capsys.readouterr().out
    fake = Runs([run(1, "Autopilot #7 (opened) · b")])
    monkeypatch.setattr(cli, "build_client", lambda **_: fake)
    assert cli.main(["clean-start-runs", "--repo", "o/r", "--number", "7", "--workflow", "autopilot.yml"]) == 0
    assert "Removed 1 finished run" in capsys.readouterr().out


def test_a_check_that_was_settled_and_goes_again_is_written_as_a_new_run() -> None:
    gh = Gh()
    gh.line("CI Autopilot / CI / Lint", "completed", "success", "Passed")
    gh.line("CI Autopilot / CI / Unit", "completed", "success", "Passed")
    gh.line("CI", "completed", "success", "Passed: lint, unit")
    gh.line("E2E", "queued", None, "Waiting for an approval")
    update(gh, "approved")  # nothing goes: settled (neutral), written as the first run
    assert gh.mine()["status"] == "completed" and gh.mine().get("conclusion") == "neutral"
    gh.line("CI Autopilot / E2E / Alpha Stub", "in_progress", None, "Running")
    update(gh, "alpha started")
    runs = [c for c in gh.checks if c["name"] == autopilot_summary.NAME]
    assert [r["status"] for r in runs] == ["completed", "in_progress"], "GitHub does not reopen a completed run"
    assert [w[0] for w in gh.writes] == ["POST", "POST"]
