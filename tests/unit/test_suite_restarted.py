"""A suite that had ended is going again (GitHub's "Re-run"): its line and the E2E check say so, once."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from test_suite_ended import CA, DECISION, HEAD, POLICY, RUN_CA, RUN_VM, VM, VM_FILE, VM_TITLE, Gh, claim

from osac_ci import e2e_dispatch
from osac_ci.github.api import Response

pytestmark = pytest.mark.unit
JOB = f"https://github.com/o/r/actions/runs/{RUN_VM}/job/900"  # the job of the new attempt


class GhWithJobs(Gh):
    """The same pull request, and the jobs of the run as the new attempt has them."""

    def __init__(self) -> None:
        super().__init__()
        self.runs[RUN_VM] = ("in_progress", "")
        running = {"name": "vmaas / e2e-vmaas-full-install / e2e", "status": "in_progress", "html_url": JOB}
        self.jobs: list[dict[str, Any]] = [{**running, "started_at": "2026-10-10T12:00:00Z"}]

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path.endswith(f"/actions/runs/{RUN_VM}/jobs"):
            return Response(200, {"jobs": self.jobs}, {})
        return super().request(method, path, params=params, body=body)

    def line(self, name: str) -> tuple[str, str, str]:
        last = [c for c in self.checks if c["name"] == name][-1]
        return (last.get("conclusion") or last["status"], last["output"]["title"], last["output"]["summary"])


class GhWithStatuses(GhWithJobs):
    """The same, with the lines of the suites kept as commit statuses (newest first, as GitHub lists them), which is how
    osac writes them; the E2E check stays a check run."""

    def __init__(self) -> None:
        super().__init__()
        self.statuses: list[dict[str, Any]] = []

    def status(self, context: str, state: str, run: int, text: str) -> None:
        self.statuses.insert(
            0,
            {
                "context": context, "state": state, "description": text, "creator": {"login": "osac-ci[bot]"},
                "target_url": f"https://github.com/o/r/actions/runs/{run}/job/1",
                "created_at": f"2026-10-10T10:{len(self.statuses):02d}:00Z",
            },
        )  # fmt: skip

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path.endswith("/statuses"):
            return Response(200, self.statuses, {})
        if method == "POST" and path == f"/repos/o/r/statuses/{HEAD}":
            self.writes += 1
            self.status(body["context"], body["state"], RUN_VM, body.get("description", ""))
            self.statuses[0]["target_url"] = body.get("target_url", "")
            return Response(201, {}, {})
        return super().request(method, path, params=params, body=body)

    def line_status(self, name: str) -> tuple[str, str, str]:
        last = next(s for s in self.statuses if s["context"] == name)
        return last["state"], last["description"], last["target_url"]


def restarted(gh: Gh, title: str = VM_TITLE, path: str = VM_FILE, kind: str = "checks") -> str:
    return e2e_dispatch.suite_restarted(gh, "o/r", RUN_VM, title, path, POLICY, lines_kind=kind)  # type: ignore[arg-type]


def failed_before(gh: GhWithJobs, e2e: tuple[str, str] = ("failure", "Failed: vmaas")) -> None:
    """The first attempt failed: the line and the E2E check say so, and the line links to a job of that attempt."""
    gh.add(CA, "completed", "success", RUN_CA, "Passed")
    gh.add(VM, "completed", "failure", RUN_VM, "Failed")
    gh.add("E2E", "completed", e2e[0], DECISION, e2e[1])
    gh.runs[DECISION] = ("completed", "failure")


def test_a_rerun_shows_on_the_line_and_the_failed_check_waits_for_it() -> None:
    gh = GhWithJobs()
    failed_before(gh)
    restarted(gh)
    status, title, summary = gh.line(VM)
    assert (status, title) == ("in_progress", "Running") and JOB in summary  # the job of the new attempt, not the first
    assert gh.check() == ("in_progress", "Waiting: vmaas running again")


def test_a_rerun_with_the_lines_as_commit_statuses_shows_the_new_attempt_and_waits() -> None:
    gh = GhWithStatuses()
    gh.status(CA, "success", RUN_CA, "Passed")
    gh.status(VM, "failure", RUN_VM, "Failed")  # the first attempt, linked to its own job
    gh.add("E2E", "completed", "failure", DECISION, "Failed: vmaas")
    gh.runs[DECISION] = ("completed", "failure")
    restarted(gh, kind="statuses")
    assert gh.line_status(VM) == ("pending", "Running", JOB)
    assert gh.check() == ("in_progress", "Waiting: vmaas running again")
    writes = gh.writes
    restarted(gh, kind="statuses")
    assert gh.writes == writes


def test_the_second_event_of_the_same_attempt_changes_nothing() -> None:
    gh = GhWithJobs()
    failed_before(gh)
    restarted(gh)
    writes = gh.writes
    restarted(gh)
    assert gh.writes == writes


def test_a_suite_that_is_waiting_for_a_runner_is_a_queued_line() -> None:
    gh = GhWithJobs()
    gh.jobs = []
    failed_before(gh)
    restarted(gh)
    assert gh.line(VM)[:2] == ("queued", "Waiting for a runner")
    assert gh.check() == ("in_progress", "Waiting: vmaas running again")


def test_another_suite_that_goes_is_named_with_it() -> None:
    gh = GhWithJobs()
    failed_before(gh)
    gh.add(CA, "in_progress", None, RUN_CA, "Running")
    restarted(gh)
    assert gh.check() == ("in_progress", "Waiting: vmaas, caas running again")


def test_the_end_of_the_attempt_turns_the_check_green_when_it_passes() -> None:
    gh = GhWithJobs()
    failed_before(gh)
    restarted(gh)
    gh.runs[RUN_VM] = ("completed", "success")
    e2e_dispatch.suite_ended(gh, "o/r", RUN_VM, VM_TITLE, VM_FILE, POLICY, lines_kind="checks")  # type: ignore[arg-type]
    assert gh.check() == ("success", "Passed: vmaas, caas")


def test_a_check_that_passed_stays_and_the_line_still_shows_the_new_attempt() -> None:
    gh = GhWithJobs()
    failed_before(gh, ("success", "Passed: vmaas, caas"))
    restarted(gh)
    assert gh.check() == ("success", "Passed: vmaas, caas")
    assert gh.line(VM)[:2] == ("in_progress", "Running")


@pytest.mark.parametrize(
    "waiting", ["Waiting for an approval: nobody has approved the head commit", "By hand: vmaas running"]
)
def test_a_check_that_waits_for_something_else_keeps_its_reason(waiting: str) -> None:
    gh = GhWithJobs()
    gh.add(VM, "completed", "failure", RUN_VM, "Failed")
    gh.add("E2E", "in_progress", None, DECISION, waiting)
    restarted(gh)
    assert gh.check() == ("in_progress", waiting)
    assert gh.line(VM)[:2] == ("in_progress", "Running")


def test_the_claim_of_a_run_that_goes_is_left_alone() -> None:
    gh = GhWithJobs()
    gh.add(CA, "completed", "success", RUN_CA, "Passed")
    gh.add(VM, "completed", "failure", RUN_VM, "Failed")
    claim(gh)  # the decision run is still going: its finish step adds up
    restarted(gh)
    assert gh.check() == ("in_progress", "Running: vmaas, caas")


def test_a_run_that_is_already_over_is_left_to_its_end() -> None:
    gh = GhWithJobs()
    failed_before(gh)
    gh.runs[RUN_VM] = ("completed", "failure")
    writes = gh.writes
    said = restarted(gh)
    assert gh.writes == writes and "already over" in said


def test_a_line_that_a_newer_run_of_the_suite_owns_is_left_alone() -> None:
    gh = GhWithJobs()
    failed_before(gh)
    gh.add(VM, "in_progress", None, 333, "Running")
    writes = gh.writes
    said = restarted(gh)
    assert gh.writes == writes and "newer run" in said


def test_a_commit_that_is_no_longer_the_head_is_left_alone() -> None:
    gh = GhWithJobs()
    failed_before(gh)
    gh.head = "b" * 40
    writes = gh.writes
    said = restarted(gh)
    assert gh.writes == writes and "another commit" in said


@pytest.mark.parametrize(
    ("title", "path"),
    [
        ("E2E · osac-project:main", VM_FILE),  # not the name a suite gives its run
        (f"E2E suite · vmaas · #7 · {HEAD}", ".github/workflows/e2e-caas-full-install.yml"),  # not the suite's workflow
        (f"E2E suite · nothing · #7 · {HEAD}", VM_FILE),  # a suite the policy does not have
    ],
)
def test_what_is_not_the_run_of_a_suite_is_ignored(title: str, path: str) -> None:
    gh = GhWithJobs()
    failed_before(gh)
    writes = gh.writes
    restarted(gh, title, path)
    assert gh.writes == writes


def test_a_suite_the_change_does_not_need_shows_its_line_and_leaves_the_check_alone() -> None:
    gh = GhWithJobs()
    failed_before(gh, ("failure", "Failed: caas"))
    gh.files = ["README.md"]  # the policy's filter for the suites is **/*.go
    said = restarted(gh)
    assert gh.line(VM)[:2] == ("in_progress", "Running")
    assert gh.check() == ("failure", "Failed: caas") and "does not need this suite" in said


def test_without_an_e2e_check_only_the_line_is_written() -> None:
    gh = GhWithJobs()
    gh.add(VM, "completed", "failure", RUN_VM, "Failed")
    said = restarted(gh)
    assert gh.check() is None and gh.line(VM)[:2] == ("in_progress", "Running") and "has not started" in said
