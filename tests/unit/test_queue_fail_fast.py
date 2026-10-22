"""A merge queue entry whose suite failed is told at once: the run of its suites is stopped on the first failure, and
the required check names the suite that failed. Kept by a small in-memory GitHub: the jobs of one workflow run."""

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

from osac_ci import cli
from osac_ci.e2e_suites_check import fail_fast, suite_progress
from osac_ci.github.api import Response

pytestmark = pytest.mark.unit
REPO = "o/r"
RUN = 500
BASE = f"/repos/{REPO}"


class Run:
    """The jobs of one workflow run, and what was asked of it."""

    def __init__(self, cancel_status: int = 202) -> None:
        self.jobs: list[dict[str, Any]] = []
        self.cancelled: list[int] = []
        self.cancel_status = cancel_status
        self.looks = 0
        self.then: list[Mapping[str, tuple[str, str | None]]] = []  # what the jobs become at each look after the first

    def job(self, name: str, status: str, conclusion: str | None = None) -> None:
        self.jobs = [j for j in self.jobs if j["name"] != name]
        self.jobs.append({"name": name, "status": status, "conclusion": conclusion, "html_url": f"https://x/{name}"})

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path == f"{BASE}/actions/runs/{RUN}/jobs":
            if (params or {}).get("page", "1") == "1":
                self.looks += 1
                if self.looks > 2 and self.then:  # each look takes two requests (progress, then the jobs of the suites)
                    for name, (status, conclusion) in self.then.pop(0).items():
                        self.job(name, status, conclusion)
            page, per = int((params or {}).get("page", 1)), int((params or {}).get("per_page", 100))
            return Response(200, {"jobs": self.jobs[(page - 1) * per : page * per]})
        if method == "POST" and path == f"{BASE}/actions/runs/{RUN}/cancel":
            self.cancelled.append(RUN)
            return Response(self.cancel_status, {})
        return Response(404, {})


def watch(run: Run, suites: list[str], **kw: Any) -> tuple[str, list[str]]:
    kw.setdefault("timeout", 20)  # short, so an implementation that never stops fails the test instead of spinning
    ticks = iter(range(100_000))  # a clock that moves a second per look, and no real sleeping
    return fail_fast(run, REPO, RUN, suites, interval=1, sleep=lambda _: None, clock=lambda: float(next(ticks)), **kw)  # type: ignore[arg-type]


# where each suite is -------------------------------------------------------------------------------------------------


def test_each_suite_is_waiting_running_passed_cancelled_failed_or_skipped_by_its_deepest_job() -> None:
    run = Run()
    run.job("caas / e2e", "in_progress")
    run.job("vmaas / e2e-vmaas-full-install / e2e", "completed", "success")
    run.job("bmaas / e2e", "completed", "failure")
    run.job("netris", "completed", "skipped")
    run.job("other / e2e", "completed", "cancelled")
    got = suite_progress(run, REPO, RUN, ["caas", "vmaas", "bmaas", "netris", "other", "late"])  # type: ignore[arg-type]
    assert got == {
        "caas": "running", "vmaas": "passed", "bmaas": "failed", "netris": "skipped", "other": "cancelled",
        "late": "waiting",
    }  # fmt: skip


@pytest.mark.parametrize("conclusion", ["failure", "timed_out", "startup_failure", "action_required"])
def test_every_way_of_not_passing_that_is_not_a_cancel_is_a_failure(conclusion: str) -> None:
    run = Run()
    run.job("caas / e2e", "completed", conclusion)
    assert suite_progress(run, REPO, RUN, ["caas"]) == {"caas": "failed"}  # type: ignore[arg-type]


def test_a_suite_whose_jobs_are_all_skipped_is_skipped_and_one_whose_job_is_queued_is_running() -> None:
    run = Run()
    run.job("a", "completed", "skipped")
    run.job("b", "queued")
    assert suite_progress(run, REPO, RUN, ["a", "b"]) == {"a": "skipped", "b": "running"}  # type: ignore[arg-type]


# stopping the run ---------------------------------------------------------------------------------------------------


def test_the_run_is_cancelled_as_soon_as_one_suite_has_failed_and_the_failed_suites_are_named() -> None:
    run = Run()
    run.job("vmaas / e2e", "in_progress")
    run.job("caas / e2e", "in_progress")
    run.job("bmaas / e2e", "completed", "failure")
    assert watch(run, ["vmaas", "caas", "bmaas"]) == ("stopped", ["bmaas"])
    assert run.cancelled == [RUN]


def test_a_failure_that_comes_later_stops_the_run_then() -> None:
    run = Run()
    run.job("vmaas / e2e", "in_progress")
    run.job("bmaas / e2e", "in_progress")
    run.then = [{"vmaas / e2e": ("in_progress", None)}, {"bmaas / e2e": ("completed", "failure")}]
    assert watch(run, ["vmaas", "bmaas"]) == ("stopped", ["bmaas"])
    assert run.looks > 2 and run.cancelled == [RUN]


def test_nothing_is_cancelled_while_the_suites_pass_and_the_watch_ends_when_they_have_all_ended() -> None:
    run = Run()
    run.job("vmaas / e2e", "in_progress")
    run.job("caas / e2e", "completed", "success")
    run.then = [{"vmaas / e2e": ("in_progress", None)}, {"vmaas / e2e": ("completed", "success")}]
    assert watch(run, ["vmaas", "caas"]) == ("done", [])
    assert run.cancelled == []


def test_a_suite_that_was_cancelled_by_someone_else_or_skipped_is_not_a_failure() -> None:
    run = Run()
    run.job("vmaas / e2e", "completed", "cancelled")
    run.job("caas / e2e", "completed", "skipped")
    run.job("bmaas / e2e", "completed", "success")
    assert watch(run, ["vmaas", "caas", "bmaas"]) == ("done", [])
    assert run.cancelled == []


def test_the_watch_gives_up_after_the_timeout_without_cancelling_anything() -> None:
    run = Run()
    run.job("vmaas / e2e", "in_progress")
    assert watch(run, ["vmaas"], timeout=5) == ("timeout", [])
    assert run.cancelled == []


def test_a_suite_that_has_no_job_yet_is_waited_for_not_taken_for_ended() -> None:
    run = Run()
    run.job("vmaas / e2e", "completed", "success")
    assert watch(run, ["vmaas", "caas"], timeout=3) == ("timeout", [])


def test_a_run_that_has_ended_already_is_not_an_error_but_a_refusal_to_cancel_is() -> None:
    ended = Run(cancel_status=409)
    ended.job("bmaas / e2e", "completed", "failure")
    assert watch(ended, ["bmaas"]) == ("stopped", ["bmaas"])
    refused = Run(cancel_status=403)
    refused.job("bmaas / e2e", "completed", "failure")
    with pytest.raises(Exception, match="cancelling the run"):
        watch(refused, ["bmaas"])


@pytest.mark.parametrize("suites", [[], ["vmaas; id"], ["../x"], [""]])
def test_a_list_that_is_not_suite_ids_is_refused_before_anything_is_asked(suites: list[str]) -> None:
    run = Run()
    with pytest.raises(ValueError, match="suite ids"):
        watch(run, suites)
    assert run.looks == 0 and run.cancelled == []


# the command --------------------------------------------------------------------------------------------------------


def test_the_command_prints_what_happened_and_a_bad_list_exits_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = Run()
    run.job("bmaas / e2e", "completed", "failure")
    monkeypatch.setattr(cli, "build_client", lambda **_: run)
    base = ["e2e-queue-fail-fast", "--repo", REPO, "--run-id", str(RUN), "--interval", "0", "--timeout", "3"]
    assert cli.main([*base, "--suites", '["bmaas"]']) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == {"result": "stopped", "failed": ["bmaas"]} and "bmaas failed" in out.err
    assert cli.main([*base, "--suites", "not json"]) == 3
    assert cli.main([*base, "--suites", '{"a": 1}']) == 3
    assert "e2e-queue-fail-fast failed" in capsys.readouterr().err
    assert cli.main([*base, "--suites", "[]"]) == 0 and json.loads(capsys.readouterr().out)["result"] == "done"


# the action and the workflow ------------------------------------------------------------------------------------------

ACTION = ROOT / ".github" / "actions" / "e2e-queue-fail-fast" / "action.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "e2e-queue-fail-fast.yml"
FINISH = ROOT / ".github" / "workflows" / "e2e-queue-finish.yml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_every_input_used_is_declared_nothing_is_expanded_into_the_script_and_nothing_is_checked_out() -> None:
    assert set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", ACTION.read_text(encoding="utf-8"))) == set(
        load(ACTION)["inputs"]
    )
    for step in load(ACTION)["runs"]["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")
        assert "checkout" not in step.get("uses", "")


def run_action(tmp_path: Path, **env: str) -> tuple[int, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(f"#!/bin/bash\necho \"$@\" >> '{tmp_path}/uv-args'\n")
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    script = next(s["run"] for s in load(ACTION)["runs"]["steps"] if s.get("name") == "Watch the suites")
    full = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_TOKEN": "t", "REPO": "o/r", "SUITES": '["vmaas"]',
            "RUN_ID": "7", "TIMEOUT": "60", "INTERVAL": "5", "ACTION_PATH": "/a/b/c", **env}  # fmt: skip
    done = subprocess.run(["bash", "-c", script], env=full, capture_output=True, text=True, check=False)
    args = tmp_path / "uv-args"
    return done.returncode, args.read_text() if args.exists() else ""


def test_the_action_passes_its_inputs_to_the_command(tmp_path: Path) -> None:
    code, args = run_action(tmp_path)
    assert code == 0
    assert 'e2e-queue-fail-fast --repo o/r --run-id 7 --suites ["vmaas"] --timeout 60 --interval 5' in args


@pytest.mark.parametrize("env", [{"RUN_ID": "7; id"}, {"RUN_ID": ""}, {"TIMEOUT": "1h"}, {"INTERVAL": "$(id)"}])
def test_a_value_that_is_not_digits_is_refused_before_the_command_runs(tmp_path: Path, env: dict[str, str]) -> None:
    code, args = run_action(tmp_path, **env)
    assert code == 1 and args == ""


def test_the_workflow_watches_with_the_one_permission_it_needs_and_checks_nothing_out() -> None:
    wf = load(WORKFLOW)
    assert wf["permissions"] == {} and wf["jobs"]["watch"]["permissions"] == {"actions": "write"}
    assert wf[True]["workflow_call"]["inputs"]["suites"]["required"] is True
    (step,) = wf["jobs"]["watch"]["steps"]
    assert step["uses"] == "$/.github/actions/e2e-queue-fail-fast"
    assert step["with"]["github-token"] == "${{ github.token }}" and step["with"]["repo"] == "${{ github.repository }}"
    assert "secrets" not in wf[True]["workflow_call"], "no secret: the built-in token cancels the run"
    assert "uses: actions/checkout" not in WORKFLOW.read_text(encoding="utf-8")


# what the check says after a stop ---------------------------------------------------------------------------


def summed(tmp_path: Path, results: dict[str, Any]) -> dict[str, str]:
    script = next(s["run"] for s in load(FINISH)["jobs"]["finish"]["steps"] if s.get("id") == "sum")
    out = tmp_path / "out"
    env = {**os.environ, "RESULTS": json.dumps(results), "PLAN": "plan", "GITHUB_OUTPUT": str(out)}
    done = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


PLAN = {"result": "success", "outputs": {"suites": '["vmaas","caas","bmaas"]'}}


def test_after_a_stop_the_check_is_a_failure_that_names_the_suite_that_failed_not_every_suite(tmp_path: Path) -> None:
    got = summed(tmp_path, {"plan": PLAN, "vmaas": {"result": "cancelled"}, "caas": {"result": "cancelled"},
                            "bmaas": {"result": "failure"}})  # fmt: skip
    assert got["result"] == "failure" and got["detail"] == "bmaas" and got["suites"] == "vmaas, caas, bmaas"


def test_several_failed_suites_are_all_named(tmp_path: Path) -> None:
    got = summed(tmp_path, {"plan": PLAN, "vmaas": {"result": "failure"}, "caas": {"result": "success"},
                            "bmaas": {"result": "failure"}})  # fmt: skip
    assert got["result"] == "failure" and got["detail"] == "vmaas, bmaas"


def test_a_pass_and_a_cancel_with_no_failure_name_every_suite_as_before(tmp_path: Path) -> None:
    ok = {"vmaas": {"result": "success"}, "caas": {"result": "success"}, "bmaas": {"result": "success"}}
    assert summed(tmp_path, {"plan": PLAN, **ok}) == {"suites": "vmaas, caas, bmaas", "result": "success",
                                                      "detail": "vmaas, caas, bmaas"}  # fmt: skip
    other = tmp_path / "c"
    other.mkdir()
    cancelled = summed(other, {"plan": PLAN, **{**ok, "caas": {"result": "cancelled"}}})
    assert cancelled["result"] == "cancelled" and cancelled["detail"] == "vmaas, caas, bmaas"


def test_a_plan_that_did_not_succeed_is_a_failure_with_the_suites_as_the_detail(tmp_path: Path) -> None:
    got = summed(tmp_path, {"plan": {"result": "failure", "outputs": {"suites": "[]"}}})
    assert got["result"] == "failure" and got["detail"] == ""
