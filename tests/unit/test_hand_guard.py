"""The automatic start leaves alone a suite that a person is running by hand (its line waits or runs in a run that is
alive), and starts the others; the sum of the suites is then written by whichever run ends last."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from osac_ci import e2e_check
from osac_ci.e2e_start import (
    Action,
    Inputs,
    Review,
    Suite,
    _live_runs,
    decide,
    to_json,
)
from osac_ci.github.api import GitHubError, Response
from osac_ci.model import CheckRun
from osac_ci.planner import latest_checks

pytestmark = pytest.mark.unit
HEAD = "a" * 40
VM = "E2E / VMaaS Full Install"
CA = "E2E / CaaS Full Install"
SUITES = (Suite("vmaas", VM, (), ("code",)), Suite("caas", CA, (), ("code",)))


def url(run: int) -> str:
    return f"https://github.com/o/r/actions/runs/{run}/job/9"


def line(
    name: str, status: str = "in_progress", conclusion: str | None = None, run: int = 0, title: str = ""
) -> CheckRun:
    return CheckRun(
        name,
        status,
        conclusion,
        "2026-10-10T10:00:00Z",
        app="osac-ci",
        app_id=1,
        title=title,
        url=url(run) if run else "",
    )


def inputs(*lines: CheckRun, live: tuple[int, ...] = (), approved: bool = True, **changes: object) -> Inputs:
    base = Inputs(
        number=7, head_sha=HEAD, author="alice", state="open", draft=False,
        reviews=(Review("bob", "APPROVED", "MEMBER", HEAD, permission="write"),) if approved else (),
        check_runs=lines, changed_files=("svc/main.go",), changed_files_known=True, cheap=(), suites=SUITES,
        filters={"code": ["**/*.go"]}, live_runs=frozenset(live),
    )  # fmt: skip
    return replace(base, **changes)  # type: ignore[arg-type]


def test_without_a_run_by_hand_every_needed_suite_starts_as_before() -> None:
    d = decide(inputs())
    assert d.action is Action.START and d.suites == ("vmaas", "caas") and not d.manual and d.selected_checks == ()


def test_a_suite_a_person_runs_by_hand_is_left_alone_and_the_others_start_as_a_sum() -> None:
    d = decide(inputs(line(CA, run=50, title="Running"), live=(50,)))
    assert d.action is Action.START and d.suites == ("vmaas",)
    assert d.manual, "no claim on the E2E: the sum of the suites is written when every one has passed"
    assert dict(d.selected_checks) == {"vmaas": VM, "caas": CA} and "caas run by hand" in d.reason
    assert d.suite_checks == (("vmaas", VM),)


def test_a_line_whose_run_is_gone_is_a_stale_line_and_is_not_believed() -> None:
    d = decide(inputs(line(CA, run=50, title="Running"), live=()))
    assert d.suites == ("vmaas", "caas") and not d.manual


def test_the_run_that_claimed_the_e2e_is_the_automatic_one_and_its_lines_are_not_a_hand_run() -> None:
    claim = line("E2E", run=50, title="Running: vmaas, caas")
    d = decide(inputs(line(CA, run=50, title="Running"), claim, live=(50,)))
    assert d.suites == ("vmaas", "caas") and not d.manual
    other = decide(inputs(line(CA, run=51, title="Running"), claim, live=(50, 51)))
    assert other.suites == ("vmaas",) and other.manual, "another run than the claim's is a run by hand"


def test_every_needed_suite_run_by_hand_means_nothing_to_start_and_says_why() -> None:
    d = decide(inputs(line(CA, run=50), line(VM, run=51), live=(50, 51)))
    assert d.action is Action.RUNNING_BY_HAND and d.suites == () and not d.starts
    assert "vmaas, caas are being run by hand" in d.reason and dict(d.selected_checks) == {"vmaas": VM, "caas": CA}
    one = decide(inputs(line(CA, run=50), line(VM, status="completed", conclusion="success", run=51), live=(50,)))
    assert one.action is Action.RUNNING_BY_HAND and "caas is being run by hand" in one.reason


def test_the_gates_still_apply_to_what_is_left() -> None:
    d = decide(inputs(line(CA, run=50), live=(50,), approved=False))
    assert d.action is Action.WAIT_APPROVAL and d.suites == ("vmaas",)


def test_a_suite_that_passed_or_is_run_in_its_variant_is_read_the_same_way() -> None:
    d = decide(inputs(line(CA, status="completed", conclusion="success", run=50), live=(50,)))
    assert d.suites == ("vmaas",) and not d.manual, "passed: not to do, and not held"
    variant = decide(inputs(line(CA, run=50, title="Running · full"), live=(50,)))
    assert variant.suites == ("vmaas",) and variant.manual


def test_the_decision_says_what_it_did_in_its_json_and_the_report_ignores_it() -> None:
    d = decide(inputs(line(CA, run=50), line(VM, run=51), live=(50, 51)))
    assert json.loads(to_json(d))["action"] == "running-by-hand"
    outcome = e2e_check.plan(json.loads(to_json(d)), None, 9, lambda _: False)
    assert (outcome.state, outcome.start, outcome.writes) == ("ignored", False, ())
    assert "being run by hand" in outcome.note


class Runs:
    """The workflow runs: id -> status; an id that is absent is a 404, and 500 answers an error."""

    def __init__(self, runs: Mapping[int, str]) -> None:
        self.runs = dict(runs)
        self.asked: list[str] = []

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        self.asked.append(path)
        run = int(path.rsplit("/", 1)[1])
        if run == 500:
            return Response(502, {})
        if run not in self.runs:
            return Response(404, {})
        return Response(200, {"id": run, "status": self.runs[run]})


def live_of(*lines: CheckRun, runs: Mapping[int, str]) -> tuple[frozenset[int], Runs]:
    client = Runs(runs)
    return _live_runs(client, "o/r", latest_checks(tuple(lines)), SUITES), client  # type: ignore[arg-type]


def test_the_live_runs_are_the_ones_of_the_active_lines_whose_run_has_not_ended() -> None:
    got, client = live_of(line(VM, run=50), line(CA, run=51), runs={50: "in_progress", 51: "completed"})
    assert got == frozenset({50}) and sorted(client.asked) == sorted([
        "/repos/o/r/actions/runs/50", "/repos/o/r/actions/runs/51"])  # fmt: skip


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "requested", "pending"])
def test_every_status_of_a_run_that_has_not_ended_is_alive(status: str) -> None:
    assert live_of(line(VM, run=50), runs={50: status})[0] == frozenset({50})


def test_a_run_that_cannot_be_read_or_is_gone_is_not_alive_and_a_run_is_asked_about_once() -> None:
    got, client = live_of(line(VM, run=500), line(CA, run=404), runs={})
    assert got == frozenset()
    shared, client = live_of(line(VM, run=50), line(CA, run=50), runs={50: "in_progress"})
    assert shared == frozenset({50}) and client.asked == ["/repos/o/r/actions/runs/50"]


def test_lines_that_have_ended_or_have_no_run_ask_nothing() -> None:
    got, client = live_of(
        line(VM, status="completed", conclusion="success", run=50), line(CA), runs={50: "in_progress"}
    )
    assert got == frozenset() and client.asked == []


def test_an_error_of_github_in_reading_a_run_does_not_make_it_alive() -> None:
    class Boom:
        def request(self, *_: object, **__: object) -> Response:
            raise GitHubError(0, "network")

    assert _live_runs(Boom(), "o/r", latest_checks((line(VM, run=50),)), SUITES) == frozenset()  # type: ignore[arg-type]


class Repo:
    """What fetch_inputs reads of one open pull request whose caas line says it runs in run 50."""

    def __init__(self, runs: Mapping[int, str]) -> None:
        self.runs = dict(runs)

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        page = (params or {}).get("page", "1")
        if path == "/repos/o/r/pulls/7":
            pull = {"state": "open", "draft": False, "user": {"login": "alice"}, "base": {"ref": "main"}}
            return Response(200, {**pull, "head": {"sha": HEAD, "repo": {"full_name": "o/r"}}})
        if path == f"/repos/o/r/commits/{HEAD}/check-runs":
            return Response(200, {"check_runs": []})
        if path == f"/repos/o/r/commits/{HEAD}/statuses":
            status = {"context": CA, "state": "pending", "description": "Running", "target_url": url(50)}
            lines = [{**status, "created_at": "2026-10-10T10:00:00Z", "creator": {"login": "osac-ci[bot]"}}]
            return Response(200, lines if page == "1" else [])
        if path.startswith("/repos/o/r/actions/runs/"):
            run = int(path.rsplit("/", 1)[1])
            return Response(200, {"status": self.runs[run]}) if run in self.runs else Response(404, {})
        if path == "/repos/o/r/pulls/7/files":
            return Response(200, [{"filename": "svc/main.go"}] if page == "1" else [])
        return Response(200, [])


@pytest.mark.parametrize(("status", "live"), [("in_progress", frozenset({50})), ("completed", frozenset())])
def test_fetch_inputs_reads_whether_the_run_of_an_active_line_is_alive(status: str, live: frozenset[int]) -> None:
    from osac_ci.e2e_start import fetch_inputs
    from osac_ci.policy import parse_policy

    policy = parse_policy(
        "version: 1\nrepo: o/r\npath_filters:\n  mode: enforce\n  filters:\n    code: ['**']\njobs:\n"
        f'  e2e-vmaas:\n    kind: e2e\n    check: "{VM}"\n    suite: vmaas\n    filters_any: [code]\n'
        f'  e2e-caas:\n    kind: e2e\n    check: "{CA}"\n    suite: caas\n    filters_any: [code]\n'
    )
    got = fetch_inputs(Repo({50: status}), policy, "o/r", 7, required=())  # type: ignore[arg-type]
    assert got.live_runs == live


# the text of the E2E check when every needed suite has already passed -----------------------------


def passed_line(name: str, title: str, run: int = 50) -> CheckRun:
    return line(name, status="completed", conclusion="success", run=run, title=title)


def test_when_every_needed_suite_has_passed_the_decision_says_with_which_version() -> None:
    d = decide(inputs(passed_line(VM, "Passed"), passed_line(CA, "Passed · full")))
    assert d.action is Action.ALREADY_PASSED and d.suites == ()
    assert d.tiers == (("caas", "full"),), "only a suite that passed with a version names it"
    assert json.loads(to_json(d))["tiers"] == {"caas": "full"}
    plain = decide(inputs(passed_line(VM, "Passed"), passed_line(CA, "Passed")))
    assert plain.action is Action.ALREADY_PASSED and plain.tiers == ()


def test_the_e2e_check_written_for_suites_that_already_passed_names_the_version_they_ran_with() -> None:
    entry = json.loads(to_json(decide(inputs(passed_line(VM, "Passed"), passed_line(CA, "Passed · full")))))
    outcome = e2e_check.plan(entry, None, 9, lambda _: False)
    assert outcome.state == "already-passed" and [w.description for w in outcome.writes] == [
        "Passed: vmaas, caas (full)"
    ]
    plain = json.loads(to_json(decide(inputs(passed_line(VM, "Passed"), passed_line(CA, "Passed")))))
    assert [w.description for w in e2e_check.plan(plain, None, 9, lambda _: False).writes] == ["Passed: vmaas, caas"]


def test_a_version_for_a_suite_the_decision_did_not_count_is_refused() -> None:
    entry = json.loads(to_json(decide(inputs(passed_line(VM, "Passed"), passed_line(CA, "Passed")))))
    entry["tiers"] = {"netris": "full"}
    with pytest.raises(ValueError, match="tier"):
        e2e_check.plan(entry, None, 9, lambda _: False)
    entry["tiers"] = {"caas": "x; id"}
    with pytest.raises(ValueError, match="tier"):
        e2e_check._tiers(entry)
