"""The lines of suites that run as a run of their own follow that run: waiting for a runner, running, then its result by
how the run ended; a line a newer run has taken over is left alone."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from osac_ci import e2e_suites_check as tracker
from osac_ci.github.api import GitHubError, Response, TransportError

pytestmark = pytest.mark.unit
HEAD = "a" * 40
VM = "E2E / VMaaS Full Install"
CA = "E2E / CaaS Full Install"
RUN_VM, RUN_CA = 111, 222


class Gh:
    """Workflow runs (status, conclusion, jobs) and the commit statuses that are written (newest first)."""

    def __init__(self) -> None:
        self.runs: dict[int, tuple[str, str]] = {RUN_VM: ("queued", ""), RUN_CA: ("queued", "")}
        self.jobs: dict[int, list[dict[str, Any]]] = {RUN_VM: [], RUN_CA: [], 9: []}  # 9: the run of the decision
        self.statuses: list[dict[str, Any]] = []

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and "/actions/runs/" in path and path.endswith("/jobs"):
            rid = int(path.split("/runs/")[1].split("/")[0])
            mine = [{**j, "html_url": j["html_url"].replace("/runs/1/", f"/runs/{rid}/")} for j in self.jobs[rid]]
            return Response(200, {"jobs": mine}, {})
        if method == "GET" and "/actions/runs/" in path:
            status, conclusion = self.runs[int(path.rsplit("/", 1)[1])]
            return Response(200, {"status": status, "conclusion": conclusion or None}, {})
        if method == "GET" and path.endswith("/statuses"):
            return Response(200, self.statuses, {})
        if method == "GET" and path.endswith("/check-runs"):
            return Response(200, {"check_runs": []}, {})
        if method == "POST" and "/statuses/" in path:
            self.statuses.insert(0, {**body, "creator": {"login": "github-actions[bot]"}})
            return Response(201, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")


def job(name: str, status: str, conclusion: str | None = None, started: str = "10:00") -> dict[str, Any]:
    return {"name": name, "status": status, "conclusion": conclusion, "started_at": f"2026-10-10T{started}:00Z",
            "html_url": f"https://github.com/o/r/actions/runs/1/job/{abs(hash(name)) % 1000}"}  # fmt: skip


def says(gh: Gh, line: str) -> tuple[str, str]:
    newest = next(s for s in gh.statuses if s["context"] == line)
    return newest["state"], newest["description"]


def follow(gh: Gh, *, follow_to_the_end: bool = True, **more: Any) -> list[str]:
    runs = {"vmaas": (RUN_VM, VM), "caas": (RUN_CA, CA)}
    more.setdefault("sleep", lambda _: None)
    return tracker.track(  # type: ignore[arg-type]
        gh, "o/r", HEAD, 9, "https://x/9", {}, kind="statuses", follow=follow_to_the_end, runs=runs,
        clock=lambda: 0.0, **more,
    )  # fmt: skip


def test_a_run_that_has_not_started_a_job_is_waiting_for_a_runner_and_links_to_the_run() -> None:
    gh = Gh()
    states = [tracker.run_state(gh, "o/r", RUN_VM)]  # type: ignore[arg-type]
    assert states == [("Waiting for a runner", "waiting", "https://github.com/o/r/actions/runs/111", False)]


def test_a_run_with_a_job_going_is_running_and_links_to_the_deepest_job_that_ran() -> None:
    gh = Gh()
    gh.runs[RUN_VM] = ("in_progress", "")
    gh.jobs[RUN_VM] = [job("e2e-vmaas", "completed", "success"), job("e2e-vmaas / e2e", "in_progress")]
    description, state, url, over = tracker.run_state(gh, "o/r", RUN_VM)  # type: ignore[arg-type]
    assert (description, state, over) == ("Running", "pending", False)
    assert url.endswith(f"/job/{abs(hash('e2e-vmaas / e2e')) % 1000}")


@pytest.mark.parametrize(
    ("conclusion", "title", "state"),
    [("success", "Passed", "success"), ("failure", "Failed", "failure"), ("cancelled", "Cancelled", "error"),
     ("timed_out", "Failed", "failure"), ("startup_failure", "Failed", "failure")],
)  # fmt: skip
def test_a_run_that_is_over_says_how_it_ended(conclusion: str, title: str, state: str) -> None:
    gh = Gh()
    gh.runs[RUN_VM] = ("completed", conclusion)
    gh.jobs[RUN_VM] = [job("e2e", "completed", conclusion)]
    description, got, _, over = tracker.run_state(gh, "o/r", RUN_VM)  # type: ignore[arg-type]
    assert (description, got, over) == (title, state, True)


def test_the_lines_of_two_runs_follow_each_run_and_the_loop_ends_when_both_are_over() -> None:
    gh = Gh()
    gh.runs = {RUN_VM: ("completed", "success"), RUN_CA: ("completed", "failure")}
    gh.jobs.update({RUN_VM: [job("e2e", "completed", "success")], RUN_CA: [job("e2e", "completed", "failure")]})
    assert sorted(follow(gh)) == ["caas", "vmaas"]
    assert says(gh, VM) == ("success", "Passed") and says(gh, CA) == ("failure", "Failed")


def test_a_line_is_written_only_when_it_changes() -> None:
    gh = Gh()
    gh.runs = {RUN_VM: ("completed", "success"), RUN_CA: ("completed", "success")}
    gh.jobs.update({RUN_VM: [job("e2e", "completed", "success")], RUN_CA: [job("e2e", "completed", "success")]})
    follow(gh)
    assert len(gh.statuses) == 2


def test_a_line_that_a_newer_run_has_taken_over_is_left_alone() -> None:
    gh = Gh()
    gh.statuses = [{"context": VM, "state": "pending", "description": "Running", "target_url": "https://github.com/o/r/actions/runs/333/job/1",
                    "creator": {"login": "github-actions[bot]"}}]  # fmt: skip
    gh.runs = {RUN_VM: ("completed", "failure"), RUN_CA: ("completed", "success")}
    gh.jobs.update({RUN_VM: [job("e2e", "completed", "failure")], RUN_CA: [job("e2e", "completed", "success")]})
    follow(gh)
    assert says(gh, VM) == ("pending", "Running"), (
        "the retest in run 333 owns the line; the old run does not overwrite it"
    )
    assert says(gh, CA) == ("success", "Passed")


def test_the_version_a_suite_was_asked_for_is_kept_on_its_line() -> None:
    gh = Gh()
    gh.statuses = [{"context": VM, "state": "pending", "description": "Waiting for a runner · full", "target_url": "https://github.com/o/r/actions/runs/111",
                    "creator": {"login": "github-actions[bot]"}}]  # fmt: skip
    gh.runs = {RUN_VM: ("completed", "success"), RUN_CA: ("completed", "success")}
    gh.jobs.update({RUN_VM: [job("e2e", "completed", "success")], RUN_CA: [job("e2e", "completed", "success")]})
    follow(gh)
    assert says(gh, VM) == ("success", "Passed · full")


def test_without_follow_the_loop_ends_once_the_runs_are_running_not_when_they_are_over() -> None:
    gh = Gh()
    gh.runs = {RUN_VM: ("in_progress", ""), RUN_CA: ("in_progress", "")}
    gh.jobs.update({RUN_VM: [job("e2e", "in_progress")], RUN_CA: [job("e2e", "in_progress")]})
    assert sorted(follow(gh, follow_to_the_end=False)) == ["caas", "vmaas"]


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "3",
        '{"vmaas": 3}',
        '{"vmaas": {"run": 0, "check": "E2E / VMaaS"}}',
        '{"vmaas": {"run": true, "check": "E2E / V"}}',
        '{"vmaas": {"run": 5, "check": "E2E"}}',
        '{"VMAAS": {"run": 5, "check": "E2E / V"}}',
        "not json",
    ],
)
def test_runs_that_are_not_what_they_should_be_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        tracker.parse_runs(text)


def test_runs_as_e2e_dispatch_writes_them_are_read() -> None:
    assert tracker.parse_runs('{"vmaas": {"run": 111, "check": "E2E / VMaaS Full Install"}}') == {
        "vmaas": (111, "E2E / VMaaS Full Install")
    }
    assert tracker.parse_runs("{}") == {}


def test_the_loop_goes_on_while_a_run_is_not_over_and_writes_each_step_it_goes_through() -> None:
    gh = Gh()
    gh.runs = {RUN_VM: ("queued", ""), RUN_CA: ("in_progress", "")}
    gh.jobs.update({RUN_VM: [], RUN_CA: [job("e2e", "in_progress")]})
    steps = iter(
        [
            {RUN_VM: ("in_progress", ""), RUN_CA: ("in_progress", "")},
            {RUN_VM: ("completed", "success"), RUN_CA: ("completed", "failure")},
        ]
    )

    def sleep(_: float) -> None:
        gh.runs = next(steps)
        gh.jobs[RUN_VM] = [job("e2e", "in_progress")]
        if gh.runs[RUN_VM][0] == "completed":
            gh.jobs[RUN_VM] = [job("e2e", "completed", "success")]
            gh.jobs[RUN_CA] = [job("e2e", "completed", "failure")]

    follow(gh, sleep=sleep)
    assert [s["description"] for s in gh.statuses if s["context"] == VM] == [
        "Passed",
        "Running",
        "Waiting for a runner",
    ]
    assert says(gh, CA) == ("failure", "Failed")


class Dropping(Gh):
    """A GitHub whose reads of the jobs of a run fail the first ``fail`` times (a dropped connection)."""

    def __init__(self, fail: int, status: int = 0) -> None:
        super().__init__()
        self.fail, self.status, self.dropped = fail, status, 0

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path.endswith("/jobs") and self.dropped < self.fail:
            self.dropped += 1
            if self.status == 0:
                raise TransportError(f"GET {path}: RemoteDisconnected")
            raise GitHubError(self.status, f"GET {path}")
        return super().request(method, path, params=params, body=body)


def _both_passed(gh: Gh) -> None:
    gh.runs = {RUN_VM: ("completed", "success"), RUN_CA: ("completed", "success")}
    gh.jobs.update({RUN_VM: [job("e2e", "completed", "success")], RUN_CA: [job("e2e", "completed", "success")]})


def test_a_poll_that_fails_in_transit_is_tried_again_on_the_next_tick() -> None:
    gh = Dropping(fail=3)
    _both_passed(gh)
    waits: list[float] = []
    assert sorted(follow(gh, sleep=waits.append)) == ["caas", "vmaas"]
    assert gh.dropped == 3 and len(waits) == 3
    assert says(gh, VM) == ("success", "Passed")


def test_polls_that_keep_failing_in_transit_end_the_follower_with_the_error() -> None:
    gh = Dropping(fail=100)
    _both_passed(gh)
    with pytest.raises(GitHubError):
        follow(gh)
    assert gh.dropped == tracker.TOLERATED_FAILURES + 1


def test_the_count_of_failures_in_a_row_starts_again_after_a_poll_that_works() -> None:
    gh = Dropping(fail=tracker.TOLERATED_FAILURES)
    gh.runs = {RUN_VM: ("in_progress", ""), RUN_CA: ("in_progress", "")}
    gh.jobs.update({RUN_VM: [job("e2e", "in_progress")], RUN_CA: [job("e2e", "in_progress")]})
    ticks = 0

    def sleep(_: float) -> None:
        nonlocal ticks
        ticks += 1
        if ticks == tracker.TOLERATED_FAILURES + 1:  # one poll worked in between: the run goes on and then ends
            gh.fail = gh.dropped + tracker.TOLERATED_FAILURES
        if ticks == 2 * tracker.TOLERATED_FAILURES + 1:
            _both_passed(gh)

    assert sorted(follow(gh, sleep=sleep)) == ["caas", "vmaas"]


@pytest.mark.parametrize("status", [401, 403, 404, 422])
def test_a_refusal_is_not_tried_again(status: int) -> None:
    gh = Dropping(fail=100, status=status)
    with pytest.raises(GitHubError):
        follow(gh)
    assert gh.dropped == 1


class LosingWrites(Gh):
    """A GitHub whose first write of a line goes through but whose answer is lost (a dropped connection)."""

    def __init__(self) -> None:
        super().__init__()
        self.posts = 0

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "POST" and "/statuses/" in path:
            self.posts += 1
            super().request(method, path, params=params, body=body)
            if self.posts == 1:
                raise TransportError(f"POST {path}: RemoteDisconnected")
            return Response(201, {}, {})
        return super().request(method, path, params=params, body=body)


def test_a_write_that_failed_is_not_written_again_by_another_poll() -> None:
    gh = LosingWrites()
    _both_passed(gh)
    with pytest.raises(GitHubError):
        follow(gh)
    assert gh.posts == 1  # the line is on the commit once; going round again would have written it a second time


def test_a_poll_that_fails_in_transit_after_the_deadline_gives_way_to_the_return() -> None:
    gh = Dropping(fail=1)
    _both_passed(gh)
    assert follow(gh, timeout=-1) == []  # no line was written yet: the time was up before the first poll worked


def test_a_failure_that_is_not_one_in_transit_is_not_tolerated_even_at_the_deadline() -> None:
    gh = Dropping(fail=1, status=0)

    def too_many_pages(*args: Any, **kwargs: Any) -> Response:
        raise GitHubError(0, "too many pages for /x (more than 50)")  # what paginate raises: the same on every try

    gh.request = too_many_pages  # type: ignore[method-assign]
    with pytest.raises(GitHubError, match="too many pages"):
        follow(gh, timeout=-1)
