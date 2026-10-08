import pytest

from osac_ci.model import State, Verdict
from osac_ci.publish import OUTCOME, payload

pytestmark = pytest.mark.unit
HEAD = "a" * 40


def verdict(state: State, headline: str = "why", **kw) -> Verdict:  # type: ignore[no-untyped-def]
    return Verdict(state, headline, "do the thing", "someone", mode=kw.pop("mode", "pr"), **kw)  # type: ignore[arg-type]


def test_every_state_has_exactly_one_outcome() -> None:
    assert set(OUTCOME) == set(State)


def test_only_a_planner_failure_is_red() -> None:
    red = {s for s, (_, c) in OUTCOME.items() if c == "failure"}
    assert red == {State.PLANNER_ERROR}


def test_a_blocked_pr_never_reports_success_or_neutral() -> None:
    blocked = {
        State.DRAFT,
        State.NEEDS_AUTHORIZATION,
        State.CHECKS_FAILED,
        State.AWAITING_APPROVAL,
        State.AWAITING_E2E_SIGNAL,
        State.E2E_FAILED,
        State.QUEUE_FAILED,
    }
    assert all(OUTCOME[s] == ("completed", "action_required") for s in blocked)


def test_working_states_are_in_progress_without_a_conclusion() -> None:
    body = payload(verdict(State.E2E_RUNNING), HEAD)
    assert body["status"] == "in_progress" and "conclusion" not in body


def test_ready_is_a_success_on_the_head_commit() -> None:
    body = payload(verdict(State.READY_TO_ENQUEUE), HEAD)
    assert (body["name"], body["head_sha"], body["status"], body["conclusion"]) == (
        "OSAC CI",
        HEAD,
        "completed",
        "success",
    )
    assert body["output"]["title"].startswith("ready-to-enqueue: ")
    assert "### OSAC CI: ready-to-enqueue" in body["output"]["summary"]


def test_the_same_verdict_gives_the_same_external_id_and_a_different_one_gives_another() -> None:
    a = payload(verdict(State.AWAITING_APPROVAL, "needs approval"), HEAD)
    assert a["external_id"] == payload(verdict(State.AWAITING_APPROVAL, "needs approval"), HEAD)["external_id"]
    assert a["external_id"] != payload(verdict(State.AWAITING_APPROVAL, "needs two approvals"), HEAD)["external_id"]
    assert a["external_id"].startswith("osac-ci:")


def test_a_note_goes_above_the_verdict_and_changes_the_id() -> None:
    plain = payload(verdict(State.IN_QUEUE), HEAD)
    noted = payload(verdict(State.IN_QUEUE), HEAD, note="Informational for now.")
    assert noted["output"]["summary"].startswith("Informational for now.\n\n### OSAC CI")
    assert noted["external_id"] != plain["external_id"]


def test_long_text_is_cut_to_what_the_api_accepts() -> None:
    body = payload(verdict(State.CHECKS_FAILED, "x" * 5000, blockers=tuple("b" * 200 for _ in range(1000))), HEAD)
    assert len(body["output"]["title"]) <= 200 and len(body["output"]["summary"]) <= 60_000
    assert body["output"]["summary"].endswith("…")


def test_a_custom_check_name_is_used() -> None:
    assert payload(verdict(State.IN_QUEUE), HEAD, check_name="OSAC CI (beta)")["name"] == "OSAC CI (beta)"
