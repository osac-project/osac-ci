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


def test_a_finished_ci_run_targets_its_own_pr_and_uses_that_pr_concurrency_group() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "osac-ci-check.yml").read_text(encoding="utf-8"))
    number = "github.event.pull_request.number || github.event.workflow_run.pull_requests[0].number || inputs.number"
    env = next(s["env"] for s in workflow["jobs"]["publish"]["steps"] if s.get("name") == "Post the verdict")
    assert env["PR_NUMBER"] == "${{ " + number + " }}"
    assert workflow["concurrency"]["group"] == "osac-ci-check-${{ " + number + " || 'sweep' }}"


def _workflow(name: str) -> dict:  # type: ignore[type-arg]
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> dict:  # type: ignore[type-arg]
    return workflow.get("on") or workflow[True]  # YAML 1.1 reads a bare `on` key as True


def test_the_organization_read_key_is_only_used_from_the_environment_and_only_for_membership() -> None:
    job = _workflow("osac-ci-check.yml")["jobs"]["publish"]
    assert job["environment"] == "org-read"
    (mint,) = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/create-github-app-token@")]
    permissions = {k for k in mint["with"] if k.startswith("permission-")}
    assert permissions == {"permission-members"} and mint["with"]["permission-members"] == "read"
    assert len(mint["uses"].split("@")[1].split()[0]) == 40  # pinned by commit SHA


def test_the_pr_branch_workflow_never_sees_the_key_and_reviews_are_relayed() -> None:
    check, review = _workflow("osac-ci-check.yml"), _workflow("osac-ci-review.yml")
    assert "pull_request_review" not in _triggers(check)
    assert "pull_request_review" in _triggers(review)
    text = (ROOT / ".github" / "workflows" / "osac-ci-review.yml").read_text(encoding="utf-8")
    assert "secrets." not in text and "environment:" not in text and "actions/checkout" not in text
    assert review["permissions"] == {} and review["jobs"]["relay"]["permissions"] == {"actions": "write"}


def test_the_relay_only_dispatches_the_default_branch_workflow_with_the_pr_number() -> None:
    (step,) = _workflow("osac-ci-review.yml")["jobs"]["relay"]["steps"]
    assert (
        step["run"] == 'gh workflow run osac-ci-check.yml --repo "$REPO" --ref "$DEFAULT_BRANCH" -f number="$PR_NUMBER"'
    )
    assert "workflow_dispatch" in _triggers(_workflow("osac-ci-check.yml"))


def test_owner_checks_are_on_so_the_planner_agrees_with_the_ruleset() -> None:
    assert load_policy(ROOT / "policy" / "osac-ci.yml").approval.require_code_owners is True  # type: ignore[union-attr]
