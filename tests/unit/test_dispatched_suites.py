"""A suite that runs as a run of its own: the policy key, and the decision that carries the workflow of each suite it
starts."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from osac_ci.e2e_start import Action, Inputs, Review, Suite, decide, suites_of, to_json
from osac_ci.policy import PolicyError, parse_policy

pytestmark = pytest.mark.unit
HEAD = "a" * 40
VM = "E2E / VMaaS Full Install"
CA = "E2E / CaaS Full Install"


def policy(extra: str = "", variant: str = "") -> str:
    return f"""
version: 1
repo: o/r
jobs:
  e2e-vmaas:
    kind: e2e
    check: "{VM}"
    suite: vmaas
    filters_any: [code]
    {extra}
  e2e-caas:
    kind: e2e
    check: "{CA}"
    suite: caas
    filters_any: [code]
{variant}
path_filters:
  mode: enforce
  filters: {{code: ["**/*.go"]}}
"""


def test_a_suite_may_name_the_file_of_its_workflow() -> None:
    suites = suites_of(parse_policy(policy("workflow: e2e-vmaas-full-install.yml")))
    assert {s.name: s.workflow for s in suites} == {"vmaas": "e2e-vmaas-full-install.yml", "caas": ""}


@pytest.mark.parametrize("bad", ["../x.yml", ".github/workflows/x.yml", "x", "x.yml; id", "a b.yml", "-x.yml", ""])
def test_a_workflow_that_is_not_a_file_name_is_refused(bad: str) -> None:
    with pytest.raises(PolicyError):
        parse_policy(policy(f"workflow: '{bad}'"))


def test_only_a_suite_that_is_not_a_variant_may_have_one() -> None:
    variant = """
  e2e-caas-full:
    kind: e2e
    check: "E2E / CaaS Full Install"
    suite: caas-full
    manual_only: true
    variant_of: caas
    tier: full
    label: "CaaS Full"
    workflow: e2e-caas-full-install.yml
"""
    with pytest.raises(PolicyError, match="not a variant"):
        parse_policy(policy(variant=variant))
    cheap = policy().replace('kind: e2e\n    check: "E2E / VMaaS Full Install"', 'kind: cheap\n    check: "x"')
    with pytest.raises(PolicyError):
        parse_policy(cheap.replace("filters_any: [code]\n    ", "filters_any: [code]\n    workflow: x.yml\n    ", 1))


SUITES = (
    Suite("vmaas", VM, (), ("code",), workflow="e2e-vmaas-full-install.yml"),
    Suite("caas", CA, (), ("code",)),
)


def inputs(**changes: object) -> Inputs:
    base = Inputs(
        number=7, head_sha=HEAD, author="alice", state="open", draft=False,
        reviews=(Review("bob", "APPROVED", "MEMBER", HEAD, permission="write"),),
        check_runs=(), changed_files=("svc/main.go",), changed_files_known=True, cheap=(), suites=SUITES,
        filters={"code": ["**/*.go"]},
    )  # fmt: skip
    return replace(base, **changes)  # type: ignore[arg-type]


def test_the_decision_names_the_workflow_of_each_suite_it_starts_that_has_one() -> None:
    d = decide(inputs())
    assert d.action is Action.START and d.suites == ("vmaas", "caas")
    assert d.workflows == (("vmaas", "e2e-vmaas-full-install.yml"),)
    assert json.loads(to_json(d))["workflows"] == {"vmaas": "e2e-vmaas-full-install.yml"}


def test_a_decision_that_starts_nothing_names_no_workflow() -> None:
    assert decide(inputs(reviews=())).workflows == ()


def test_a_request_by_hand_names_the_workflow_of_the_suites_it_starts() -> None:
    d = decide(inputs(requested=("vmaas", "caas"), requested_by="bob"))
    assert d.action is Action.START and d.manual
    assert dict(d.workflows) == {"vmaas": "e2e-vmaas-full-install.yml"}
    only_caas = decide(inputs(requested=("caas",), requested_by="bob"))
    assert only_caas.workflows == ()


def test_without_a_workflow_nothing_changes_for_a_repository() -> None:
    plain = tuple(replace(s, workflow="") for s in SUITES)
    d = decide(inputs(suites=plain))
    assert d.workflows == () and json.loads(to_json(d))["workflows"] == {}


# the report ------------------------------------------------------------------------------------------------------

from osac_ci import e2e_check  # noqa: E402
from osac_ci.github.api import Response  # noqa: E402


class Gh:
    """A GitHub that has no E2E check yet and records what is written."""

    def __init__(self) -> None:
        self.posted: list[tuple[str, str, object]] = []

    def request(self, method: str, path: str, *, params=None, body=None) -> Response:  # type: ignore[no-untyped-def]
        self.posted.append((method, path, body))
        if method == "GET" and path.endswith("/check-runs"):
            return Response(200, {"check_runs": [{"id": 1, "name": "E2E", "status": "in_progress", "conclusion": None,
                "started_at": "2026-10-10T10:00:00Z", "app": {"slug": "osac-ci"}, "details_url": "https://github.com/o/r/actions/runs/9/job/1",
                "output": {"title": "Running: vmaas, caas"}}]}, {})  # fmt: skip
        if method == "GET" and path.endswith("/statuses"):
            return Response(200, [], {})
        return Response(201, {}, {})


def entry(**more: object) -> dict:
    return {
        "number": 7, "head_sha": HEAD, "action": "start", "suites": ["vmaas", "caas"],
        "suite_checks": {"vmaas": VM, "caas": CA}, "tiers": {"caas": "full"},
        "workflows": {"vmaas": "e2e-vmaas-full-install.yml"}, **more,
    }  # fmt: skip


def test_the_report_splits_the_suites_into_those_of_the_run_and_those_that_run_on_their_own() -> None:
    found = e2e_check.report(Gh(), "o/r", [entry()], 9, "https://x/9", "checks", "checks")  # type: ignore[arg-type]
    assert found.inline_suites == ("caas",) and dict(found.inline_checks) == {"caas": CA}
    assert list(found.dispatched) == [
        {"suite": "vmaas", "workflow": "e2e-vmaas-full-install.yml", "check": VM, "tier": ""}
    ]
    out = json.loads(e2e_check.to_json(found))
    assert out["suites"] == ["vmaas", "caas"], "every suite the decision starts"
    assert out["inline_suites"] == ["caas"] and out["inline_suite_checks"] == {"caas": CA}
    assert out["dispatched"][0]["suite"] == "vmaas"


def test_the_tier_of_a_dispatched_suite_goes_with_it() -> None:
    found = e2e_check.report(  # type: ignore[arg-type]
        Gh(), "o/r", [entry(workflows={"caas": "e2e-caas-full-install.yml"})], 9, "https://x/9", "checks", "checks"
    )
    assert list(found.dispatched) == [
        {"suite": "caas", "workflow": "e2e-caas-full-install.yml", "check": CA, "tier": "full"}
    ]


@pytest.mark.parametrize(
    "workflows",
    [{"nope": "x.yml"}, {"vmaas": "../x.yml"}, {"vmaas": "x"}, {"vmaas": 3}, ["vmaas"], {"vmaas": "x.yml?a=b"}],
)
def test_a_workflow_that_is_not_one_of_a_started_suite_or_not_a_file_name_is_refused(workflows: object) -> None:
    gh = Gh()
    with pytest.raises(ValueError):
        e2e_check.report(gh, "o/r", [entry(workflows=workflows)], 9, "https://x/9", "checks", "checks")  # type: ignore[arg-type]


def test_without_workflows_every_suite_is_inline_and_nothing_is_dispatched() -> None:
    found = e2e_check.report(Gh(), "o/r", [entry(workflows={})], 9, "https://x/9", "checks", "checks")  # type: ignore[arg-type]
    assert found.inline_suites == ("vmaas", "caas") and found.dispatched == ()
