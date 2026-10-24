"""Suite lines written as commit statuses: read back as check runs of the app, written by the report, followed by the
tracking, and read by the sum of a run started by hand. Kept by a small in-memory GitHub with both kinds of line."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import pytest

from osac_ci import e2e_check
from osac_ci.e2e_suites_check import settle, track
from osac_ci.github.api import GitHubError, Response
from osac_ci.suite_lines import suite_line_runs, suite_line_runs_if_readable

pytestmark = pytest.mark.unit
REPO = "o/r"
BASE = f"/repos/{REPO}"
SHA = "a" * 40
RUN = 500
RUN_URL = f"https://example.invalid/o/r/actions/runs/{RUN}"
JOB_URL = f"https://example.invalid/o/r/actions/runs/{RUN}/job/9"
VM = "E2E / VMaaS Full Install"
CA = "E2E / CaaS Full Install"
APP = "osac-ci"


class Hub:
    """GitHub with the statuses and the check runs of one commit, the jobs of one run, and what was written."""

    def __init__(self) -> None:
        self.statuses: list[dict[str, Any]] = []  # newest first, as GitHub lists them
        self.check_runs: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.statuses_answer = 200
        self.clock = 0

    def status(self, context: str, state: str, creator: str = "osac-ci[bot]", url: str = "", text: str = "") -> None:
        self.clock += 1
        self.statuses.insert(
            0,
            {
                "context": context, "state": state, "description": text, "target_url": url,
                "creator": {"login": creator}, "created_at": f"2026-10-20T10:00:{self.clock:02d}Z",
            },
        )  # fmt: skip

    def check(
        self, name: str, conclusion: str | None, app: str = APP, link: str = "https://x/check", title: str = ""
    ) -> None:
        self.clock += 1
        self.check_runs.append(
            {
                "id": self.clock, "name": name, "status": "completed" if conclusion else "in_progress",
                "conclusion": conclusion, "started_at": f"2026-10-20T10:00:{self.clock:02d}Z",
                "app": {"slug": app}, "details_url": link, "output": {"title": title},
            }
        )  # fmt: skip

    def job(self, name: str, status: str, conclusion: str | None = None) -> None:
        self.jobs.append({"name": name, "status": status, "conclusion": conclusion, "html_url": JOB_URL})

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        page, per = int((params or {}).get("page", 1)), int((params or {}).get("per_page", 100))
        if method == "GET" and path == f"{BASE}/commits/{SHA}/statuses":
            if self.statuses_answer != 200:
                return Response(self.statuses_answer, {"message": "no"})
            return Response(200, self.statuses[(page - 1) * per : page * per])
        if method == "GET" and path == f"{BASE}/commits/{SHA}/check-runs":
            return Response(200, {"check_runs": self.check_runs[(page - 1) * per : page * per]})
        if method == "GET" and path == f"{BASE}/actions/runs/{RUN}/jobs":
            return Response(200, {"jobs": self.jobs[(page - 1) * per : page * per]})
        if method == "GET" and re.fullmatch(rf"{BASE}/actions/runs/\d+", path):
            return Response(200, {"status": "in_progress"})
        if method == "POST" and path == f"{BASE}/statuses/{SHA}":
            self.posts.append(("status", body))
            self.status(
                body["context"], body["state"], url=body.get("target_url", ""), text=body.get("description", "")
            )
            return Response(201, {})
        if method == "POST" and path == f"{BASE}/check-runs":
            self.posts.append(("check", body))
            self.check(
                body["name"], body.get("conclusion"), link=body.get("details_url", ""), title=body["output"]["title"]
            )
            return Response(201, {})
        return Response(404, {"message": f"no route for {method} {path}"})


# reading the statuses as check runs --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "status", "conclusion"),
    [("pending", "in_progress", None), ("success", "completed", "success"), ("failure", "completed", "failure"),
     ("error", "completed", "cancelled")],
)  # fmt: skip
def test_a_status_is_read_as_a_check_run_of_the_app_with_its_link_as_the_details(
    state: str, status: str, conclusion: str | None
) -> None:
    hub = Hub()
    hub.status(VM, state, url=JOB_URL, text="Passed")
    (run,) = suite_line_runs(hub, REPO, SHA, [VM], APP)  # type: ignore[arg-type]
    assert run["name"] == VM and run["status"] == status and run["conclusion"] == conclusion
    assert run["app"] == {"slug": APP} and run["details_url"] == JOB_URL == run["html_url"]
    assert run["output"] == {"title": "Passed"} and run["started_at"]


def test_the_statuses_come_back_oldest_first_so_that_of_equal_times_the_newer_is_the_later() -> None:
    hub = Hub()
    hub.status(VM, "pending")
    hub.status(VM, "success")  # newest: listed first by GitHub
    runs = suite_line_runs(hub, REPO, SHA, [VM], APP)  # type: ignore[arg-type]
    assert [r["conclusion"] for r in runs] == [None, "success"]


def test_only_the_named_contexts_written_by_the_app_are_lines() -> None:
    hub = Hub()
    hub.status(VM, "success", creator="osac-ci[bot]")
    hub.status(VM, "success", creator="github-actions[bot]")  # any workflow can write one of these
    hub.status(VM, "failure", creator="mallory")  # the same name, anyone else
    hub.status("license/snyk", "success")
    hub.status(CA, "success")
    runs = suite_line_runs(hub, REPO, SHA, [VM], APP)  # type: ignore[arg-type]
    assert [(r["name"], r["conclusion"]) for r in runs] == [(VM, "success")]


def test_a_forged_passing_line_of_the_actions_bot_is_not_believed_by_the_sum_of_a_run_by_hand() -> None:
    hub = Hub()
    hub.status(VM, "success", creator="github-actions[bot]")
    assert e2e_check.suite_states(hub, REPO, SHA, {"vmaas": VM})["vmaas"] == "none"  # type: ignore[arg-type]
    assert "not every suite has passed" in e2e_check.finish_by_hand(hub, REPO, SHA, {"vmaas": VM}, RUN_URL, "checks")  # type: ignore[arg-type]
    assert not [1 for kind, _ in hub.posts if kind == "check"], "the E2E check was not written"


def test_no_contexts_asks_for_nothing_and_a_bad_commit_is_refused() -> None:
    hub = Hub()
    hub.statuses_answer = 500
    assert suite_line_runs(hub, REPO, SHA, [], APP) == []  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="invalid commit"):
        suite_line_runs(hub, REPO, "xyz", [VM], APP)  # type: ignore[arg-type]


@pytest.mark.parametrize(("answer", "readable"), [(401, True), (403, True), (404, True), (500, False)])
def test_a_token_that_may_not_read_statuses_gives_no_lines_but_a_server_error_is_an_error(
    answer: int, readable: bool
) -> None:
    hub = Hub()
    hub.statuses_answer = answer
    with pytest.raises(GitHubError):
        suite_line_runs(hub, REPO, SHA, [VM], APP)  # type: ignore[arg-type]
    if readable:
        assert suite_line_runs_if_readable(hub, REPO, SHA, [VM], APP) == []  # type: ignore[arg-type]
    else:
        with pytest.raises(GitHubError):
            suite_line_runs_if_readable(hub, REPO, SHA, [VM], APP)  # type: ignore[arg-type]


# what a line says --------------------------------------------------------------------------------------------------


def test_the_sum_of_a_run_by_hand_reads_lines_that_are_statuses_and_lines_that_are_check_runs() -> None:
    hub = Hub()
    hub.status(VM, "success")
    hub.check(CA, "success")
    states = e2e_check.suite_states(hub, REPO, SHA, {"vmaas": VM, "caas": CA, "bmaas": "E2E / BMaaS"})  # type: ignore[arg-type]
    assert states == {"vmaas": "passed", "caas": "passed", "bmaas": "none"}


def test_the_newest_line_of_a_name_wins_whatever_kind_it_is() -> None:
    hub = Hub()
    hub.check(VM, "success")  # an older line written as a check run
    hub.status(VM, "failure")  # the newer one, a status
    assert e2e_check.suite_states(hub, REPO, SHA, {"vmaas": VM})["vmaas"] == "failed"  # type: ignore[arg-type]
    other = Hub()
    other.status(VM, "failure")
    other.check(VM, "success")
    assert e2e_check.suite_states(other, REPO, SHA, {"vmaas": VM})["vmaas"] == "passed"  # type: ignore[arg-type]


def test_a_pending_status_is_a_suite_that_is_active() -> None:
    hub = Hub()
    hub.status(VM, "pending", text="Waiting for a runner")
    assert e2e_check.suite_states(hub, REPO, SHA, {"vmaas": VM})["vmaas"] == "active"  # type: ignore[arg-type]


def test_a_token_that_cannot_read_statuses_leaves_the_sum_unchanged() -> None:
    hub = Hub()
    hub.statuses_answer = 403
    hub.check(VM, "success")
    assert e2e_check.suite_states(hub, REPO, SHA, {"vmaas": VM})["vmaas"] == "passed"  # type: ignore[arg-type]


# writing the lines as statuses while E2E stays a check run ---------------------------------------------------------


def entry(**extra: Any) -> dict[str, Any]:
    base = {
        "number": 7, "head_sha": SHA, "action": "start", "reason": "approved", "suites": ["vmaas", "caas"],
        "suite_checks": {"vmaas": VM, "caas": CA}, "approvers": ["bob"],
    }  # fmt: skip
    return {**base, **extra}


def test_the_report_writes_the_e2e_line_as_a_check_run_and_the_suite_lines_as_statuses() -> None:
    hub = Hub()
    found = e2e_check.report(hub, REPO, [entry()], RUN, RUN_URL, "checks", lines_kind="statuses")  # type: ignore[arg-type]
    assert found.start
    kinds = [(kind, body.get("name") or body.get("context")) for kind, body in hub.posts]
    assert ("check", "E2E") in kinds
    assert ("status", VM) in kinds and ("status", CA) in kinds
    assert not [1 for kind, body in hub.posts if kind == "check" and body["name"] in (VM, CA)]
    waiting = [body for kind, body in hub.posts if kind == "status"]
    assert {b["state"] for b in waiting} == {"pending"} and {b["description"] for b in waiting} == {
        "Waiting for a runner"
    }


def test_without_a_lines_kind_the_lines_are_written_like_the_e2e_line() -> None:
    hub = Hub()
    e2e_check.report(hub, REPO, [entry()], RUN, RUN_URL, "checks")  # type: ignore[arg-type]
    assert {kind for kind, _ in hub.posts} == {"check"}
    other = Hub()
    e2e_check.report(other, REPO, [entry()], RUN, RUN_URL, "checks", lines_kind="checks")  # type: ignore[arg-type]
    assert {kind for kind, _ in other.posts} == {"check"}


def test_a_lines_kind_that_is_neither_is_refused_before_anything_is_written() -> None:
    hub = Hub()
    with pytest.raises(ValueError, match="lines kind"):
        e2e_check.report(hub, REPO, [entry()], RUN, RUN_URL, "checks", lines_kind="labels")  # type: ignore[arg-type]
    assert hub.posts == []


def test_a_run_started_by_hand_writes_its_waiting_lines_as_statuses_too() -> None:
    hub = Hub()
    e2e_check.report(hub, REPO, [entry(manual=True)], RUN, RUN_URL, "checks", lines_kind="statuses")  # type: ignore[arg-type]
    assert [kind for kind, body in hub.posts if (body.get("context") or body.get("name")) in (VM, CA)] == [
        "status",
        "status",
    ]


# following the suites as statuses ------------------------------------------------------------------------------------


def test_tracking_and_settling_with_statuses_link_the_row_straight_to_the_job() -> None:
    hub = Hub()
    hub.job("vmaas / e2e-vmaas-full-install / e2e", "in_progress")
    ticks = iter(range(10_000))
    track(hub, REPO, SHA, RUN, RUN_URL, {"vmaas": VM}, kind="statuses", follow=False, timeout=0, interval=1,  # type: ignore[arg-type]
          sleep=lambda _: None, clock=lambda: float(next(ticks)))  # fmt: skip
    line = hub.statuses[0]
    assert (line["context"], line["state"], line["description"]) == (VM, "pending", "Running")
    assert line["target_url"] == JOB_URL, "the row of a status opens the job: its link is the job's"
    hub.jobs.clear()
    hub.job("vmaas / e2e-vmaas-full-install / e2e", "completed", "success")
    settle(hub, REPO, SHA, RUN, RUN_URL, {"vmaas": VM}, "statuses")  # type: ignore[arg-type]
    assert (hub.statuses[0]["state"], hub.statuses[0]["description"]) == ("success", "Passed")
    assert e2e_check.suite_states(hub, REPO, SHA, {"vmaas": VM})["vmaas"] == "passed"  # type: ignore[arg-type]


def test_the_status_of_a_workflow_s_own_token_still_says_which_run_owns_an_e2e_status() -> None:
    hub = Hub()
    hub.status("E2E", "pending", creator="github-actions[bot]", url="https://example.invalid/o/r/actions/runs/502")
    newest = e2e_check.newest_status(hub, REPO, SHA, "E2E", "statuses")  # type: ignore[arg-type]
    assert newest is not None and e2e_check.run_of_url(str(newest["target_url"])) == 502


def test_the_lines_of_an_app_that_wrote_them_as_statuses_count_as_ours_when_tracking() -> None:
    hub = Hub()
    hub.status(VM, "success", creator="osac-ci[bot]", url="https://example.invalid/o/r/actions/runs/501/job/1")
    newest = e2e_check.newest_status(hub, REPO, SHA, VM, "statuses")  # type: ignore[arg-type]
    assert newest is not None and e2e_check.run_of_url(str(newest["target_url"])) == 501
    hub.status(VM, "failure", creator="mallory")
    again = e2e_check.newest_status(hub, REPO, SHA, VM, "statuses")  # type: ignore[arg-type]
    assert again is not None and again["state"] == "success", "a status of anyone else is not a line"


# the workflows -----------------------------------------------------------------------------------------------------

import yaml  # noqa: E402
from helpers import ROOT  # noqa: E402

WORKFLOWS = ROOT / ".github" / "workflows"


def workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["e2e-pr-decide.yml", "e2e-pr-track.yml", "e2e-pr-finish.yml"])
def test_every_workflow_of_the_flow_takes_the_lines_kind_and_keeps_check_runs_by_default(name: str) -> None:
    given = workflow(name)[True]["workflow_call"]["inputs"]["lines-kind"]
    assert given["default"] == "checks" and given["type"] == "string"


def statuses_permissions(name: str) -> list[str]:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    return re.findall(r"permission-statuses: (.*?)  #", text)


@pytest.mark.parametrize(
    ("name", "tokens"), [("e2e-pr-decide.yml", 2), ("e2e-pr-track.yml", 3), ("e2e-pr-finish.yml", 1)]
)
def test_the_app_token_asks_for_statuses_write_only_when_the_lines_are_statuses(name: str, tokens: int) -> None:
    found = statuses_permissions(name)
    assert found == ["${{ inputs.lines-kind == 'statuses' && 'write' || '' }}"] * tokens


def test_the_suite_lines_follow_the_lines_kind_and_the_e2e_line_stays_a_check_run() -> None:
    track = (WORKFLOWS / "e2e-pr-track.yml").read_text(encoding="utf-8")
    assert track.count("kind: ${{ inputs.lines-kind }}") == 3 and "kind: checks" not in track
    finish = (WORKFLOWS / "e2e-pr-finish.yml").read_text(encoding="utf-8")
    settle_step = finish.split("e2e-suites-settle")[1].split("- uses:")[0]
    assert "kind: ${{ inputs.lines-kind }}" in settle_step
    assert finish.count("kind: checks") == 2, "the E2E line, written for a run and for a run started by hand"
    decide = workflow("e2e-pr-decide.yml")["jobs"]["decide"]["steps"]
    report = next(s for s in decide if s.get("uses", "").endswith("/e2e-report"))
    assert report["with"]["kind"] == "checks" and report["with"]["lines-kind"] == "${{ inputs.lines-kind }}"
    reading = next(s for s in decide if s.get("uses", "").endswith("/e2e-decide"))
    assert (
        reading["with"]["statuses-token"]
        == "${{ inputs.lines-kind == 'statuses' && steps.app-token.outputs.token || '' }}"
    )


# the command line ------------------------------------------------------------------------------------------------


def test_the_report_command_takes_a_lines_kind(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from osac_ci import cli

    hub = Hub()
    monkeypatch.setattr(cli, "build_client", lambda **_: hub)
    decision = tmp_path / "decision.json"
    decision.write_text(json.dumps([entry()]), encoding="utf-8")
    argv = ["e2e-report", "--repo", REPO, "--decision-file", str(decision), "--run-id", str(RUN), "--run-url", RUN_URL]
    assert cli.main([*argv, "--kind", "checks", "--lines-kind", "statuses"]) == 0
    assert ("status", VM) in [(k, b.get("context")) for k, b in hub.posts]
    with pytest.raises(SystemExit) as refused:
        cli.main([*argv, "--kind", "checks", "--lines-kind", "labels"])
    assert refused.value.code == 2


def test_the_statuses_token_makes_a_client_and_no_token_makes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    from osac_ci import cli

    monkeypatch.delenv(cli.STATUSES_TOKEN_ENV, raising=False)
    assert cli.build_statuses_client() is None
    monkeypatch.setenv(cli.STATUSES_TOKEN_ENV, "t")
    assert cli.build_statuses_client() is not None
