"""The OSAC policy must stay equal to what the live ruleset requires (ruleset drift guard, design task T11)."""

import json

import pytest
from helpers import ROOT

pytestmark = pytest.mark.unit


def _ruleset_contexts() -> set[str]:
    data = json.loads((ROOT / "tests" / "fixtures" / "ruleset-ci-status-checks.json").read_text())
    rule = next(r for r in data["rules"] if r["type"] == "required_status_checks")
    return {c["context"] for c in rule["parameters"]["required_status_checks"]}


def test_policy_checks_plus_check_labels_equal_the_recorded_ruleset(osac_policy) -> None:  # type: ignore[no-untyped-def]
    policy_contexts = {job.check for job in osac_policy.jobs.values()} | {"check-labels"}
    assert policy_contexts == _ruleset_contexts()
    assert len(_ruleset_contexts()) == 26


def test_check_labels_is_not_a_job(osac_policy) -> None:  # type: ignore[no-untyped-def]
    assert "check-labels" not in {job.check for job in osac_policy.jobs.values()}


def test_exactly_three_cost_gated_e2e_jobs(osac_policy) -> None:  # type: ignore[no-untyped-def]
    e2e = {j.check for j in osac_policy.jobs.values() if j.kind == "e2e"}
    assert e2e == {"e2e-vmaas-gate", "e2e-caas-gate", "e2e-bmaas-gate"}
    assert all(j.needs_readiness for j in osac_policy.jobs.values() if j.kind == "e2e")
