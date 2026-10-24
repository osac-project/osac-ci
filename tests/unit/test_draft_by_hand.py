"""A suite started by hand on a draft (or on a pull request that waits for an approval): the E2E check that waits says
so in front of its wait, so the page does not read "waiting" while the suites run."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from osac_ci import e2e_check
from osac_ci.e2e_check import Existing
from osac_ci.e2e_start import Inputs, Review, Suite, decide, to_json
from osac_ci.github.api import Response
from osac_ci.model import CheckRun

pytestmark = pytest.mark.unit
HEAD = "a" * 40
REPO = "o/r"
VM = "E2E / VMaaS Full Install"
CA = "E2E / CaaS Full Install"
SUITES = (Suite("vmaas", VM, (), ("code",)), Suite("caas", CA, (), ("code",)))
DRAFT = "Waiting: the pull request is a draft"


def line(name: str, status: str = "in_progress", conclusion: str | None = None) -> CheckRun:
    return CheckRun(name, status, conclusion, "2026-10-10T10:00:00Z", app="osac-ci", app_id=1)


def inputs(*lines: CheckRun, **changes: object) -> Inputs:
    base = Inputs(
        number=7, head_sha=HEAD, author="alice", state="open", draft=True,
        reviews=(Review("bob", "APPROVED", "MEMBER", HEAD, permission="write"),),
        check_runs=lines, changed_files=("svc/main.go",), changed_files_known=True, cheap=(), suites=SUITES,
        filters={"code": ["**/*.go"]},
    )  # fmt: skip
    return replace(base, **changes)  # type: ignore[arg-type]


def test_a_draft_with_a_suite_running_by_hand_carries_what_the_suites_say() -> None:
    d = decide(inputs(line(VM), line(CA, "completed", "failure")))
    assert d.action.value == "skip-draft"
    assert dict(d.by_hand) == {"vmaas": "active", "caas": "failed"}
    assert json.loads(to_json(d))["by_hand"] == {"vmaas": "active", "caas": "failed"}


def test_without_a_line_nothing_is_said_by_hand() -> None:
    assert decide(inputs()).by_hand == ()
    assert e2e_check.waiting_description(json.loads(to_json(decide(inputs())))) == DRAFT


def test_the_waiting_check_names_the_suites_in_front_of_the_wait() -> None:
    entry = json.loads(to_json(decide(inputs(line(VM), line(CA, "completed", "success")))))
    assert e2e_check.waiting_description(entry) == f"By hand: vmaas running, caas passed · {DRAFT}"


def test_the_wait_for_an_approval_says_it_too() -> None:
    entry = json.loads(to_json(decide(inputs(line(VM), draft=False, reviews=()))))
    assert e2e_check.waiting_description(entry).startswith("By hand: vmaas running · Waiting for an approval")


def waiting(text: str = DRAFT) -> Existing:
    return Existing("pending", text, "waiting")


def start_entry(**more: object) -> dict[str, Any]:
    return {
        "action": "start", "manual": True, "suites": ["vmaas", "caas"], "by_hand": {},
        "selected_checks": {"vmaas": VM, "caas": CA}, **more,
    }  # fmt: skip


def test_a_start_by_hand_rewrites_a_waiting_check_to_say_the_suites_run() -> None:
    out = e2e_check.plan(start_entry(), waiting(), 9, lambda _: False)
    assert out.start and [(w.state, w.description) for w in out.writes] == [
        ("pending", f"By hand: vmaas running, caas running · {DRAFT}")  # in progress: the page spins
    ]


def test_a_start_by_hand_keeps_what_other_suites_already_said_and_the_wait_underneath() -> None:
    entry = start_entry(suites=["caas"], by_hand={"vmaas": "passed"})
    said = waiting(f"By hand: vmaas passed, caas running · {DRAFT}")
    assert e2e_check.plan(entry, said, 9, lambda _: False).writes == (), "it says so already: no second row"
    out = e2e_check.plan(entry, waiting("Waiting for an approval"), 9, lambda _: False)
    assert out.writes[0].description == "By hand: vmaas passed, caas running · Waiting for an approval"


@pytest.mark.parametrize(
    "existing",
    [None, Existing("pending", "Running: vmaas", "run:5"), Existing("success", "Passed: vmaas", "run:5")],
)
def test_a_check_that_does_not_wait_is_not_touched_by_a_start_by_hand(existing: Existing | None) -> None:
    assert e2e_check.plan(start_entry(), existing, 9, lambda _: False).writes == ()


def test_a_suite_the_change_does_not_need_is_not_counted() -> None:
    entry = start_entry(suites=["netris"], by_hand={"netris": "active"})
    assert e2e_check.plan(entry, waiting(), 9, lambda _: False).writes == ()


def test_the_check_spins_only_while_a_suite_started_by_hand_is_going() -> None:
    assert e2e_check.wait_state({"vmaas": "active", "caas": "passed"}) == "pending"
    assert e2e_check.wait_state({"vmaas": "passed", "caas": "failed"}) == "waiting"
    assert e2e_check.wait_state({}) == "waiting"
    going = e2e_check.plan({"action": "skip-draft", "by_hand": {"vmaas": "active"}}, None, 9, lambda _: False)
    idle = e2e_check.plan({"action": "skip-draft", "by_hand": {"vmaas": "passed"}}, None, 9, lambda _: False)
    assert going.writes[0].state == "pending" and idle.writes[0].state == "waiting"


def test_the_front_is_taken_off_to_find_the_wait() -> None:
    assert e2e_check.waiting_part(f"By hand: vmaas running · {DRAFT}") == DRAFT
    assert e2e_check.waiting_part(DRAFT) == DRAFT


class Fake:
    """The check runs of one commit; a POST adds a run, the way the app's token writes one."""

    def __init__(self, runs: list[dict[str, Any]]) -> None:
        self.runs = runs
        self.posted: list[Mapping[str, Any]] = []

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path.endswith("/check-runs"):
            return Response(200, {"check_runs": self.runs}, {})
        if method == "GET" and path.endswith("/statuses"):
            return Response(200, [], {})
        if method == "POST" and path.endswith("/check-runs"):
            self.posted.append(body)
            return Response(201, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")


def run(name: str, status: str = "completed", conclusion: str | None = "success", title: str = "") -> dict[str, Any]:
    return {
        "id": abs(hash(name + title)) % 10_000, "name": name, "status": status, "conclusion": conclusion,
        "started_at": "2026-10-10T10:00:00Z", "app": {"slug": "osac-ci"}, "output": {"title": title},
        "details_url": "https://github.com/o/r/actions/runs/5/job/1",
    }  # fmt: skip


def finish(fake: Fake) -> str:
    lines = {"vmaas": VM, "caas": CA}
    return e2e_check.finish_by_hand(fake, REPO, HEAD, lines, "", "checks")  # type: ignore[arg-type]


def test_the_end_of_a_run_by_hand_updates_the_waiting_text_when_not_every_suite_passed() -> None:
    e2e = run("E2E", "queued", None, f"By hand: vmaas running, caas running · {DRAFT}")
    fake = Fake([e2e, run(VM), run(CA, "in_progress", None)])
    assert "not turned green" in finish(fake)
    (written,) = fake.posted
    assert written["name"] == "E2E" and written["output"]["title"] == f"By hand: vmaas passed, caas running · {DRAFT}"


def test_the_end_of_a_run_by_hand_writes_nothing_when_the_text_is_right_or_the_check_does_not_wait() -> None:
    text = f"By hand: vmaas passed, caas running · {DRAFT}"
    same = Fake([run("E2E", "queued", None, text), run(VM), run(CA, "in_progress", None)])
    finish(same)
    assert same.posted == []
    running = Fake([run("E2E", "in_progress", None, "Running: vmaas"), run(VM), run(CA, "in_progress", None)])
    finish(running)
    assert running.posted == []


def test_every_suite_passed_still_turns_the_check_green_over_a_by_hand_wait() -> None:
    fake = Fake([run("E2E", "queued", None, f"By hand: vmaas passed, caas running · {DRAFT}"), run(VM), run(CA)])
    assert "green" in finish(fake)
    (written,) = fake.posted
    assert written["conclusion"] == "success" and written["output"]["title"] == "Passed: vmaas, caas"
