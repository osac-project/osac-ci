"""A suite can have a variant: a second box and `/test` name that starts the same suite, with a tier for its job."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from osac_ci import e2e_check, manual_run
from osac_ci.compare import Requirement
from osac_ci.e2e_start import NEEDED, Action, Inputs, Suite, decide, suites_of, to_json
from osac_ci.model import CheckRun
from osac_ci.policy import PolicyError, parse_policy

HEAD = "a" * 40
CAAS = "E2E / CaaS Full Install"
SUITES = (
    Suite("vmaas", "E2E / VMaaS Full Install", (), ("code",)),
    Suite("caas", CAAS, (), ("code",)),
    Suite("caas-regression", CAAS, (), (), True, "caas", "regression", "CaaS Full Install · sanity + regression"),
)
POLICY = """
version: 1
repo: o/r
path_filters:
  mode: enforce
  filters:
    code: ['**']
jobs:
  e2e-vmaas:
    kind: e2e
    check: "E2E / VMaaS Full Install"
    suite: vmaas
    filters_any: [code]
  e2e-caas:
    kind: e2e
    check: "E2E / CaaS Full Install"
    suite: caas
    filters_any: [code]
  e2e-caas-regression:
    kind: e2e
    check: "E2E / CaaS Full Install"
    suite: caas-regression
    manual_only: true
    variant_of: caas
    tier: regression
    label: "CaaS Full Install · sanity + regression"
"""


def line(name: str, conclusion: str | None = "success") -> CheckRun:
    status = "completed" if conclusion else "in_progress"
    return CheckRun(name, status, conclusion, "2026-10-10T10:00:00Z", app="osac-ci", app_id=1)


def inputs(**changes: object) -> Inputs:
    base = Inputs(
        number=7, head_sha=HEAD, author="alice", state="open", draft=False, reviews=(), check_runs=(),
        changed_files=("svc/main.go",), changed_files_known=True, cheap=(Requirement("lint", 1),),
        suites=SUITES, filters={"code": ["**"]},
    )  # fmt: skip
    return replace(base, **changes)  # type: ignore[arg-type]


def policy_with(**job: object) -> str:
    """The policy above with the variant job changed (a value None removes the key)."""
    raw = yaml.safe_load(POLICY)
    for key, value in job.items():
        if value is None:
            raw["jobs"]["e2e-caas-regression"].pop(key, None)
        else:
            raw["jobs"]["e2e-caas-regression"][key] = value
    return yaml.safe_dump(raw)


# the policy -----------------------------------------------------------------------------------------------------------


def test_a_variant_is_read_from_the_policy_and_shares_the_line_of_its_suite() -> None:
    suites = {s.name: s for s in suites_of(parse_policy(POLICY))}
    variant = suites["caas-regression"]
    assert (variant.variant_of, variant.tier, variant.label) == ("caas", "regression", SUITES[2].label)
    assert variant.manual_only and variant.check == suites["caas"].check
    assert suites["caas"].variant_of == "" and suites["caas"].tier == ""


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"manual_only": None}, "manual_only"),
        ({"tier": None}, "tier"),
        ({"tier": "Regression!"}, "tier"),
        ({"label": None}, "label"),
        ({"label": ""}, "label"),
        ({"check": "E2E / Other"}, "must report the line of its suite"),
        ({"variant_of": "vmaas"}, "must report the line of its suite"),
        ({"variant_of": "nothing"}, "not a suite that the decision chooses"),
        ({"variant_of": "caas-regression"}, "not a suite that the decision chooses"),
    ],
)
def test_a_variant_that_is_incomplete_or_points_at_the_wrong_suite_is_refused(change: dict, why: str) -> None:
    with pytest.raises(PolicyError, match=why):
        parse_policy(policy_with(**change))


def test_tier_and_label_without_variant_of_are_refused_and_two_ordinary_jobs_still_cannot_share_a_line() -> None:
    with pytest.raises(PolicyError, match="only make sense together with variant_of"):
        parse_policy(policy_with(variant_of=None))
    raw = yaml.safe_load(POLICY)
    raw["jobs"]["e2e-vmaas"]["check"] = CAAS
    with pytest.raises(PolicyError, match="report the same check"):
        parse_policy(yaml.safe_dump(raw))


def test_a_variant_that_is_the_suite_of_another_variant_is_refused() -> None:
    raw = yaml.safe_load(POLICY)
    raw["jobs"]["e2e-again"] = {**raw["jobs"]["e2e-caas-regression"], "suite": "again", "variant_of": "caas-regression"}
    with pytest.raises(PolicyError, match="not a suite that the decision chooses"):
        parse_policy(yaml.safe_dump(raw))


# the decision ---------------------------------------------------------------------------------------------------------


def test_a_variant_starts_its_suite_on_the_suite_s_line_with_the_tier() -> None:
    d = decide(inputs(requested=("caas-regression",), requested_by="bob"))
    assert d.action is Action.START and d.manual and d.suites == ("caas",)
    assert d.suite_checks == (("caas", CAAS),) and d.tiers == (("caas", "regression"),)
    assert d.notes == (("caas-regression", "started by bob"),)
    assert json.loads(to_json(d))["tiers"] == {"caas": "regression"}


def test_the_suite_itself_starts_without_a_tier() -> None:
    d = decide(inputs(requested=("caas",), requested_by="bob"))
    assert d.suites == ("caas",) and d.tiers == () and json.loads(to_json(d))["tiers"] == {}


def test_a_variant_runs_even_when_its_suite_passed_because_it_asks_for_more() -> None:
    d = decide(inputs(requested=("caas-regression",), requested_by="bob", check_runs=(line(CAAS, "success"),)))
    assert d.starts and d.suites == ("caas",) and d.tiers == (("caas", "regression"),)


def test_the_suite_alone_is_still_not_run_again_after_it_passed() -> None:
    d = decide(inputs(requested=("caas",), requested_by="bob", check_runs=(line(CAAS, "success"),)))
    assert d.action is Action.NOTHING_TO_START and d.tiers == ()


@pytest.mark.parametrize("name", ["caas", "caas-regression"])
def test_neither_box_starts_the_suite_while_it_is_running(name: str) -> None:
    d = decide(inputs(requested=(name,), requested_by="bob", check_runs=(line(CAAS, None),)))
    assert d.action is Action.NOTHING_TO_START and d.tiers == ()
    assert d.notes == ((name, "is already running on this commit; it was not started again"),)


def test_a_failed_suite_can_be_started_again_through_the_variant() -> None:
    d = decide(inputs(requested=("caas-regression",), requested_by="bob", check_runs=(line(CAAS, "failure"),)))
    assert d.starts and d.tiers == (("caas", "regression"),)


@pytest.mark.parametrize("order", [("caas", "caas-regression"), ("caas-regression", "caas")])
def test_both_boxes_start_the_suite_once_with_the_tier_in_either_order(order: tuple[str, ...]) -> None:
    d = decide(inputs(requested=order, requested_by="bob"))
    assert d.suites == ("caas",) and d.tiers == (("caas", "regression"),)
    assert d.suite_checks == (("caas", CAAS),)


def test_the_variant_covers_a_suite_that_passed_when_both_are_asked_for() -> None:
    d = decide(inputs(requested=("caas", "caas-regression"), requested_by="bob", check_runs=(line(CAAS, "success"),)))
    assert d.starts and d.suites == ("caas",)
    assert ("caas", "covered by the run started by bob") in d.notes
    assert not any("already passed" in note for _, note in d.notes)


def test_a_variant_asked_for_with_another_suite_starts_both() -> None:
    d = decide(inputs(requested=("vmaas", "caas-regression"), requested_by="bob"))
    assert d.suites == ("vmaas", "caas") and d.tiers == (("caas", "regression"),)


def test_the_bare_command_and_the_automatic_start_never_choose_a_variant() -> None:
    d = decide(inputs(requested=(NEEDED,), requested_by="bob"))
    assert d.suites == ("vmaas", "caas") and d.tiers == ()
    assert [s.name for s in suites_of(parse_policy(POLICY)) if not s.manual_only] == ["vmaas", "caas"]


def test_a_variant_whose_suite_is_missing_from_the_repository_is_reported() -> None:
    d = decide(inputs(requested=("caas-regression",), requested_by="bob", suites=(SUITES[0], SUITES[2])))
    assert d.action is Action.NOTHING_TO_START
    assert d.notes == (("caas-regression", "is a variant of caas, which is not a suite of this repository"),)


def test_the_checks_the_other_flow_did_list_a_shared_line_once() -> None:
    d = decide(inputs(check_runs=(line(CAAS, "success"),)))
    assert d.e2e_checks.count(f"{CAAS}: success") == 1


# the report -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tiers", "ok"),
    [
        ({"caas": "regression"}, True),
        ({}, True),
        ({"vmaas": "regression"}, False),  # a suite that does not run
        ({"caas": "Regression"}, False),
        ({"caas": "x; id"}, False),
        ({"caas": 1}, False),
        (["caas"], False),
    ],
)
def test_the_tiers_of_the_decision_are_checked_before_they_reach_a_job(tiers: object, ok: bool) -> None:
    entry = {"suites": ["caas"], "tiers": tiers}
    if ok:
        assert e2e_check._tiers(entry) == tuple(dict(tiers).items())  # type: ignore[arg-type]
    else:
        with pytest.raises(ValueError, match="tier"):
            e2e_check._tiers(entry)


def test_the_report_carries_the_tiers_to_the_workflow() -> None:
    found = e2e_check.Report(HEAD, 7, "running", True, ("caas",), "", (("caas", CAAS),), (("caas", "regression"),))
    assert json.loads(e2e_check.to_json(found))["tiers"] == {"caas": "regression"}
    assert json.loads(e2e_check.to_json(e2e_check.Report(HEAD, 7, "running", True, ("caas",), "")))["tiers"] == {}


# the comment ----------------------------------------------------------------------------------------------------------


def test_a_variant_has_a_box_of_its_own_without_a_state_and_is_not_counted_in_the_summary() -> None:
    views = manual_run.views_for(SUITES, [])
    body = manual_run.render_comment(HEAD, views)
    assert "- [ ] <!-- suite:caas-regression --> **CaaS Full Install · sanity + regression**\n" in body
    assert "suite:caas --> **CaaS Full Install** · " in body
    assert manual_run.suites_summary(views) == f"{manual_run.icon('neutral')} 2 not run"
    assert "(3 available)" in body


def test_a_checked_variant_box_is_a_request_for_its_id() -> None:
    body = manual_run.render_comment(HEAD, manual_run.views_for(SUITES, []))
    ticked = body.replace("- [ ] <!-- suite:caas-regression -->", "- [x] <!-- suite:caas-regression -->")
    assert manual_run.requested_suites(ticked) == ("caas-regression",)


def test_the_variant_shows_the_line_of_its_suite_only_on_the_suite_s_box() -> None:
    runs = [{"name": CAAS, "status": "completed", "conclusion": "success", "app": {"slug": "osac-ci"},
             "details_url": "https://x/run/1", "started_at": "2026-10-10T10:00:00Z"}]  # fmt: skip
    body = manual_run.render_comment(HEAD, manual_run.views_for(SUITES, runs))
    assert body.count("https://x/run/1") == 1


# the workflows --------------------------------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]


def test_the_decision_workflow_hands_the_tiers_from_the_report_to_its_callers() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/e2e-pr-decide.yml").read_text(encoding="utf-8"))
    assert wf[True]["workflow_call"]["outputs"]["tiers"]["value"] == "${{ jobs.decide.outputs.tiers }}"
    assert wf["jobs"]["decide"]["outputs"]["tiers"] == "${{ steps.report.outputs.tiers }}"
