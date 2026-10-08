"""This repository's own policy must describe this repository's own CI."""

import pytest
import yaml
from helpers import ROOT

from osac_ci.policy import load_policy

pytestmark = pytest.mark.unit


def test_policy_checks_are_exactly_the_jobs_of_ci_yml() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    job_names = {job.get("name", key) for key, job in workflow["jobs"].items()}
    policy = load_policy(ROOT / "policy" / "osac-ci.yml")
    assert {job.check for job in policy.jobs.values()} == job_names


def test_own_policy_uses_native_approval_without_labels() -> None:
    policy = load_policy(ROOT / "policy" / "osac-ci.yml")
    assert policy.approval is not None and policy.merge.required_labels == ()


def test_the_publishing_workflow_never_checks_out_pull_request_code() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "osac-ci-check.yml").read_text(encoding="utf-8"))
    checkouts = [
        s for s in workflow["jobs"]["publish"]["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts and all(s["with"]["ref"] == "${{ github.event.repository.default_branch }}" for s in checkouts)
    assert all(s["with"]["persist-credentials"] is False for s in checkouts)
