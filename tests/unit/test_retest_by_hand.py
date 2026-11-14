"""A suite started by hand after the E2E check said Failed (a box of the control comment, or `/test`): the check waits
for it instead of saying Failed until the retest ends, and keeps whatever else it said."""

from __future__ import annotations

import pytest
from test_suite_lines import CA, REPO, RUN, RUN_URL, SHA, VM, Hub, entry

from osac_ci import e2e_check

pytestmark = pytest.mark.unit
OLD_RUN = 400


def retest(hub: Hub, suite: str = "caas", line: str = CA) -> None:
    e2e_check.report(
        hub,  # type: ignore[arg-type]
        REPO,
        [entry(manual=True, suites=[suite], suite_checks={suite: line})],
        RUN,
        RUN_URL,
        "checks",
        lines_kind="statuses",
    )


def e2e(hub: Hub) -> tuple[str, str]:
    last = [c for c in hub.check_runs if c["name"] == "E2E"][-1]
    return last["conclusion"] or last["status"], last["output"]["title"]


def failed_before(hub: Hub) -> None:
    hub.status(VM, "success", url=f"https://example.invalid/o/r/actions/runs/{OLD_RUN}/job/1", text="Passed")
    hub.status(CA, "failure", url=f"https://example.invalid/o/r/actions/runs/{OLD_RUN}/job/2", text="Failed")
    hub.check("E2E", "failure", link=f"https://example.invalid/o/r/actions/runs/{OLD_RUN}", title="Failed: caas")


def test_a_failed_check_waits_for_the_suite_that_is_run_again_by_hand() -> None:
    hub = Hub()
    failed_before(hub)
    retest(hub)
    assert e2e(hub) == ("in_progress", "Waiting: caas running again")
    assert hub.statuses[0]["context"] == CA and hub.statuses[0]["description"] == "Waiting for a runner"


def test_the_end_of_the_retest_adds_the_suites_up_from_the_wait() -> None:
    hub = Hub()
    failed_before(hub)
    retest(hub)
    hub.status(CA, "success", url=f"https://example.invalid/o/r/actions/runs/{RUN}/job/3", text="Passed")
    assert e2e_check.sum_from_lines(hub, REPO, SHA, {"vmaas": VM, "caas": CA}, RUN_URL) == "passed"  # type: ignore[arg-type]
    assert e2e(hub) == ("success", "Passed: vmaas, caas")


def test_a_check_that_passed_stays() -> None:
    hub = Hub()
    hub.status(CA, "success", url=f"https://example.invalid/o/r/actions/runs/{OLD_RUN}/job/2", text="Passed")
    hub.check("E2E", "success", link=f"https://example.invalid/o/r/actions/runs/{OLD_RUN}", title="Passed: vmaas, caas")
    retest(hub)  # a retest for a flake takes no pass away
    assert e2e(hub) == ("success", "Passed: vmaas, caas")


def test_a_check_that_waits_for_something_else_keeps_its_reason() -> None:
    hub = Hub()
    hub.check("E2E", None, title="Waiting for an approval: nobody has approved the head commit")
    retest(hub)
    assert e2e(hub) == ("in_progress", "Waiting for an approval: nobody has approved the head commit")


def test_without_an_e2e_check_nothing_is_written_for_it() -> None:
    hub = Hub()
    retest(hub)
    assert not [c for c in hub.check_runs if c["name"] == "E2E"]


def test_an_automatic_start_is_not_changed() -> None:
    hub = Hub()
    hub.status(VM, "failure", url=f"https://example.invalid/o/r/actions/runs/{OLD_RUN}/job/1", text="Failed")
    e2e_check.report(hub, REPO, [entry()], RUN, RUN_URL, "checks", lines_kind="statuses")  # type: ignore[arg-type]
    assert e2e(hub)[1].startswith("Running")  # the claim of the run, not a wait for a retest
