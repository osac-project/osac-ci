"""A suite that ran as a run of its own has ended: its line gets the result and the E2E check is added up from the lines
of the suites the pull request needs. Also the sum when the run that started them ends."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from osac_ci import e2e_check, e2e_dispatch
from osac_ci.github.api import Response
from osac_ci.policy import parse_policy

pytestmark = pytest.mark.unit
HEAD = "a" * 40
VM = "E2E / VMaaS Full Install"
CA = "E2E / CaaS Full Install"
LINES = {"vmaas": VM, "caas": CA}
RUN_VM, RUN_CA, DECISION = 111, 222, 9
VM_TITLE = f"E2E suite · vmaas · #7 · {HEAD}"
VM_FILE = ".github/workflows/e2e-vmaas-full-install.yml"

POLICY = parse_policy(f"""
version: 1
repo: o/r
jobs:
  e2e-vmaas:
    kind: e2e
    check: "{VM}"
    suite: vmaas
    filters_any: [code]
    workflow: e2e-vmaas-full-install.yml
  e2e-caas:
    kind: e2e
    check: "{CA}"
    suite: caas
    filters_any: [code]
    workflow: e2e-caas-full-install.yml
path_filters:
  mode: enforce
  filters: {{code: ["**/*.go"]}}
""")


class Gh:
    """A pull request, its check runs (the lines and the E2E check) and the workflow runs, with what is written."""

    def __init__(self, head: str = HEAD) -> None:
        self.head = head
        self.checks: list[dict[str, Any]] = []
        self.runs: dict[int, tuple[str, str]] = {
            RUN_VM: ("completed", "success"),
            RUN_CA: ("in_progress", ""),
            DECISION: ("in_progress", ""),
        }
        self.files = ["main.go"]
        self.writes = 0

    def add(self, name: str, status: str, conclusion: str | None, run: int, title: str) -> None:
        self.checks.append(
            {
                "id": len(self.checks) + 1, "name": name, "status": status, "conclusion": conclusion,
                "started_at": f"2026-10-10T10:{len(self.checks):02d}:00Z", "app": {"slug": "osac-ci"},
                "output": {"title": title}, "details_url": f"https://github.com/o/r/actions/runs/{run}/job/1",
            }
        )  # fmt: skip

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path.endswith("/check-runs"):
            name = (params or {}).get("check_name")
            return Response(200, {"check_runs": [c for c in self.checks if name in (None, c["name"])]}, {})
        if method == "GET" and path.endswith("/statuses"):
            return Response(200, [], {})
        if method == "GET" and path == "/repos/o/r/pulls/7":
            return Response(200, {"head": {"sha": self.head}}, {})
        if method == "GET" and path == "/repos/o/r/pulls/7/files":
            return Response(200, [{"filename": f} for f in self.files], {})
        if method == "GET" and path.endswith("/jobs"):
            return Response(200, {"jobs": []}, {})
        if method == "GET" and "/actions/runs/" in path:
            status, conclusion = self.runs[int(path.rsplit("/", 1)[1])]
            return Response(200, {"status": status, "conclusion": conclusion or None}, {})
        if method == "POST" and path.endswith("/check-runs"):
            self.writes += 1
            self.checks.append(
                {
                    **body,
                    "id": len(self.checks) + 1,
                    "started_at": f"2026-10-10T11:{len(self.checks):02d}:00Z",
                    "app": {"slug": "osac-ci"},
                    "output": body["output"],
                }  # fmt: skip
            )
            return Response(201, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")

    def check(self) -> tuple[str, str] | None:
        e2e = [c for c in self.checks if c["name"] == "E2E"]
        if not e2e:
            return None
        last = e2e[-1]
        return (last.get("conclusion") or last["status"], last["output"]["title"])


def claim(gh: Gh, run: int = DECISION) -> None:
    gh.add("E2E", "in_progress", None, run, "Running: vmaas, caas")


def ended(gh: Gh, title: str = VM_TITLE, path: str = VM_FILE, run: int = RUN_VM) -> str:
    return e2e_dispatch.suite_ended(gh, "o/r", run, title, path, POLICY, lines_kind="checks")  # type: ignore[arg-type]


def test_the_last_suite_to_end_with_all_passed_makes_the_check_green() -> None:
    gh = Gh()
    claim(gh)
    gh.runs[DECISION] = ("completed", "success")
    gh.add(CA, "completed", "success", RUN_CA, "Passed")
    gh.add(VM, "in_progress", None, RUN_VM, "Running")
    ended(gh)
    assert gh.check() == ("success", "Passed: vmaas, caas")
    assert [c for c in gh.checks if c["name"] == VM][-1]["conclusion"] == "success"


def test_a_suite_that_ended_while_another_still_goes_leaves_the_check_waiting_for_it() -> None:
    gh = Gh()
    claim(gh)
    gh.runs[DECISION] = ("completed", "success")
    gh.add(CA, "in_progress", None, RUN_CA, "Running")
    gh.add(VM, "in_progress", None, RUN_VM, "Running")
    ended(gh)
    assert gh.check() == ("in_progress", "Waiting: caas running")
    assert [c for c in gh.checks if c["name"] == VM][-1]["conclusion"] == "success"


def test_a_failed_suite_makes_the_check_fail_once_nothing_goes() -> None:
    gh = Gh()
    claim(gh)
    gh.runs[DECISION] = ("completed", "success")
    gh.runs[RUN_VM] = ("completed", "failure")
    gh.add(CA, "completed", "success", RUN_CA, "Passed")
    gh.add(VM, "in_progress", None, RUN_VM, "Running")
    ended(gh)
    assert gh.check() == ("failure", "Failed: vmaas")


def test_the_claim_of_a_run_that_goes_is_left_alone_the_run_writes_its_own_answer() -> None:
    gh = Gh()
    claim(gh)  # the decision run is still going: its finish step adds up
    gh.add(CA, "completed", "success", RUN_CA, "Passed")
    gh.add(VM, "in_progress", None, RUN_VM, "Running")
    said = ended(gh)
    assert gh.check() == ("in_progress", "Running: vmaas, caas") and "already has its answer" in said


def test_a_suite_run_again_after_a_failure_turns_the_failed_check_green_when_it_passes() -> None:
    gh = Gh()
    gh.add(CA, "completed", "success", RUN_CA, "Passed")
    gh.add(VM, "completed", "failure", 5, "Failed")
    gh.add("E2E", "completed", "failure", DECISION, "Failed: vmaas")
    gh.add(VM, "in_progress", None, RUN_VM, "Running")
    ended(gh)
    assert gh.check() == ("success", "Passed: vmaas, caas")


def test_a_check_that_passed_stays() -> None:
    gh = Gh()
    gh.add(CA, "completed", "success", RUN_CA, "Passed")
    gh.add(VM, "in_progress", None, RUN_VM, "Running")
    gh.add("E2E", "completed", "success", DECISION, "Passed: vmaas, caas")
    ended(gh)
    assert gh.check() == ("success", "Passed: vmaas, caas") and gh.writes == 1  # only the suite's own line


def test_a_failure_that_was_already_final_stays_when_a_suite_fails_again() -> None:
    gh = Gh()
    gh.add(CA, "completed", "success", RUN_CA, "Passed")
    gh.add("E2E", "completed", "failure", DECISION, "Failed: caas")
    gh.add(VM, "in_progress", None, RUN_VM, "Running")
    gh.runs[RUN_VM] = ("completed", "failure")
    ended(gh)
    assert gh.check() == ("failure", "Failed: caas")


def test_a_cancelled_suite_is_a_suite_that_did_not_pass() -> None:
    gh = Gh()
    claim(gh)
    gh.runs[DECISION] = ("completed", "success")
    gh.runs[RUN_VM] = ("completed", "cancelled")
    gh.add(CA, "completed", "success", RUN_CA, "Passed")
    gh.add(VM, "in_progress", None, RUN_VM, "Running")
    ended(gh)
    assert gh.check() == ("failure", "Failed: vmaas")


def test_a_line_that_a_newer_run_of_the_suite_owns_is_left_alone() -> None:
    gh = Gh()
    claim(gh)
    gh.add(VM, "in_progress", None, 333, "Running")
    said = ended(gh)
    assert gh.writes == 0 and "newer run" in said


def test_a_pull_request_that_moved_on_is_not_touched() -> None:
    gh = Gh(head="b" * 40)
    claim(gh)
    said = ended(gh)
    assert gh.writes == 0 and "another commit" in said


@pytest.mark.parametrize(
    "title",
    [
        "Some other run",
        f"E2E suite · vmaas · #7 · {HEAD[:39]}",
        f"E2E suite · vmaas · #7 · {HEAD}\nmore",
        f"E2E suite · vmaas · #x · {HEAD}",
        f"E2E suite · Vmaas · #7 · {HEAD}",
    ],
)
def test_a_run_whose_name_is_not_the_one_the_workflow_gives_is_ignored(title: str) -> None:
    gh = Gh()
    assert "not the run of a suite" in ended(gh, title) and gh.writes == 0


def test_a_run_of_another_workflow_cannot_speak_for_a_suite() -> None:
    gh = Gh()
    said = ended(gh, path=".github/workflows/e2e-caas-full-install.yml")
    assert gh.writes == 0 and "not the workflow of the suite" in said


def test_a_suite_the_policy_does_not_know_is_ignored() -> None:
    gh = Gh()
    title = f"E2E suite · nothere · #7 · {HEAD}"
    assert "not the workflow of the suite" in ended(gh, title) and gh.writes == 0


def test_a_run_that_has_not_ended_writes_nothing() -> None:
    gh = Gh()
    gh.runs[RUN_VM] = ("in_progress", "")
    assert "has not ended" in ended(gh) and gh.writes == 0


# the run that started the suites ----------------------------------------------------------------------------------


def finish(gh: Gh, result: str = "success") -> str:
    url = f"https://github.com/o/r/actions/runs/{DECISION}"
    return e2e_check.finish_with_dispatched(gh, "o/r", HEAD, result, url, "alpha", LINES, "checks")  # type: ignore[arg-type]


def test_when_the_run_ends_with_a_suite_still_going_the_check_waits_for_it() -> None:
    gh = Gh()
    claim(gh)
    gh.add(VM, "completed", "success", RUN_VM, "Passed")
    gh.add(CA, "in_progress", None, RUN_CA, "Running")
    assert finish(gh) == "waiting" and gh.check() == ("in_progress", "Waiting: caas running")


def test_when_the_run_ends_and_every_suite_passed_the_check_is_green() -> None:
    gh = Gh()
    claim(gh)
    gh.add(VM, "completed", "success", RUN_VM, "Passed")
    gh.add(CA, "completed", "success", RUN_CA, "Passed")
    assert finish(gh) == "passed" and gh.check() == ("success", "Passed: vmaas, caas")


def test_a_suite_that_failed_before_the_run_ended_fails_the_check() -> None:
    gh = Gh()
    claim(gh)
    gh.add(VM, "completed", "failure", RUN_VM, "Failed")
    gh.add(CA, "completed", "success", RUN_CA, "Passed")
    assert finish(gh) == "failure" and gh.check() == ("failure", "Failed: vmaas")


def test_a_failure_of_the_run_itself_still_waits_for_a_suite_run_again() -> None:
    gh = Gh()
    claim(gh)
    gh.add(VM, "in_progress", None, RUN_CA, "Running")
    gh.add(CA, "completed", "success", RUN_CA, "Passed")
    gh.runs[RUN_CA] = ("in_progress", "")
    assert finish(gh, "failure") == "waiting" and gh.check() == ("in_progress", "Waiting: vmaas running again")


def test_a_cancel_is_written_as_a_cancel() -> None:
    gh = Gh()
    claim(gh)
    gh.add(VM, "in_progress", None, RUN_VM, "Running")
    gh.add(CA, "completed", "success", RUN_CA, "Passed")
    assert finish(gh, "cancelled") == "cancelled" and gh.check() == ("cancelled", "Cancelled: alpha")


def test_a_wait_that_is_already_written_is_not_written_again() -> None:
    gh = Gh()
    claim(gh)
    gh.add(VM, "completed", "success", RUN_VM, "Passed")
    gh.add(CA, "in_progress", None, RUN_CA, "Running")
    finish(gh)
    before = gh.writes
    finish(gh)
    assert gh.writes == before


def test_a_final_failure_is_not_turned_into_a_wait_by_a_suite_that_is_run_again() -> None:
    gh = Gh()
    gh.add(CA, "in_progress", None, RUN_CA, "Running")
    gh.add("E2E", "completed", "failure", DECISION, "Failed: caas")
    gh.add(VM, "in_progress", None, RUN_VM, "Running")
    ended(gh)
    assert gh.check() == ("failure", "Failed: caas")


def test_a_suite_with_no_line_is_not_a_failure_the_check_is_left_as_it_is() -> None:
    gh = Gh()
    claim(gh)
    gh.runs[DECISION] = ("completed", "success")
    gh.add(VM, "in_progress", None, RUN_VM, "Running")  # caas has no line at all
    said = ended(gh)
    assert gh.check() == ("in_progress", "Running: vmaas, caas") and "no line yet" in said


def test_a_failed_suite_fails_the_check_naming_only_the_failed_one_even_if_another_has_no_line() -> None:
    gh = Gh()
    claim(gh)
    gh.runs[DECISION] = ("completed", "success")
    gh.runs[RUN_VM] = ("completed", "failure")
    gh.add(VM, "in_progress", None, RUN_VM, "Running")
    ended(gh)
    assert gh.check() == ("failure", "Failed: vmaas")
