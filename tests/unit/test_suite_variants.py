"""A suite can have a variant: a second box and `/test` name that starts the same suite, with a tier for its job."""

from __future__ import annotations

import json
import re
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


def line(name: str, conclusion: str | None = "success", title: str = "", run: int = 0) -> CheckRun:
    status = "completed" if conclusion else "in_progress"
    url = f"https://github.com/o/r/actions/runs/{run}/job/9" if run else ""
    return CheckRun(name, status, conclusion, "2026-10-10T10:00:00Z", app="osac-ci", app_id=1, title=title, url=url)


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


def test_a_suite_asked_for_by_name_runs_again_after_it_passed_and_the_bare_command_leaves_it_alone() -> None:
    passed = (line(CAAS, "success"),)
    d = decide(inputs(requested=("caas",), requested_by="bob", check_runs=passed))
    assert d.starts and d.suites == ("caas",) and d.tiers == () and d.cancel == ()
    bare = decide(
        inputs(
            requested=(NEEDED,), requested_by="bob", check_runs=passed + (line("E2E / VMaaS Full Install", "success"),)
        )
    )
    assert bare.action is Action.NOTHING_TO_START


def tiered_title(tier: str) -> str:
    return f"Running · {tier}" if tier else "Running"


@pytest.mark.parametrize(("name", "running"), [("caas", ""), ("caas-regression", "regression")])
def test_the_same_thing_asked_for_again_while_it_runs_does_nothing(name: str, running: str) -> None:
    running_line = line(CAAS, None, tiered_title(running), run=50)
    d = decide(inputs(requested=(name,), requested_by="bob", check_runs=(running_line,)))
    assert d.action is Action.NOTHING_TO_START and d.cancel == () and d.tiers == ()
    assert d.notes == ((name, "is already running on this commit; it was not started again"),)


def test_a_failed_suite_can_be_started_again_through_the_variant() -> None:
    d = decide(inputs(requested=("caas-regression",), requested_by="bob", check_runs=(line(CAAS, "failure"),)))
    assert d.starts and d.tiers == (("caas", "regression"),)


@pytest.mark.parametrize("order", [("caas", "caas-regression"), ("caas-regression", "caas")])
def test_both_boxes_start_the_suite_once_with_the_tier_in_either_order(order: tuple[str, ...]) -> None:
    d = decide(inputs(requested=order, requested_by="bob"))
    assert d.suites == ("caas",) and d.tiers == (("caas", "regression"),)
    assert d.suite_checks == (("caas", CAAS),)


def test_both_boxes_for_a_suite_that_passed_run_it_once_with_the_tier() -> None:
    d = decide(inputs(requested=("caas", "caas-regression"), requested_by="bob", check_runs=(line(CAAS, "success"),)))
    assert d.starts and d.suites == ("caas",) and d.tiers == (("caas", "regression"),)


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


def test_a_variant_has_a_box_of_its_own_and_is_not_counted_twice_in_the_summary() -> None:
    views = manual_run.views_for(SUITES, [])
    body = manual_run.render_comment(HEAD, views)
    nothing = f"{manual_run.icon('neutral')} not run on this commit"
    assert f"- [ ] <!-- suite:caas-regression --> **CaaS Full Install · sanity + regression** · {nothing}\n" in body
    assert f"suite:caas --> **CaaS Full Install** · {nothing}" in body
    assert manual_run.suites_summary(views) == f"{manual_run.icon('neutral')} 2 not run"
    assert "(3 available)" in body


def test_a_checked_variant_box_is_a_request_for_its_id() -> None:
    body = manual_run.render_comment(HEAD, manual_run.views_for(SUITES, []))
    ticked = body.replace("- [ ] <!-- suite:caas-regression -->", "- [x] <!-- suite:caas-regression -->")
    assert manual_run.requested_suites(ticked) == ("caas-regression",)


def run_line(title: str, state: str | None = "success", at: str = "10:00", run: int = 1) -> dict:
    return {"name": CAAS, "status": "completed" if state else "in_progress", "conclusion": state or None,
            "app": {"slug": "osac-ci"}, "details_url": f"https://x/run/{run}", "started_at": f"2026-10-10T{at}:00Z",
            "output": {"title": title}}  # fmt: skip


def boxes(runs: list[dict]) -> dict[str, str]:
    body = manual_run.render_comment(HEAD, manual_run.views_for(SUITES, runs))
    return {m.group(1): m.group(2) for m in re.finditer(r"suite:([a-z-]+) --> \*\*[^*]+\*\*(.*)", body)}


def test_a_line_for_the_suite_shows_on_its_box_and_the_variant_box_says_nothing_ran() -> None:
    got = boxes([run_line("Passed")])
    passed = f" · {manual_run.icon('passed')} passed ([run](https://x/run/1))"
    assert got["caas"] == passed
    assert got["caas-regression"] == f" · {manual_run.icon('neutral')} not run on this commit"


def test_a_line_for_the_variant_shows_on_the_variant_box_and_the_suite_box_says_it_is_covered() -> None:
    got = boxes([run_line("Passed · regression")])
    assert got["caas-regression"] == f" · {manual_run.icon('passed')} passed ([run](https://x/run/1))"
    assert got["caas"] == f" · {manual_run.icon('passed')} passed (in the regression run) ([run](https://x/run/1))"


def test_a_running_variant_is_running_on_both_boxes_and_the_summary_counts_the_suite_once() -> None:
    views = manual_run.views_for(SUITES, [run_line("Running · regression", "")])
    assert [(v.suite, v.state) for v in views] == [("vmaas", "none"), ("caas", "active"), ("caas-regression", "active")]
    assert manual_run.suites_summary(views).count("1 running") == 1


# the workflows --------------------------------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]


def test_the_decision_workflow_hands_the_tiers_from_the_report_to_its_callers() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/e2e-pr-decide.yml").read_text(encoding="utf-8"))
    assert wf[True]["workflow_call"]["outputs"]["tiers"]["value"] == "${{ jobs.decide.outputs.tiers }}"
    assert wf["jobs"]["decide"]["outputs"]["tiers"] == "${{ steps.report.outputs.tiers }}"


# something else asked for while a suite runs -----------------------------------------------------------------------


def test_the_variant_asked_for_while_the_suite_runs_stops_that_run_and_starts_the_variant() -> None:
    d = decide(
        inputs(requested=("caas-regression",), requested_by="bob", check_runs=(line(CAAS, None, "Running", 50),))
    )
    assert d.starts and d.suites == ("caas",) and d.tiers == (("caas", "regression"),) and d.cancel == (50,)
    assert d.notes == (("caas-regression", "started by bob; the run of the suite was stopped"),)
    assert json.loads(to_json(d))["cancel"] == [50]


def test_the_suite_asked_for_while_the_variant_runs_stops_that_run_and_starts_the_suite_alone() -> None:
    d = decide(
        inputs(requested=("caas",), requested_by="bob", check_runs=(line(CAAS, None, "Running · regression", 50),))
    )
    assert d.starts and d.suites == ("caas",) and d.tiers == () and d.cancel == (50,)
    assert d.notes == (("caas", "started by bob; the run of regression was stopped"),)


def test_the_suites_that_shared_the_stopped_run_start_again_as_they_were() -> None:
    vmaas = "E2E / VMaaS Full Install"
    d = decide(inputs(
        requested=("caas-regression",), requested_by="bob",
        check_runs=(line(CAAS, None, "Running", 50), line(vmaas, None, "Running", 50)),
    ))  # fmt: skip
    assert d.suites == ("caas", "vmaas") and d.cancel == (50,) and d.tiers == (("caas", "regression"),)
    assert d.suite_checks == (("caas", CAAS), ("vmaas", vmaas))
    assert ("vmaas", "was stopped with the run of another suite and started again") in d.notes


def test_a_suite_that_runs_in_another_run_is_left_alone() -> None:
    vmaas = "E2E / VMaaS Full Install"
    d = decide(inputs(
        requested=("caas-regression",), requested_by="bob",
        check_runs=(line(CAAS, None, "Running", 50), line(vmaas, None, "Running", 60)),
    ))  # fmt: skip
    assert d.suites == ("caas",) and d.cancel == (50,)


def test_the_suite_asked_for_while_its_variant_runs_restarts_what_shared_that_run() -> None:
    vmaas = "E2E / VMaaS Full Install"
    d = decide(inputs(
        requested=("caas",), requested_by="bob",
        check_runs=(line(vmaas, None, "Running", 50), line(CAAS, None, "Running · regression", 50)),
    ))  # fmt: skip
    assert d.suites == ("caas", "vmaas") and d.tiers == () and d.cancel == (50,)


def test_a_line_with_no_run_to_stop_is_started_without_stopping_anything() -> None:
    d = decide(inputs(requested=("caas-regression",), requested_by="bob", check_runs=(line(CAAS, None, "Running"),)))
    assert d.starts and d.cancel == ()


def test_the_bare_command_never_stops_a_run() -> None:
    d = decide(inputs(requested=(NEEDED,), requested_by="bob", check_runs=(line(CAAS, None, "Running", 50),)))
    assert d.cancel == () and ("caas", "is already running on this commit; it was not started again") in d.notes


# what the line says --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "tier"),
    [("Running · regression", "regression"), ("Running", ""), ("Passed: all · x y", ""), ("", ""), (" · full", "")],
)
def test_the_tier_a_line_says_is_read_back_from_what_it_says(text: str, tier: str) -> None:
    from osac_ci.e2e_start import tier_of

    assert tier_of(text) == tier


def test_tiered_adds_the_tier_after_the_state_and_nothing_for_the_suite_itself() -> None:
    from osac_ci.e2e_start import tier_of, tiered

    assert tiered("Waiting for a runner", "regression") == "Waiting for a runner · regression"
    assert tiered("Running", "") == "Running" and tier_of(tiered("Passed", "full")) == "full"


def test_the_report_carries_the_runs_to_stop_and_checks_them() -> None:
    entry = {"suites": ["caas"], "cancel": [50, 51]}
    assert e2e_check._cancel(entry) == (50, 51)
    assert e2e_check._cancel({}) == ()
    for bad in ([0], [-1], ["50"], [True], "50", [50, "x; id"], [1.5]):
        with pytest.raises(ValueError, match="run numbers"):
            e2e_check._cancel({"cancel": bad})
    found = e2e_check.Report(HEAD, 7, "running", True, ("caas",), "", (("caas", CAAS),), (), (50,))
    assert json.loads(e2e_check.to_json(found))["cancel"] == [50]


# the workflow ---------------------------------------------------------------------------------------------------------


def test_the_decision_workflow_stops_only_the_runs_the_report_names_and_never_its_own() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/e2e-pr-decide.yml").read_text(encoding="utf-8"))
    steps = {s.get("name"): s for s in wf["jobs"]["decide"]["steps"]}
    step = steps["Stop the run this request replaces"]
    assert (
        step["env"]["CANCEL"] == "${{ steps.report.outputs.cancel }}"
        and step["env"]["GH_TOKEN"] == "${{ github.token }}"
    )
    assert "${{" not in step["run"], "nothing is expanded into the script"
    assert "THIS_RUN" in step["run"] and "*[!0-9]*" in step["run"] and "actions/runs/${id}/cancel" in step["run"]
    names = [s.get("name") or s.get("id") for s in wf["jobs"]["decide"]["steps"]]
    assert names.index("report") < names.index("Stop the run this request replaces"), "after the new lines are written"


def test_the_decision_gets_the_text_of_the_edited_comment_from_the_event_never_through_the_script() -> None:
    action = yaml.safe_load((ROOT / ".github/actions/e2e-decide/action.yml").read_text(encoding="utf-8"))
    step = next(s for s in action["runs"]["steps"] if s.get("id") == "decide")
    assert step["env"]["MANUAL_COMMENT_BODY"] == "${{ inputs.manual-comment-body }}"
    assert "${{" not in step["run"] and "--manual-comment-body-file" in step["run"]
    assert action["inputs"]["manual-comment-body"]["default"] == ""
    wf = yaml.safe_load((ROOT / ".github/workflows/e2e-pr-decide.yml").read_text(encoding="utf-8"))
    given = next(s for s in wf["jobs"]["decide"]["steps"] if s.get("id") == "decide")["with"]
    assert (
        given["manual-comment-body"] == "${{ github.event_name == 'issue_comment' && github.event.comment.body || '' }}"
    )


# each box keeps the last result of its own version -----------------------------------------------------------------


def test_a_newer_plain_run_does_not_take_the_result_of_the_full_run_from_its_box() -> None:
    got = boxes([run_line("Passed · regression", "success", "10:00", 1), run_line("Running", None, "11:00", 2)])
    assert got["caas-regression"] == f" · {manual_run.icon('passed')} passed ([run](https://x/run/1))"
    assert got["caas"] == f" · {manual_run.icon('pending')} running ([run](https://x/run/2))"


def test_a_newer_full_run_does_not_take_the_result_of_the_plain_run_from_its_box() -> None:
    got = boxes([run_line("Failed", "failure", "10:00", 1), run_line("Running · regression", None, "11:00", 2)])
    assert got["caas"] == f" · {manual_run.icon('error')} failed ([run](https://x/run/1))"
    assert got["caas-regression"] == f" · {manual_run.icon('pending')} running ([run](https://x/run/2))"


def test_a_run_the_other_version_replaced_says_it_was_stopped_instead_of_running_for_ever() -> None:
    stopped = f" · {manual_run.icon('closed')} stopped, replaced by another run ([run](https://x/run/1))"
    got = boxes(
        [run_line("Waiting for a runner", None, "10:00", 1), run_line("Passed · regression", "success", "11:00", 2)]
    )
    assert got["caas"] == stopped
    assert got["caas-regression"] == f" · {manual_run.icon('passed')} passed ([run](https://x/run/2))"
    views = manual_run.views_for(
        SUITES,
        [run_line("Waiting for a runner", None, "10:00", 1), run_line("Passed · regression", "success", "11:00", 2)],
    )
    # the line shows the newest run, the full one (passed); the stopped plain run is not counted; vmaas never ran
    passed, none = manual_run.icon("passed"), manual_run.icon("neutral")
    assert manual_run.suites_summary(views) == f"{passed} 1 passed · {none} 1 not run"


def test_the_suite_box_is_covered_by_the_variant_only_when_the_suite_itself_never_ran() -> None:
    only_full = boxes([run_line("Passed · regression", "success", "10:00", 1)])
    assert "(in the regression run)" in only_full["caas"]
    both = boxes([run_line("Passed", "success", "09:00", 1), run_line("Passed · regression", "success", "10:00", 2)])
    assert "in the" not in both["caas"] and "https://x/run/1" in both["caas"]


def test_a_suite_without_a_variant_is_read_as_before() -> None:
    views = manual_run.views_for(SUITES, [run_line("Passed", "success", "10:00", 1)])
    assert next(v for v in views if v.suite == "vmaas").state == "none"


# the history of the runs --------------------------------------------------------------------------------------------


def status_line(check: str, title: str, state: str | None, at: str, run: int) -> dict:
    return {"name": check, "status": "completed" if state else "in_progress", "conclusion": state or None,
            "app": {"slug": "osac-ci"}, "details_url": f"https://github.com/o/r/actions/runs/{run}/job/9",
            "started_at": f"2026-10-10T{at}:00Z", "output": {"title": title}}  # fmt: skip


def test_the_history_lists_each_run_once_with_how_it_ended_newest_first() -> None:
    runs = [
        status_line(CAAS, "Waiting for a runner", None, "10:00", 1),
        status_line(CAAS, "Running", None, "10:05", 1),
        status_line(CAAS, "Passed", "success", "10:30", 1),
        status_line(CAAS, "Waiting for a runner · regression", None, "11:00", 2),
        status_line(CAAS, "Failed · regression", "failure", "11:40", 2),
        status_line("E2E / VMaaS Full Install", "Passed", "success", "09:00", 3),
    ]
    rows = manual_run.history_for(SUITES, runs)
    assert [(h.at[11:16], h.suite, h.version, h.state) for h in rows] == [
        ("11:00", "CaaS Full Install", "regression", "failed"),
        ("10:00", "CaaS Full Install", "", "passed"),
        ("09:00", "VMaaS Full Install", "", "passed"),
    ]
    assert [h.versioned for h in rows] == [True, True, False]
    assert rows[0].url.endswith("/runs/2/job/9")


def test_a_run_that_was_waiting_when_a_newer_run_began_is_stopped_and_the_newest_run_is_not() -> None:
    runs = [
        status_line(CAAS, "Waiting for a runner", None, "10:00", 1),
        status_line(CAAS, "Waiting for a runner · regression", None, "10:10", 2),
    ]
    assert [h.state for h in manual_run.history_for(SUITES, runs)] == ["active", "stopped"]


def test_the_history_ignores_other_lines_other_apps_and_is_cut_at_twenty_runs() -> None:
    runs = [
        status_line("E2E", "Passed", "success", "10:00", 9),
        {**status_line(CAAS, "Passed", "success", "10:00", 8), "app": {"slug": "other"}},
    ]
    assert manual_run.history_for(SUITES, runs) == ()
    many = [status_line(CAAS, "Passed", "success", f"{h:02d}:00", h + 1) for h in range(24)]
    assert len(manual_run.history_for(SUITES, many)) == manual_run.HISTORY_MAX


def test_the_comment_has_a_collapsed_history_inside_the_e2e_section_only_when_there_are_runs() -> None:
    runs = [
        status_line(CAAS, "Passed", "success", "10:30", 1),
        status_line(CAAS, "Failed · regression", "failure", "11:40", 2),
    ]
    body = manual_run.render_comment(
        HEAD, manual_run.views_for(SUITES, runs), "", None, manual_run.history_for(SUITES, runs)
    )
    section = body.split("<details><summary><b>🧪 E2E tests</b>")[1].split("</details>\n\n<!-- notes:start")[0]
    assert "<details><summary>History · 2 runs</summary>" in section
    assert section.index("- [ ] <!-- suite:caas-regression") < section.index("History · 2 runs")
    assert "- 2026-10-10 10:30 UTC · **CaaS Full Install** · default · " in section
    assert "- 2026-10-10 11:40 UTC · **CaaS Full Install** · regression · " in section
    assert "stopped" not in section
    assert "History" not in manual_run.render_comment(HEAD, manual_run.views_for(SUITES, []))
    assert "History · 1 run<" in manual_run.render_comment(
        HEAD, manual_run.views_for(SUITES, runs[:1]), "", None, manual_run.history_for(SUITES, runs[:1])
    )


# what the review of the pull request found ----------------------------------------------------------------------------


def test_a_replaced_run_that_ended_cancelled_is_stopped_not_failed_in_the_history_and_on_its_box() -> None:
    runs = [
        status_line(CAAS, "Cancelled", "cancelled", "10:00", 1),
        status_line(CAAS, "Passed · regression", "success", "11:00", 2),
    ]
    assert [h.state for h in manual_run.history_for(SUITES, runs)] == ["passed", "stopped"]
    assert "stopped, replaced by another run" in boxes(runs)["caas"]
    alone = [status_line(CAAS, "Cancelled", "cancelled", "10:00", 1)]
    assert [h.state for h in manual_run.history_for(SUITES, alone)] == ["failed"]  # nothing replaced it
    assert "failed" in boxes(alone)["caas"]


def test_the_summary_counts_the_version_the_shared_line_shows_now() -> None:
    passed, running = manual_run.icon("passed"), manual_run.icon("pending")
    none = manual_run.icon("neutral")
    base_then_variant = [run_line("Passed", "success", "10:00", 1), run_line("Running · regression", None, "11:00", 2)]
    assert (
        manual_run.suites_summary(manual_run.views_for(SUITES, base_then_variant))
        == f"{running} 1 running · {none} 1 not run"
    )
    variant_then_base = [run_line("Passed · regression", "success", "10:00", 1), run_line("Running", None, "11:00", 2)]
    assert (
        manual_run.suites_summary(manual_run.views_for(SUITES, variant_then_base))
        == f"{running} 1 running · {none} 1 not run"
    )
    only_base = [run_line("Passed", "success", "10:00", 1)]
    assert manual_run.suites_summary(manual_run.views_for(SUITES, only_base)) == f"{passed} 1 passed · {none} 1 not run"
    nothing = manual_run.views_for(SUITES, [])
    assert manual_run.suites_summary(nothing) == f"{none} 2 not run"


def test_two_variants_of_a_suite_cannot_share_a_tier() -> None:
    raw = yaml.safe_load(POLICY)
    raw["jobs"]["e2e-caas-again"] = {**raw["jobs"]["e2e-caas-regression"], "suite": "caas-again", "label": "Again"}
    with pytest.raises(PolicyError, match="same tier"):
        parse_policy(yaml.safe_dump(raw))
    raw["jobs"]["e2e-caas-again"]["tier"] = "other"
    assert {s.name for s in suites_of(parse_policy(yaml.safe_dump(raw)))} == {
        "vmaas",
        "caas",
        "caas-regression",
        "caas-again",
    }


def test_the_stop_step_waits_a_bounded_time_for_the_run_to_end_and_then_goes_on() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/e2e-pr-decide.yml").read_text(encoding="utf-8"))
    step = next(s for s in wf["jobs"]["decide"]["steps"] if s.get("name") == "Stop the run this request replaces")
    run = step["run"]
    assert "seq 1 24" in run and "sleep 5" in run and "--jq .status" in run, "at most two minutes"
    assert '"completed"' in run and "::warning::" in run and "start anyway" in run
    assert "exit 1" in run and run.count("exit 1") == 1, "only a bad run number stops the step"


# the tier in the texts a person reads -------------------------------------------------------------


def test_the_run_summary_of_the_decision_and_of_the_report_name_the_tier_of_a_suite() -> None:
    from osac_ci.e2e_start import render_markdown

    d = decide(inputs(requested=("caas-regression", "vmaas"), requested_by="bob"))
    assert "- suites to start: caas (regression), vmaas\n" in render_markdown(d)
    plain = decide(inputs(requested=("caas",), requested_by="bob"))
    assert "- suites to start: caas\n" in render_markdown(plain)
    found = e2e_check.Report(HEAD, 7, "running", True, ("vmaas", "caas"), "", (), (("caas", "regression"),))
    assert "- suites: vmaas, caas (regression)" in e2e_check.render(found)
    assert "- suites: vmaas, caas\n" in e2e_check.render(
        e2e_check.Report(HEAD, 7, "running", True, ("vmaas", "caas"), "")
    )
