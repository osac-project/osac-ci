"""One job looks after a merge queue entry: the first failure of a suite of the entry, or of a required check of the
other workflows on its commit (the unit and integration tests), writes the required check at once and stops the rest.
Kept by a small in-memory GitHub: the workflow runs of one commit and their jobs."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml
from helpers import ROOT

from osac_ci import cli, queue_guard
from osac_ci.compare import Requirement
from osac_ci.e2e_suites_check import suite_progress
from osac_ci.github.api import Response
from osac_ci.queue_guard import verdict_of

pytestmark = pytest.mark.unit
REPO = "o/r"
SHA = "a" * 40
BASE = f"/repos/{REPO}"
THIS = 500  # the E2E run, which holds the suite jobs
CI = 501
REQUIRED = {"CI", "jira-reference"}


class GitHub:
    """The workflow runs of the merge group commit, their jobs, and what was asked of them."""

    def __init__(self, cancel_status: int = 202) -> None:
        self.runs: dict[int, dict[str, Any]] = {}
        self.cancelled: list[int] = []
        self.cancel_status = cancel_status
        self.looks = 0
        self.requests: list[str] = []  # every GET, to count what a look costs
        self.fail_reads = 0  # the next reads of the runs list that answer 502
        self.later: list[
            dict[str, Any]
        ] = []  # changes applied one per look: {"run": id, "job": name, "status", "conclusion"}
        self.run(THIS, "E2E for the merge queue", "in_progress")
        self.run(CI, "CI", "in_progress")

    def run(self, run_id: int, name: str, status: str = "in_progress") -> None:
        self.runs[run_id] = {"id": run_id, "name": name, "status": status, "jobs": {}}

    def job(self, run_id: int, name: str, status: str, conclusion: str | None = None) -> None:
        self.runs[run_id]["jobs"][name] = {"name": name, "status": status, "conclusion": conclusion}

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        params = params or {}
        if method == "GET" and params.get("page", "1") == "1":
            self.requests.append(path.removeprefix(BASE))
        if method == "GET" and path == f"{BASE}/actions/runs" and self.fail_reads > 0:
            self.fail_reads -= 1
            return Response(502, {})
        if method == "GET" and path == f"{BASE}/actions/runs":
            assert params.get("head_sha") == SHA and params.get("event") == "merge_group"
            if params.get("page", "1") == "1":
                self.looks += 1
                if self.looks > 1 and self.later:
                    change = self.later.pop(0)
                    if "fail" in change:
                        self.fail_reads = change["fail"]
                    else:
                        self.job(change["run"], change["job"], change["status"], change.get("conclusion"))
            listed = [{k: v for k, v in r.items() if k != "jobs"} for r in self.runs.values()]
            return Response(200, {"workflow_runs": listed if params.get("page", "1") == "1" else []})
        if method == "GET" and (m := re.fullmatch(rf"{BASE}/actions/runs/(\d+)/jobs", path)):
            jobs = list(self.runs[int(m.group(1))]["jobs"].values())
            return Response(200, {"jobs": jobs if params.get("page", "1") == "1" else []})
        if method == "POST" and (m := re.fullmatch(rf"{BASE}/actions/runs/(\d+)/cancel", path)):
            self.cancelled.append(int(m.group(1)))
            return Response(self.cancel_status, {})
        return Response(404, {})


def watch(gh: GitHub, suites: list[str], **kw: Any) -> str:
    kw.setdefault("timeout", 20)  # short, so an implementation that never stops fails the test instead of spinning
    ticks = iter(range(100_000))
    return queue_guard.watch(  # type: ignore[arg-type]
        gh, REPO, SHA, THIS, suites, REQUIRED, interval=1, sleep=lambda _: None, clock=lambda: float(next(ticks)), **kw
    )


def running(gh: GitHub) -> None:
    gh.job(THIS, "vmaas / e2e", "in_progress")
    gh.job(THIS, "caas / e2e", "in_progress")
    gh.job(CI, "unit-tests", "in_progress")
    gh.job(CI, "CI", "queued")


# which failures count ------------------------------------------------------------------------------


def test_a_failed_job_of_a_workflow_that_provides_a_required_check_counts_not_only_the_required_job() -> None:
    gh = GitHub()
    gh.job(CI, "unit-tests", "completed", "failure")
    gh.job(CI, "integration-tests", "completed", "success")
    gh.job(CI, "CI", "queued")
    assert queue_guard.required_failures(gh, REPO, SHA, THIS, REQUIRED) == ["CI / unit-tests"]  # type: ignore[arg-type]


def test_a_run_that_ended_failed_settles_the_entry_even_when_the_jobs_list_still_shows_a_job_running() -> None:
    gh = GitHub()
    gh.runs[CI].update(status="completed", conclusion="failure")
    gh.job(CI, "unit-tests", "in_progress")  # GitHub's jobs list lagging behind the run
    gh.job(CI, "CI", "completed", "skipped")
    assert queue_guard.required_failures(gh, REPO, SHA, THIS, REQUIRED) == ["CI"]  # type: ignore[arg-type]
    gh.job(CI, "unit-tests", "completed", "failure")  # once the list catches up the job is named instead
    assert queue_guard.required_failures(gh, REPO, SHA, THIS, REQUIRED) == ["CI / unit-tests"]  # type: ignore[arg-type]


@pytest.mark.parametrize("conclusion", ["success", "cancelled", "skipped", None])
def test_a_run_that_ended_without_failing_or_has_not_ended_does_not_settle_it(conclusion: str | None) -> None:
    gh = GitHub()
    gh.runs[CI].update(status="completed" if conclusion else "in_progress", conclusion=conclusion)
    gh.job(CI, "CI", "queued")
    assert queue_guard.required_failures(gh, REPO, SHA, THIS, REQUIRED) == []  # type: ignore[arg-type]


def test_a_workflow_that_provides_no_required_check_is_left_alone_and_so_is_this_run() -> None:
    gh = GitHub()
    gh.run(777, "CodeQL")
    gh.job(777, "analyze", "completed", "failure")
    gh.job(THIS, "bmaas / e2e", "completed", "failure")
    assert queue_guard.required_failures(gh, REPO, SHA, THIS, REQUIRED) == []  # type: ignore[arg-type]


def test_this_run_is_never_read_as_another_workflow_even_if_a_job_of_it_is_named_like_a_required_check() -> None:
    gh = GitHub()
    gh.job(THIS, "CI", "completed", "failure")  # this run's own job: the suites are watched by their ids
    assert queue_guard.required_failures(gh, REPO, SHA, THIS, REQUIRED) == []  # type: ignore[arg-type]


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure", "action_required"])
def test_every_way_of_not_passing_that_is_not_a_cancel_or_a_skip_is_a_failure(conclusion: str) -> None:
    gh = GitHub()
    gh.job(CI, "jira-reference", "completed", conclusion)
    gh.job(CI, "CI", "queued")
    assert queue_guard.required_failures(gh, REPO, SHA, THIS, REQUIRED) == ["CI / jira-reference"]  # type: ignore[arg-type]


def test_jobs_that_are_cancelled_skipped_or_not_ended_are_not_failures() -> None:
    gh = GitHub()
    gh.job(CI, "a", "completed", "cancelled")
    gh.job(CI, "b", "completed", "skipped")
    gh.job(CI, "c", "in_progress")
    gh.job(CI, "d", "completed", "success")
    gh.job(CI, "CI", "queued")
    assert queue_guard.required_failures(gh, REPO, SHA, THIS, REQUIRED) == []  # type: ignore[arg-type]


# watching ------------------------------------------------------------------------------------------


def test_a_failed_suite_settles_the_entry() -> None:
    gh = GitHub()
    running(gh)
    gh.job(THIS, "bmaas / e2e", "completed", "failure")
    assert watch(gh, ["vmaas", "caas", "bmaas"]) == "failed"


def test_a_failed_unit_test_job_settles_the_entry_while_the_suites_still_run() -> None:
    gh = GitHub()
    running(gh)
    gh.later = [{"run": CI, "job": "unit-tests", "status": "completed", "conclusion": "failure"}]
    assert watch(gh, ["vmaas", "caas"]) == "failed" and gh.looks >= 2


def test_a_failure_of_a_workflow_that_provides_no_required_check_does_not_settle_it() -> None:
    gh = GitHub()
    running(gh)
    gh.run(777, "CodeQL")
    gh.job(777, "analyze", "completed", "failure")
    assert watch(gh, ["vmaas", "caas"], timeout=5) == "timeout"


def test_the_watch_is_done_when_every_suite_has_ended_without_a_failure_whatever_ci_is_doing() -> None:
    gh = GitHub()
    running(gh)
    gh.job(THIS, "vmaas / e2e", "completed", "success")
    gh.job(THIS, "caas / e2e", "completed", "success")
    assert watch(gh, ["vmaas", "caas"]) == "done"


def test_a_suite_cancelled_or_skipped_is_not_a_failure_and_a_suite_with_no_job_yet_is_waited_for() -> None:
    gh = GitHub()
    gh.job(THIS, "vmaas / e2e", "completed", "cancelled")
    gh.job(THIS, "caas / e2e", "completed", "skipped")
    assert watch(gh, ["vmaas", "caas"]) == "done"
    pending = GitHub()
    pending.job(THIS, "vmaas / e2e", "completed", "success")
    assert watch(pending, ["vmaas", "caas"], timeout=3) == "timeout"


@pytest.mark.parametrize("suites", [["vmaas; id"], ["../x"], [""]])
def test_a_list_that_is_not_suite_ids_is_refused_before_anything_is_read(suites: list[str]) -> None:
    gh = GitHub()
    with pytest.raises(ValueError, match="suite ids"):
        watch(gh, suites)
    assert gh.looks == 0


# what the check says -------------------------------------------------------------------------------

PASSED = {"vmaas": "passed", "caas": "passed", "bmaas": "passed"}


@pytest.mark.parametrize(
    ("progress", "plan", "others", "outcome", "expected"),
    [
        (PASSED, "success", [], "success", ("success", "vmaas, caas, bmaas", False)),
        ({**PASSED, "bmaas": "failed"}, "success", [], "failed", ("failure", "bmaas", True)),
        ({**PASSED, "vmaas": "failed", "bmaas": "failed"}, "success", [], "success", ("failure", "vmaas, bmaas", True)),
        ({**PASSED, "bmaas": "running"}, "success", ["CI / unit-tests"], "failed",
         ("cancelled", "stopped, CI / unit-tests failed", True)),
        ({**PASSED, "bmaas": "failed"}, "success", ["CI / unit-tests"], "failed", ("failure", "bmaas", True)),
        ({**PASSED, "caas": "cancelled"}, "success", [], "success", ("cancelled", "", False)),
        ({**PASSED, "caas": "skipped"}, "success", [], "success", ("failure", "vmaas, caas, bmaas", False)),
        ({"vmaas": "running", "caas": "waiting"}, "success", [], "cancelled", ("cancelled", "", False)),
        (PASSED, "success", [], "cancelled", ("success", "vmaas, caas, bmaas", False)),
        ({"vmaas": "passed", "caas": "running"}, "success", [], "timeout",
         ("failure", "a suite did not end in time", True)),
        ({}, "failure", [], "skipped", ("failure", "", False)),
        ({}, "skipped", [], "skipped", ("failure", "", False)),
        ({}, "cancelled", [], "skipped", ("cancelled", "", False)),
        ({}, "success", [], "skipped", ("success", "", False)),
    ],
)  # fmt: skip
def test_the_verdict_for_each_way_an_entry_can_end(
    progress: dict, plan: str, others: list[str], outcome: str, expected: tuple[str, str, bool]
) -> None:
    got = verdict_of(progress, plan, others, outcome)
    assert (got.result, got.detail, got.stop) == expected


def test_a_failed_suite_is_named_and_the_text_is_cut_to_what_a_check_can_hold() -> None:
    many = {f"suite{n}": "failed" for n in range(40)}
    assert len(verdict_of(many, "success", [], "failed").detail) <= queue_guard.DETAIL_MAX
    long = verdict_of(PASSED, "success", ["CI / " + "x" * 300], "failed")
    assert long.result == "cancelled" and len(long.detail) <= queue_guard.DETAIL_MAX


def test_the_verdict_reads_the_state_of_the_entry_now() -> None:
    gh = GitHub()
    running(gh)
    gh.job(THIS, "bmaas / e2e", "completed", "failure")
    got = queue_guard.verdict(gh, REPO, SHA, THIS, ["vmaas", "caas", "bmaas"], REQUIRED, "success", "success")  # type: ignore[arg-type]
    assert (got.result, got.detail, got.stop) == ("failure", "bmaas", True)
    quiet = GitHub()
    got = queue_guard.verdict(quiet, REPO, SHA, THIS, [], REQUIRED, "success", "skipped")  # type: ignore[arg-type]
    assert (got.result, got.stop) == ("success", False) and quiet.looks == 0, "no suites: nothing is read"


# stopping everything -------------------------------------------------------------------------------


def test_every_run_of_the_commit_that_has_not_ended_is_cancelled_this_one_last() -> None:
    gh = GitHub()
    gh.run(502, "jira-reference", "queued")
    gh.run(503, "CodeQL", "completed")
    assert queue_guard.stop_runs(gh, REPO, SHA, THIS) == [CI, 502, THIS]  # type: ignore[arg-type]
    assert gh.cancelled == [CI, 502, THIS]


def test_a_run_that_has_ended_already_is_not_an_error_but_a_refusal_to_cancel_is() -> None:
    ended = GitHub(cancel_status=409)
    assert queue_guard.stop_runs(ended, REPO, SHA, THIS) == [CI, THIS]  # type: ignore[arg-type]
    refused = GitHub(cancel_status=403)
    with pytest.raises(Exception, match="cancelling run"):
        queue_guard.stop_runs(refused, REPO, SHA, THIS)  # type: ignore[arg-type]


def test_the_branch_of_a_merge_group_is_read_from_its_ref() -> None:
    assert queue_guard.branch_name("refs/heads/main") == "main"
    assert queue_guard.branch_name("main") == "main"


# the commands --------------------------------------------------------------------------------------


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> GitHub:
    gh = GitHub()
    running(gh)
    monkeypatch.setattr(cli, "build_client", lambda **_: gh)
    monkeypatch.setattr(cli, "required_contexts", lambda *_: (Requirement("CI"), Requirement("jira-reference")))
    return gh


def common(*more: str) -> list[str]:
    return ["--repo", REPO, "--sha", SHA, "--run-id", str(THIS), *more]


def test_the_watch_command_prints_how_it_ended(commands: GitHub, capsys: pytest.CaptureFixture[str]) -> None:
    commands.job(THIS, "bmaas / e2e", "completed", "failure")
    argv = ["e2e-queue-watch", *common("--suites", '["vmaas","caas","bmaas"]', "--branch", "refs/heads/main")]
    assert cli.main([*argv, "--interval", "0", "--timeout", "3"]) == 0
    assert json.loads(capsys.readouterr().out) == {"outcome": "failed"}


def test_the_verdict_command_prints_the_result_and_whether_to_stop(
    commands: GitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    commands.job(THIS, "bmaas / e2e", "completed", "failure")
    argv = ["e2e-queue-verdict", *common("--suites", '["vmaas","caas","bmaas"]', "--branch", "main")]
    assert cli.main([*argv, "--plan-result", "success", "--watch-outcome", "success"]) == 0
    assert json.loads(capsys.readouterr().out) == {"result": "failure", "detail": "bmaas", "stop": True}


def test_the_stop_command_cancels_the_runs_and_lists_them(commands: GitHub, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["e2e-queue-stop", *common()]) == 0
    assert json.loads(capsys.readouterr().out) == [CI, THIS] and commands.cancelled == [CI, THIS]


@pytest.mark.parametrize("suites", ["not json", '{"a": 1}', "[1]", '["x; id"]'])
def test_a_bad_suite_list_exits_3(commands: GitHub, capsys: pytest.CaptureFixture[str], suites: str) -> None:
    argv = ["e2e-queue-watch", *common("--suites", suites, "--branch", "main"), "--interval", "0", "--timeout", "2"]
    assert cli.main(argv) == 3
    assert "e2e-queue-watch failed" in capsys.readouterr().err


# the action and the workflow -----------------------------------------------------------------------

ACTION = ROOT / ".github" / "actions" / "e2e-queue-guard" / "action.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "e2e-queue-result.yml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_every_input_used_is_declared_nothing_is_expanded_into_the_script_and_nothing_is_checked_out() -> None:
    text = ACTION.read_text(encoding="utf-8")
    assert set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", text)) == set(load(ACTION)["inputs"])
    for step in load(ACTION)["runs"]["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")
        assert "checkout" not in step.get("uses", "")


def run_action(tmp_path: Path, **env: str) -> tuple[int, str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    answers = {
        "e2e-queue-watch": '{"outcome":"failed"}',
        "e2e-queue-verdict": '{"result":"failure","detail":"bmaas","stop":true}',
        "e2e-queue-stop": "[1]",
    }
    uv.write_text("#!/bin/bash\necho \"$@\" >> '" + str(tmp_path) + "/uv-args'\ncase \"$*\" in\n" + "\n".join(
        f"  *{name}*) echo '{answer}' ;;" for name, answer in answers.items()) + "\nesac\n")  # fmt: skip
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    script = next(s["run"] for s in load(ACTION)["runs"]["steps"] if s.get("id") == "guard")
    out = tmp_path / "out"
    full = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_TOKEN": "t", "PHASE": "watch", "REPO": REPO, "SHA": SHA,
            "RUN_ID": "7", "SUITES": '["vmaas"]', "BRANCH": "refs/heads/main", "PLAN_RESULT": "success",
            "WATCH_OUTCOME": "success", "TIMEOUT": "60", "INTERVAL": "5", "ACTION_PATH": "/a/b/c",
            "GITHUB_OUTPUT": str(out), **env}  # fmt: skip
    done = subprocess.run(["bash", "-c", script], env=full, capture_output=True, text=True, check=False)
    args = tmp_path / "uv-args"
    return done.returncode, args.read_text() if args.exists() else "", out.read_text() if out.exists() else ""


def test_the_watch_phase_passes_its_inputs_and_outputs_how_it_ended(tmp_path: Path) -> None:
    code, args, out = run_action(tmp_path)
    assert code == 0 and out.strip() == "outcome=failed"
    assert f'e2e-queue-watch --repo o/r --sha {SHA} --run-id 7 --suites ["vmaas"] --branch refs/heads/main' in args
    assert "--timeout 60 --interval 5" in args


def test_the_verdict_phase_outputs_the_result_the_detail_and_whether_to_stop(tmp_path: Path) -> None:
    code, args, out = run_action(tmp_path, PHASE="verdict", WATCH_OUTCOME="cancelled", PLAN_RESULT="success")
    assert code == 0 and out.splitlines() == ["result=failure", "detail=bmaas", "stop=true"]
    assert "--plan-result success --watch-outcome cancelled" in args


def test_the_stop_phase_asks_for_nothing_but_the_run_and_the_commit(tmp_path: Path) -> None:
    code, args, _ = run_action(tmp_path, PHASE="stop")
    assert code == 0 and f"e2e-queue-stop --repo o/r --sha {SHA} --run-id 7" in args and "--suites" not in args


@pytest.mark.parametrize(
    "env", [{"RUN_ID": "7; id"}, {"RUN_ID": ""}, {"TIMEOUT": "1h"}, {"INTERVAL": "$(id)"}, {"PHASE": "delete"}]
)
def test_a_value_that_is_not_what_it_should_be_is_refused_before_the_command_runs(
    tmp_path: Path, env: dict[str, str]
) -> None:
    code, args, _ = run_action(tmp_path, **env)
    assert code == 1 and args == ""


def steps_of_the_workflow() -> list[dict]:
    return load(WORKFLOW)["jobs"]["result"]["steps"]


def test_one_job_watches_then_always_tells_the_check_and_stops_what_runs_after_the_check_is_written() -> None:
    watch_step, verdict_step, token, finish, stop = steps_of_the_workflow()
    assert watch_step["if"] == "inputs.plan-result == 'success' && inputs.suites != '[]'"
    assert watch_step["with"]["phase"] == "watch" and watch_step["id"] == "watch"
    assert verdict_step["if"] == "always()" and verdict_step["with"]["phase"] == "verdict"
    assert "steps.watch.outputs.outcome" in verdict_step["with"]["watch-outcome"], (
        "what the watch returned, not only its step"
    )
    assert token["if"] == "always()" and "create-github-app-token" in token["uses"]
    assert finish["if"] == "always()" and finish["uses"] == "$/.github/actions/e2e-finish"
    assert finish["with"]["result"] == "${{ steps.verdict.outputs.result || 'cancelled' }}"
    assert (
        finish["with"]["kind"] == "checks" and finish["with"]["github-token"] == "${{ steps.app-token.outputs.token }}"
    )
    assert stop["if"] == "always() && steps.verdict.outputs.stop == 'true'" and stop["with"]["phase"] == "stop"


def test_the_app_token_is_made_after_the_watch_and_the_watch_holds_only_the_built_in_token() -> None:
    steps = steps_of_the_workflow()
    names = [s.get("id") or s.get("name") for s in steps]
    assert names.index("watch") < names.index("app-token") < names.index("Stop what still runs")
    for step in steps:
        if step.get("with", {}).get("phase"):
            assert step["with"]["github-token"] == "${{ github.token }}"


def test_the_job_has_the_permissions_it_needs_the_secret_is_explicit_and_nothing_is_checked_out() -> None:
    wf = load(WORKFLOW)
    assert wf["permissions"] == {} and wf["jobs"]["result"]["permissions"] == {"actions": "write", "contents": "read"}
    assert wf[True]["workflow_call"]["secrets"]["OSAC_CI_APP_PRIVATE_KEY"]["required"] is True
    for name in ("sha", "branch", "plan-result"):
        assert wf[True]["workflow_call"]["inputs"][name]["required"] is True
    assert "uses: actions/checkout" not in WORKFLOW.read_text(encoding="utf-8")


# what the watch costs ---------------------------------------------------------------------------------


def test_a_look_reads_the_runs_once_the_jobs_of_this_run_once_and_the_jobs_of_each_other_run_once() -> None:
    gh = GitHub()
    running(gh)
    gh.run(502, "jira-reference")
    gh.job(502, "jira-reference", "in_progress")
    watch(gh, ["vmaas", "caas"], timeout=0)
    assert sorted(gh.requests) == sorted(
        ["/actions/runs", f"/actions/runs/{THIS}/jobs", f"/actions/runs/{CI}/jobs", "/actions/runs/502/jobs"]
    )


def test_a_run_that_provides_no_required_check_or_has_ended_well_is_not_read_again() -> None:
    gh = GitHub()
    running(gh)
    gh.run(777, "CodeQL")
    gh.job(777, "analyze", "in_progress")
    gh.run(502, "jira-reference", "completed")
    gh.runs[502]["conclusion"] = "success"
    gh.job(502, "jira-reference", "completed", "success")
    watch(gh, ["vmaas", "caas"], timeout=6)
    reads = [r for r in gh.requests if r.endswith("/jobs")]
    assert reads.count("/actions/runs/777/jobs") == 1, "no required check: read once and left alone"
    assert reads.count("/actions/runs/502/jobs") == 0, "ended without failing: never read"
    assert reads.count(f"/actions/runs/{CI}/jobs") > 1, (
        "a run that provides a required check and runs is read each look"
    )


def test_the_looks_are_spaced_out_after_ten_minutes() -> None:
    gh = GitHub()
    running(gh)
    waits: list[float] = []
    now = [0.0]

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    result = queue_guard.watch(  # type: ignore[arg-type]
        gh, REPO, SHA, THIS, ["vmaas", "caas"], REQUIRED, timeout=1500, interval=30, sleep=sleep, clock=lambda: now[0]
    )
    assert result == "timeout"
    assert waits[0] == 30 and waits[-1] == 60 and set(waits) == {30, 60}
    assert waits.index(60) * 30 >= queue_guard.BACKOFF_AFTER - 30
    slower = [w for w in waits if w == 60]
    assert all(w == 60 for w in waits[waits.index(60) :]) and slower


# what the review of the pull request found ------------------------------------------------------------------


def test_a_hiccup_of_github_does_not_end_the_watch_and_a_run_of_hiccups_does() -> None:
    gh = GitHub()
    running(gh)
    gh.fail_reads = 2
    gh.job(THIS, "bmaas / e2e", "completed", "failure")
    assert watch(gh, ["vmaas", "caas", "bmaas"]) == "failed", "two failed reads, then the failure is seen"
    many = GitHub()
    running(many)
    many.fail_reads = 50
    with pytest.raises(Exception, match="502"):
        watch(many, ["vmaas", "caas"])
    assert many.fail_reads == 50 - queue_guard.MAX_ERRORS


def test_the_count_of_failed_reads_starts_again_after_a_good_one() -> None:
    gh = GitHub()
    running(gh)
    gh.fail_reads = queue_guard.MAX_ERRORS - 1
    gh.later = [{"fail": queue_guard.MAX_ERRORS - 1}]  # after the first good look, the next reads fail again
    assert watch(gh, ["vmaas", "caas"], timeout=40) == "timeout", "never MAX_ERRORS in a row, so the watch goes on"
    assert gh.fail_reads == 0


def test_the_verdict_tries_the_lookup_again_before_giving_up() -> None:
    gh = GitHub()
    running(gh)
    gh.job(THIS, "bmaas / e2e", "completed", "failure")
    gh.fail_reads = 2
    got = queue_guard.verdict(  # type: ignore[arg-type]
        gh, REPO, SHA, THIS, ["vmaas", "caas", "bmaas"], REQUIRED, "success", "success", sleep=lambda _: None
    )
    assert (got.result, got.detail, got.stop) == ("failure", "bmaas", True)
    hopeless = GitHub()
    hopeless.fail_reads = 50
    with pytest.raises(Exception, match="502"):
        queue_guard.verdict(  # type: ignore[arg-type]
            hopeless, REPO, SHA, THIS, ["vmaas"], REQUIRED, "success", "success", sleep=lambda _: None
        )


def test_a_verdict_that_cannot_be_read_closes_the_check_as_cancelled_and_stops_the_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = GitHub()
    running(gh)
    gh.fail_reads = 100
    monkeypatch.setattr(cli, "build_client", lambda **_: gh)
    monkeypatch.setattr(cli, "required_contexts", lambda *_: (Requirement("CI"),))
    monkeypatch.setattr(queue_guard.time, "sleep", lambda _: None)
    argv = ["e2e-queue-verdict", *common("--suites", '["vmaas"]', "--branch", "main")]
    assert cli.main([*argv, "--plan-result", "success", "--watch-outcome", "success"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == {
        "result": "cancelled",
        "detail": "could not read the state of the entry",
        "stop": True,
    }
    assert "::warning::" in out.err


def test_a_run_that_ended_well_is_never_read_and_one_with_no_jobs_listed_yet_is_not_taken_for_unrelated() -> None:
    gh = GitHub()
    gh.runs[CI].update(status="completed", conclusion="success")
    gh.job(CI, "CI", "completed", "success")
    assert queue_guard.required_failures(gh, REPO, SHA, THIS, REQUIRED) == []  # type: ignore[arg-type]
    assert f"/actions/runs/{CI}/jobs" not in gh.requests
    lagging = GitHub()
    lagging.runs[CI].update(status="completed", conclusion="failure")  # its jobs are not listed yet
    assert queue_guard.required_failures(lagging, REPO, SHA, THIS, REQUIRED) == ["CI"]  # type: ignore[arg-type]
    other = GitHub()
    other.run(777, "CodeQL")
    other.runs[777].update(status="completed", conclusion="failure")
    other.job(777, "analyze", "completed", "failure")  # jobs are listed, none is a required check: not ours
    assert queue_guard.required_failures(other, REPO, SHA, THIS, REQUIRED) == []  # type: ignore[arg-type]


def test_a_suite_has_not_passed_until_every_job_at_its_deepest_level_has() -> None:
    gh = GitHub()
    gh.job(THIS, "caas / e2e", "completed", "success")
    gh.job(THIS, "caas / teardown", "completed", "failure")
    assert suite_progress(gh, REPO, THIS, ["caas"]) == {"caas": "failed"}  # type: ignore[arg-type]
    gh.job(THIS, "caas / teardown", "in_progress")
    assert suite_progress(gh, REPO, THIS, ["caas"]) == {"caas": "running"}  # type: ignore[arg-type]
    gh.job(THIS, "caas / teardown", "completed", "success")
    assert suite_progress(gh, REPO, THIS, ["caas"]) == {"caas": "passed"}  # type: ignore[arg-type]
    gh.job(THIS, "caas / deeper / x", "completed", "failure")  # a deeper job is what counts
    assert suite_progress(gh, REPO, THIS, ["caas"]) == {"caas": "failed"}  # type: ignore[arg-type]
    gh.job(THIS, "caas / e2e", "completed", "cancelled")
    gh.job(THIS, "caas / deeper / x", "completed", "success")
    assert suite_progress(gh, REPO, THIS, ["caas"]) == {"caas": "passed"}  # type: ignore[arg-type]


def test_the_detail_is_one_line_and_the_outputs_are_what_they_should_be(tmp_path: Path) -> None:
    hostile = '{"result":"failure\\nstop=true","detail":"a\\r\\nstop=true","stop":"yes"}'
    uv = tmp_path / "bin"
    uv.mkdir()
    fake = uv / "uv"
    fake.write_text(f"#!/bin/bash\necho '{hostile}'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    script = next(s["run"] for s in load(ACTION)["runs"]["steps"] if s.get("id") == "guard")
    out = tmp_path / "out"
    env = {"PATH": f"{uv}:{os.environ['PATH']}", "GH_TOKEN": "t", "PHASE": "verdict", "REPO": REPO, "SHA": SHA,
           "RUN_ID": "7", "SUITES": "[]", "BRANCH": "main", "PLAN_RESULT": "success", "WATCH_OUTCOME": "success",
           "TIMEOUT": "60", "INTERVAL": "5", "ACTION_PATH": "/a/b/c", "GITHUB_OUTPUT": str(out)}  # fmt: skip
    done = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    lines = out.read_text().splitlines()
    assert lines == ["result=cancelled", "detail=a stop=true", "stop=false"], "one line each, nothing injected"


# the second review ----------------------------------------------------------------------------------


def evaluate(expression: str, **values: str) -> str:
    """An expression of the workflow with the contexts given: string literals, == and the short-circuiting && and ||."""
    import ast

    code = expression.removeprefix("${{").removesuffix("}}").strip()
    for name, value in values.items():
        code = code.replace(name, repr(value))
    code = " ".join(code.replace("&&", " and ").replace("||", " or ").split())

    def walk(node: ast.AST) -> object:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BoolOp):
            stop_on = isinstance(node.op, ast.Or)
            value: object = None
            for operand in node.values:
                value = walk(operand)
                if bool(value) is stop_on:
                    return value
            return value
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq):
            return walk(node.left) == walk(node.comparators[0])
        raise ValueError(f"unsupported: {ast.dump(node)[:60]}")

    return str(walk(ast.parse(code, mode="eval")))


@pytest.mark.parametrize(
    ("step", "returned", "expected"),
    [
        ("success", "timeout", "timeout"),
        ("success", "failed", "failed"),
        ("success", "done", "done"),
        ("success", "", "success"),  # nothing returned: the step's own word
        ("cancelled", "", "cancelled"),
        ("cancelled", "done", "cancelled"),
        ("failure", "", "failure"),
        ("skipped", "", "skipped"),
    ],
)
def test_the_verdict_gets_what_the_watch_returned_and_otherwise_how_its_step_ended(
    step: str, returned: str, expected: str
) -> None:
    binding = steps_of_the_workflow()[1]["with"]["watch-outcome"]
    got = evaluate(binding, **{"steps.watch.outputs.outcome": returned, "steps.watch.outcome": step})
    assert got == expected


def test_a_watch_that_timed_out_stops_the_suites_that_still_run_through_the_whole_binding() -> None:
    binding = steps_of_the_workflow()[1]["with"]["watch-outcome"]
    outcome = evaluate(binding, **{"steps.watch.outputs.outcome": "timeout", "steps.watch.outcome": "success"})
    got = verdict_of({"vmaas": "passed", "caas": "running"}, "success", [], outcome)
    assert (got.result, got.detail, got.stop) == ("failure", "a suite did not end in time", True)


def test_the_required_checks_that_cannot_be_read_do_not_lose_the_verdict(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from osac_ci.github.api import GitHubError

    gh = GitHub()
    running(gh)
    monkeypatch.setattr(cli, "build_client", lambda **_: gh)

    def unreadable(*_: object) -> None:
        raise GitHubError(502, "reading the rules")

    monkeypatch.setattr(cli, "required_contexts", unreadable)
    verdict_argv = ["e2e-queue-verdict", *common("--suites", '["vmaas"]', "--branch", "main")]
    assert cli.main([*verdict_argv, "--plan-result", "success", "--watch-outcome", "failed"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == {
        "result": "cancelled",
        "detail": "could not read the state of the entry",
        "stop": True,
    }
    watch_argv = [
        "e2e-queue-watch",
        *common("--suites", '["vmaas"]', "--branch", "main"),
        "--interval",
        "0",
        "--timeout",
        "2",
    ]
    assert cli.main(watch_argv) == 3, "the watch has no fallback: its step fails and the verdict reads the state itself"
