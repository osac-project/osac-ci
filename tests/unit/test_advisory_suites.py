"""An advisory suite: started by the decision with the others when its filters hold, but never counted in the E2E
check, never run by the merge queue, and its end changes only its own line."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from osac_ci import e2e_check, e2e_dispatch
from osac_ci.compare import Requirement
from osac_ci.e2e_start import (
    Action,
    CheckRun,
    Inputs,
    Review,
    Suite,
    automatic,
    decide,
    suite_checks_for_files,
    suites_of,
    to_json,
)
from osac_ci.github.api import Response
from osac_ci.policy import PolicyError, parse_policy

pytestmark = pytest.mark.unit
HEAD = "a" * 40
VM = "E2E / VMaaS Full Install"
NET = "E2E / VMaaS Netris Full Install"
NET_FILE = "e2e-pr-vmaas-netris.yml"
FILTERS = {"code": ["**/*.go"], "net": ["{net/**}"]}


def policy(netris: str = f"advisory: true\n    workflow: {NET_FILE}") -> str:
    return f"""
version: 1
repo: o/r
jobs:
  e2e-vmaas:
    kind: e2e
    check: "{VM}"
    suite: vmaas
    filters_any: [code]
  e2e-vmaas-netris:
    kind: e2e
    check: "{NET}"
    suite: vmaas-netris
    filters: [net]
    filters_any: [code]
    {netris}
path_filters:
  mode: enforce
  filters: {{code: ["**/*.go"], net: ["{{net/**}}"]}}
"""


# the policy ------------------------------------------------------------------------------------------------------


def test_a_suite_may_be_advisory() -> None:
    suites = {s.name: s for s in suites_of(parse_policy(policy()))}
    assert suites["vmaas-netris"].advisory and not suites["vmaas"].advisory


@pytest.mark.parametrize(
    ("netris", "why"),
    [
        ("advisory: true", "needs a workflow"),
        (f"advisory: true\n    manual_only: true\n    workflow: {NET_FILE}", "not manual_only"),
    ],
)
def test_an_advisory_suite_runs_on_its_own_and_is_not_also_manual(netris: str, why: str) -> None:
    with pytest.raises(PolicyError, match=why):
        parse_policy(policy(netris))


def test_a_cheap_job_cannot_be_advisory() -> None:
    cheap = policy().replace(f'kind: e2e\n    check: "{NET}"', f'kind: cheap\n    check: "{NET}"')
    with pytest.raises(PolicyError, match="advisory is only valid"):
        parse_policy(cheap)


def test_the_sum_and_the_merge_queue_leave_an_advisory_suite_out() -> None:
    parsed = parse_policy(policy())
    assert [s.name for s in automatic(suites_of(parsed))] == ["vmaas"]
    # what the E2E check is the sum of, and what the merge queue runs, for a change to the area
    assert suite_checks_for_files(parsed, ["net/a.go"]) == (("vmaas", VM),)
    assert suite_checks_for_files(parsed, []) == (("vmaas", VM),), "nothing known: the counted suites, no more"


# the decision ----------------------------------------------------------------------------------------------------

SUITES = (
    Suite("vmaas", VM, (), ("code",)),
    Suite("vmaas-netris", NET, ("net",), ("code",), workflow=NET_FILE, advisory=True),
)


def inputs(**changes: object) -> Inputs:
    base = Inputs(
        number=7, head_sha=HEAD, author="alice", state="open", draft=False,
        reviews=(Review("bob", "APPROVED", "MEMBER", HEAD, permission="write"),),
        check_runs=(), changed_files=("net/a.go",), changed_files_known=True, cheap=(), suites=SUITES,
        filters=FILTERS,
    )  # fmt: skip
    return replace(base, **changes)  # type: ignore[arg-type]


def line(name: str, status: str, conclusion: str | None, run: int = 5) -> CheckRun:
    return CheckRun(
        name=name, status=status, conclusion=conclusion, started_at="2026-10-10T10:00:00Z", app="osac-ci",
        url=f"https://github.com/o/r/actions/runs/{run}/job/1",
    )  # fmt: skip


def test_a_change_to_its_area_starts_it_with_the_others_but_does_not_count_it() -> None:
    d = decide(inputs())
    assert d.action is Action.START and not d.manual
    assert d.suites == ("vmaas", "vmaas-netris") and d.advisory == ("vmaas-netris",)
    assert dict(d.suite_checks) == {"vmaas": VM, "vmaas-netris": NET}
    assert d.workflows == (("vmaas-netris", NET_FILE),)
    assert "advisory, not counted: vmaas-netris" in d.reason
    out = json.loads(to_json(d))
    assert out["advisory"] == ["vmaas-netris"]


def test_a_change_outside_its_area_does_not_start_it() -> None:
    d = decide(inputs(changed_files=("svc/main.go",)))
    assert d.action is Action.START and d.suites == ("vmaas",) and d.advisory == () and d.workflows == ()


def test_it_waits_with_the_others_and_is_not_named_in_the_wait() -> None:
    d = decide(inputs(reviews=()))
    assert d.action is Action.WAIT_APPROVAL and d.suites == ("vmaas",) and d.advisory == ()


def test_it_is_not_started_on_a_guess_when_the_files_are_not_known() -> None:
    d = decide(inputs(changed_files_known=False))
    assert d.action is Action.START and d.suites == ("vmaas",) and d.advisory == ()


ALONE = (SUITES[1], replace(SUITES[0], filters=("other",)))  # vmaas needs a file the change does not touch
ALONE_FILTERS = {**FILTERS, "other": ["x/**"]}


def test_alone_it_starts_once_approved_and_the_e2e_check_is_not_claimed() -> None:
    d = decide(inputs(suites=ALONE, filters=ALONE_FILTERS))
    assert d.action is Action.START and d.advisory_only and d.manual
    assert d.suites == ("vmaas-netris",) and d.advisory == ("vmaas-netris",) and d.selected_checks == ()
    assert d.workflows == (("vmaas-netris", NET_FILE),)
    assert json.loads(to_json(d))["advisory_only"] is True


@pytest.mark.parametrize(
    "changes",
    [
        {"reviews": ()},
        {"reviews": (Review("bob", "CHANGES_REQUESTED", "MEMBER", HEAD, permission="write"),)},
        {"cheap": (Requirement("unit"),), "check_runs": (line("unit", "completed", "failure"),)},
        {"changed_files": ("svc/main.go",)},
    ],
)
def test_alone_it_waits_for_what_the_counted_suites_wait_for(changes: dict[str, object]) -> None:
    d = decide(inputs(suites=ALONE, filters=ALONE_FILTERS, **changes))
    assert d.action is Action.SKIP_NOT_APPLICABLE and d.suites == () and not d.advisory_only


@pytest.mark.parametrize("conclusion", ["failure", "success", None])
def test_alone_it_is_never_started_again_on_the_same_commit(conclusion: str | None) -> None:
    # every event for the commit decides again: a line of it, whatever it says, means it was started
    status = "completed" if conclusion else "in_progress"
    d = decide(inputs(suites=ALONE, filters=ALONE_FILTERS, check_runs=(line(NET, status, conclusion),)))
    assert d.action is Action.SKIP_NOT_APPLICABLE


@pytest.mark.parametrize(
    ("status", "conclusion", "run_alive"), [("completed", "success", False), ("in_progress", None, True)]
)
def test_one_that_passed_or_that_a_person_runs_is_left_alone(
    status: str, conclusion: str | None, run_alive: bool
) -> None:
    d = decide(
        inputs(check_runs=(line(NET, status, conclusion),), live_runs=frozenset({5}) if run_alive else frozenset())
    )
    assert d.action is Action.START and d.suites == ("vmaas",) and d.advisory == ()


def test_one_that_failed_starts_again_with_the_others() -> None:
    d = decide(inputs(check_runs=(line(NET, "completed", "failure"),)))
    assert d.advisory == ("vmaas-netris",)


def test_a_request_by_hand_runs_it_like_any_suite() -> None:
    d = decide(inputs(requested=("vmaas-netris",), requested_by="bob"))
    assert d.action is Action.START and d.manual and d.suites == ("vmaas-netris",)
    assert d.selected_checks == (("vmaas", VM),), "the E2E check still counts only vmaas"


# the report ------------------------------------------------------------------------------------------------------


class ReportGh:
    """A GitHub with no E2E check yet that records what is written."""

    def __init__(self) -> None:
        self.written: list[tuple[str, dict[str, Any]]] = []
        self.e2e: list[dict[str, Any]] = []

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path.endswith("/check-runs"):
            name = (params or {}).get("check_name")
            return Response(200, {"check_runs": [c for c in self.e2e if name in (None, c["name"])]}, {})
        if method == "GET" and path.endswith("/statuses"):
            return Response(200, [], {})
        if method == "POST" and path.endswith("/check-runs"):
            self.written.append((body["name"], body))
            if body["name"] == "E2E":
                self.e2e.append(
                    {**body, "id": len(self.e2e) + 1, "app": {"slug": "osac-ci"}, "details_url": body["details_url"]}
                )
            return Response(201, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")


def entry(**more: object) -> dict[str, Any]:
    return {
        "number": 7, "head_sha": HEAD, "action": "start", "suites": ["vmaas", "vmaas-netris"],
        "suite_checks": {"vmaas": VM, "vmaas-netris": NET}, "workflows": {"vmaas-netris": NET_FILE},
        "advisory": ["vmaas-netris"], **more,
    }  # fmt: skip


def report(e: dict[str, Any]) -> tuple[e2e_check.Report, ReportGh]:
    gh = ReportGh()
    found = e2e_check.report(gh, "o/r", [e], 9, "https://github.com/o/r/actions/runs/9", "checks", "checks")  # type: ignore[arg-type]
    return found, gh


def test_the_claim_names_only_the_counted_suites_and_the_finish_adds_up_only_them() -> None:
    found, gh = report(entry())
    assert found.start
    claim = [b for n, b in gh.written if n == "E2E"]
    assert claim[-1]["output"]["title"] == "Running: vmaas"
    assert {n for n, _ in gh.written} == {"E2E", VM, NET}, "the advisory suite still gets its line"
    out = json.loads(e2e_check.to_json(found))
    assert out["counted_suite_checks"] == {"vmaas": VM}
    assert out["suite_checks"] == {"vmaas": VM, "vmaas-netris": NET}
    assert [d["suite"] for d in out["dispatched"]] == ["vmaas-netris"]


@pytest.mark.parametrize(
    "bad",
    [
        {"advisory": ["nope"]},
        {"advisory": ["vmaas"]},  # started, but as a job of the run: it would be counted
        {"advisory": "vmaas-netris"},
    ],
)
def test_an_advisory_suite_that_is_not_a_dispatched_suite_of_the_decision_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="advisory"):
        report(entry(**bad))


def test_a_decision_without_advisory_suites_reports_as_before() -> None:
    found, _ = report(entry(suites=["vmaas"], suite_checks={"vmaas": VM}, workflows={}, advisory=[]))
    assert found.counted_checks == found.suite_checks == (("vmaas", VM),)


# its end ---------------------------------------------------------------------------------------------------------

RUN_NET = 333
NET_TITLE = f"E2E suite · vmaas-netris · #7 · {HEAD}"


class EndGh(ReportGh):
    def __init__(self, conclusion: str) -> None:
        super().__init__()
        self.conclusion = conclusion

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path == "/repos/o/r/pulls/7":
            return Response(200, {"head": {"sha": HEAD}}, {})
        if method == "GET" and path.endswith("/jobs"):
            return Response(200, {"jobs": []}, {})
        if method == "GET" and path == f"/repos/o/r/actions/runs/{RUN_NET}":
            return Response(200, {"status": "completed", "conclusion": self.conclusion}, {})
        if method == "GET" and path.endswith("/pulls/7/files"):
            raise AssertionError("an advisory suite's end must not add up the E2E check")
        return super().request(method, path, params=params, body=body)


@pytest.mark.parametrize("conclusion", ["failure", "success"])
def test_its_end_writes_its_line_and_leaves_the_e2e_check_alone(conclusion: str) -> None:
    gh = EndGh(conclusion)
    said = e2e_dispatch.suite_ended(
        gh,  # type: ignore[arg-type]
        "o/r",
        RUN_NET,
        NET_TITLE,
        f".github/workflows/{NET_FILE}",
        parse_policy(policy()),
        lines_kind="checks",
    )
    assert "advisory" in said
    assert [n for n, _ in gh.written] == [NET]
    assert gh.written[0][1]["conclusion"] == conclusion


class HandGh(ReportGh):
    """The E2E check failed earlier; this run starts suites by hand (a person runs a counted suite), so the report
    looks whether a suite is being run again."""

    def __init__(self) -> None:
        super().__init__()
        self.e2e.append(
            {"id": 1, "name": "E2E", "status": "completed", "conclusion": "failure", "app": {"slug": "osac-ci"},
             "started_at": "2026-10-10T09:00:00Z", "output": {"title": "Failed: vmaas"},
             "details_url": "https://github.com/o/r/actions/runs/1"}
        )  # fmt: skip

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path.startswith("/repos/o/r/actions/runs/"):
            return Response(200, {"status": "in_progress", "conclusion": None}, {})
        if method == "GET" and path.endswith("/check-runs") and (params or {}).get("check_name") != "E2E":
            lines = [
                {**b, "id": 10 + i, "app": {"slug": "osac-ci"}} for i, (n, b) in enumerate(self.written) if n != "E2E"
            ]
            name = (params or {}).get("check_name")
            return Response(200, {"check_runs": [c for c in lines if name in (None, c["name"])]}, {})
        return super().request(method, path, params=params, body=body)


def test_started_with_a_suite_run_by_hand_the_e2e_check_never_waits_for_it() -> None:
    gh = HandGh()
    e = entry(suites=["vmaas-netris"], suite_checks={"vmaas-netris": NET}, manual=True)
    e2e_check.report(gh, "o/r", [e], 9, "https://github.com/o/r/actions/runs/9", "checks", "checks")  # type: ignore[arg-type]
    assert [n for n, _ in gh.written] == [NET], "its line only: the failed E2E check is not turned into a wait for it"


def test_alone_the_report_says_not_affected_and_claims_nothing() -> None:
    e = entry(suites=["vmaas-netris"], suite_checks={"vmaas-netris": NET}, manual=True, advisory_only=True)
    found, gh = report(e)
    assert found.start and found.counted_checks == ()
    e2e = [b for n, b in gh.written if n == "E2E"]
    assert [b["conclusion"] for b in e2e] == ["success"] and e2e[0]["output"]["title"] == "Not affected by this change"
    assert [n for n, _ in gh.written if n != "E2E"] == [NET]
    assert json.loads(e2e_check.to_json(found))["counted_suite_checks"] == {}


@pytest.mark.parametrize("bad", [{"manual": False}, {"suites": ["vmaas", "vmaas-netris"], "manual": True}])
def test_a_start_of_advisory_suites_only_that_would_count_one_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="advisory suites only"):
        report(entry(suite_checks={"vmaas": VM, "vmaas-netris": NET}, advisory_only=True, **bad))
