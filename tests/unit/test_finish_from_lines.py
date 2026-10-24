"""After a run that failed: the lines of its suites decide what the E2E check says, because a suite of the run may have
been run again since, by hand, in a run of its own."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest
import yaml
from helpers import ROOT

from osac_ci import cli, e2e_check
from osac_ci.github.api import Response

pytestmark = pytest.mark.unit
REPO = "o/r"
HEAD = "a" * 40
LINES = {"vmaas": "E2E / VMaaS Full Install", "caas": "E2E / CaaS Full Install"}


def line(name: str, status: str, conclusion: str | None, run: int, title: str = "", at: str = "10:00") -> dict:
    return {
        "name": name, "status": status, "conclusion": conclusion, "started_at": f"2026-10-10T{at}:00Z",
        "app": {"slug": "osac-ci"}, "output": {"title": title},
        "details_url": f"https://github.com/o/r/actions/runs/{run}/job/1",
    }  # fmt: skip


def passed(name: str, run: int = 1, title: str = "Passed", at: str = "10:00") -> dict:
    return line(name, "completed", "success", run, title, at)


def failed(name: str, run: int = 1, at: str = "10:00") -> dict:
    return line(name, "completed", "failure", run, "Failed", at)


def running(name: str, run: int, at: str = "10:30") -> dict:
    return line(name, "in_progress", None, run, "Running", at)


class Fake:
    """The lines of a commit, the status of workflow runs (a run that is absent is gone) and what is written."""

    def __init__(self, lines: list[dict[str, Any]], alive: Mapping[int, str] | None = None) -> None:
        self.lines = lines
        self.alive = dict(alive or {})
        self.posted: list[Mapping[str, Any]] = []

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path.endswith("/check-runs"):
            return Response(200, {"check_runs": self.lines}, {})
        if method == "GET" and path.endswith("/statuses"):
            return Response(200, [], {})
        if method == "GET" and "/actions/runs/" in path:
            run = int(path.rsplit("/", 1)[1])
            return Response(200, {"status": self.alive[run]}, {}) if run in self.alive else Response(404, {}, {})
        if method == "POST" and path.endswith("/check-runs"):
            self.posted.append(body)
            return Response(201, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")


def finish(fake: Fake, result: str = "failure", suite_checks: Mapping[str, str] | None = None) -> str:
    checks = LINES if suite_checks is None else suite_checks
    return e2e_check.finish_from_lines(fake, REPO, HEAD, result, "", "vmaas, caas", checks, "checks")  # type: ignore[arg-type]


def written(fake: Fake) -> tuple[str, str]:
    (post,) = fake.posted
    state = post.get("conclusion") or post["status"]
    return state, post["output"]["title"]


def test_a_suite_that_failed_in_the_run_and_passed_when_run_again_makes_the_check_green() -> None:
    fake = Fake([failed(LINES["vmaas"], run=10), passed(LINES["vmaas"], run=20, at="10:40"), passed(LINES["caas"])])
    assert finish(fake) == "passed" and written(fake) == ("success", "Passed: vmaas, caas")


def test_the_version_a_suite_passed_with_is_named() -> None:
    fake = Fake([passed(LINES["vmaas"]), passed(LINES["caas"], title="Passed · full")])
    assert finish(fake) == "passed" and written(fake)[1] == "Passed: vmaas, caas (full)"


def test_a_suite_that_is_being_run_again_makes_the_check_wait_for_that_run() -> None:
    fake = Fake(
        [failed(LINES["vmaas"], run=10), running(LINES["vmaas"], run=20), passed(LINES["caas"])], {20: "in_progress"}
    )
    assert finish(fake) == "waiting"
    assert written(fake) == ("queued", "Waiting: vmaas running again"), (
        "a waiting check: the run that ends writes the answer"
    )


def test_a_line_that_says_running_in_a_run_that_is_gone_is_not_waited_for() -> None:
    fake = Fake([failed(LINES["vmaas"], run=10), running(LINES["vmaas"], run=20), passed(LINES["caas"])], {})
    assert finish(fake) == "failure" and written(fake) == ("failure", "Failed: vmaas, caas")


def test_a_suite_whose_latest_line_failed_keeps_the_check_failed() -> None:
    fake = Fake([passed(LINES["vmaas"], at="10:00"), failed(LINES["vmaas"], run=20, at="10:40"), passed(LINES["caas"])])
    assert finish(fake) == "failure" and written(fake)[0] == "failure"


def test_a_suite_with_no_line_at_all_is_not_a_pass() -> None:
    fake = Fake([passed(LINES["caas"])])
    assert finish(fake) == "failure"


def test_a_run_that_was_cancelled_stays_cancelled_whatever_the_lines_say() -> None:
    fake = Fake([passed(LINES["vmaas"]), passed(LINES["caas"])])
    assert finish(fake, "cancelled") == "cancelled" and written(fake) == ("cancelled", "Cancelled: vmaas, caas")


def test_a_run_that_passed_writes_its_pass_even_while_a_suite_is_being_run_again() -> None:
    fake = Fake([passed(LINES["vmaas"]), running(LINES["vmaas"], run=20), passed(LINES["caas"])], {20: "in_progress"})
    assert finish(fake, "success") == "success" and written(fake) == ("success", "Passed: vmaas, caas")


def test_without_the_suites_of_the_run_the_result_is_written_as_before() -> None:
    fake = Fake([passed(LINES["vmaas"]), passed(LINES["caas"])])
    assert finish(fake, suite_checks={}) == "failure" and written(fake) == ("failure", "Failed: vmaas, caas")


def test_the_cli_option_must_be_a_json_object(capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(["e2e-finish", "--repo", REPO, "--sha", HEAD, "--result", "failure", "--suite-checks", "[1]"])
    assert code == 3 and "must be a JSON object" in capsys.readouterr().err


def load(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def test_the_result_job_settles_the_lines_before_the_check_is_written_and_passes_the_suites_of_the_run() -> None:
    steps = load(".github/workflows/e2e-pr-finish.yml")["jobs"]["finish"]["steps"]
    uses = [s.get("uses", "") for s in steps]
    settle = uses.index("$/.github/actions/e2e-suites-settle")
    plain = next(
        i
        for i, s in enumerate(steps)
        if s.get("uses") == "$/.github/actions/e2e-finish" and s.get("if") == "inputs.manual != 'true'"
    )
    assert settle < plain, "the check is written from the lines, so the lines come first"
    assert steps[plain]["with"]["suite-checks"] == "${{ inputs.suite-checks }}"


def test_the_action_hands_the_suites_to_the_program_only_for_a_run_that_was_not_started_by_hand() -> None:
    text = (ROOT / ".github/actions/e2e-finish/action.yml").read_text(encoding="utf-8")
    assert load(".github/actions/e2e-finish/action.yml")["inputs"]["suite-checks"]["default"] == "{}"
    assert "SUITE_CHECKS: ${{ inputs.suite-checks }}" in text
    assert '[ "${MANUAL:-false}" != "true" ] && [ -n "${SUITE_CHECKS:-}" ]' in text
    assert 'args+=("--suite-checks=$SUITE_CHECKS")' in text
    assert json.loads('{"vmaas": "E2E / VMaaS Full Install"}')  # the value is JSON
