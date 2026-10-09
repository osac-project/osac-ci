"""The OSAC policy protects the files that define its checks, and nothing else."""

import pytest
from helpers import HEAD, OTHER, snap

from osac_ci.model import Review, State
from osac_ci.planner import plan
from osac_ci.policy import Policy

pytestmark = pytest.mark.unit
TEAM = {"osac-project/wg-infra": frozenset({"infra-bob"})}


def approved(user: str, commit: str = HEAD) -> Review:
    return Review(user, "APPROVED", "User", "2026-10-01T10:00:00Z", 1, commit)


def verdict(policy: Policy, files: tuple[str, ...], *reviews: Review):  # type: ignore[no-untyped-def]
    s = snap(policy, author="alice", changed_files=files, team_members=TEAM, owner_approvers=UI_OWNERS, reviews=reviews)
    return plan(s, policy)


UI_WORKFLOWS = (".github/workflows/osac-ui-lint.yaml", ".github/workflows/osac-ui-typecheck.yaml")
UI_OWNERS = {"osac-ui/OWNERS": frozenset({"rawagner", "eliorerz"})}


def test_the_shared_pipeline_belongs_to_the_group_and_the_ui_workflows_also_to_the_ui_maintainers(
    osac_policy: Policy,
) -> None:
    shared, ui = osac_policy.protected_paths
    assert shared.approvers == ui.approvers == ("@osac-project/wg-infra",)
    assert shared.approvers_from == () and ui.approvers_from == ("osac-ui/OWNERS",)
    assert set(ui.paths) == set(UI_WORKFLOWS) and set(UI_WORKFLOWS) <= set(shared.exclude_paths)


def test_each_change_is_judged_by_exactly_one_rule(osac_policy: Policy) -> None:
    for file in (*UI_WORKFLOWS, ".github/workflows/unit-tests.yml", ".github/actions/x/action.yml", "CODEOWNERS"):
        assert sum(rule.covers(file) for rule in osac_policy.protected_paths) == 1, file


def test_the_override_is_for_the_group(osac_policy: Policy) -> None:
    assert osac_policy.override is not None and osac_policy.override.approvers == ("@osac-project/wg-infra",)


@pytest.mark.parametrize(
    "file",
    [
        ".github/workflows/unit-tests.yml",
        ".github/workflows/osac-ci.yml",
        ".github/workflows/osac-ui-publish-image.yaml",  # publishes an image with secrets: not delegated
        ".github/workflows/build-image.yaml",
        ".github/actions/check-e2e-readiness/action.yml",
        ".github/scripts/auto-queue.sh",
        ".github/filters/ci-filters.yml",
        "CODEOWNERS",
        ".pre-commit-config.yaml",
    ],
)
def test_a_change_to_a_pipeline_file_needs_the_infrastructure_group(osac_policy: Policy, file: str) -> None:
    v = verdict(osac_policy, (file,))
    assert v.state is State.AWAITING_APPROVAL and "wg-infra" in v.headline
    assert verdict(osac_policy, (file,), approved("somebody")).state is State.AWAITING_APPROVAL
    assert verdict(osac_policy, (file,), approved("infra-bob")).state is State.READY_TO_ENQUEUE
    assert verdict(osac_policy, (file,), approved("infra-bob", OTHER)).state is State.AWAITING_APPROVAL  # stale


@pytest.mark.parametrize(
    "file",
    [
        ".github/AGENTS.md",
        ".github/e2e-readiness.md",
        ".github/workflows/README.md",  # a README cannot change a check
        ".github/actions/check-e2e-readiness/README.md",
        ".github/scripts/notes.md",
        ".github/dependabot.yml",
        "docs/CODEOWNERS",
        "fulfillment-service/internal/a.go",
        "osac-ui/src/App.tsx",
        "workflows/a.yml",
    ],
)
def test_other_files_are_not_held_for_the_group(osac_policy: Policy, file: str) -> None:
    assert verdict(osac_policy, (file,)).state is State.READY_TO_ENQUEUE


def test_moving_a_workflow_out_of_its_folder_is_still_a_pipeline_change(osac_policy: Policy) -> None:
    """The adapter lists both names of a rename."""
    assert verdict(osac_policy, ("tools/unit-tests.yml", ".github/workflows/unit-tests.yml")).state is (
        State.AWAITING_APPROVAL
    )


def test_the_label_rules_are_unchanged(osac_policy: Policy) -> None:
    v = plan(snap(osac_policy, labels=frozenset(), changed_files=("a.go",)), osac_policy)
    assert v.state is State.AWAITING_APPROVAL and "missing label: lgtm" in v.headline


def test_a_pipeline_file_that_only_looks_like_documentation_is_still_protected(osac_policy: Policy) -> None:
    for file in (".github/workflows/readme.yml", ".github/scripts/md.sh", ".github/workflows/notes.md.yml"):
        assert verdict(osac_policy, (file,)).state is State.AWAITING_APPROVAL, file


def test_a_pr_that_changes_a_workflow_and_its_readme_still_needs_the_group(osac_policy: Policy) -> None:
    files = (".github/workflows/README.md", ".github/workflows/unit-tests.yml")
    assert verdict(osac_policy, files).state is State.AWAITING_APPROVAL
    assert verdict(osac_policy, files, approved("infra-bob")).state is State.READY_TO_ENQUEUE


@pytest.mark.parametrize("file", UI_WORKFLOWS)
def test_the_ui_maintainers_can_approve_their_own_lint_and_typecheck(osac_policy: Policy, file: str) -> None:
    assert verdict(osac_policy, (file,)).state is State.AWAITING_APPROVAL
    assert verdict(osac_policy, (file,), approved("RawAgner")).state is State.READY_TO_ENQUEUE
    assert verdict(osac_policy, (file,), approved("infra-bob")).state is State.READY_TO_ENQUEUE
    assert verdict(osac_policy, (file,), approved("somebody")).state is State.AWAITING_APPROVAL


def test_the_ui_maintainers_cannot_approve_anything_else_in_the_pipeline(osac_policy: Policy) -> None:
    for file in (
        ".github/workflows/unit-tests.yml",
        ".github/workflows/osac-ui-publish-image.yaml",
        ".github/actions/x/action.yml",
        "CODEOWNERS",
    ):
        assert verdict(osac_policy, (file,), approved("rawagner")).state is State.AWAITING_APPROVAL, file


def test_a_waiver_lifts_only_the_protected_approval(osac_policy: Policy) -> None:
    from osac_ci.model import OverrideGrant

    grant = OverrideGrant("infra-bob", "release is blocked")
    files = (".github/workflows/unit-tests.yml",)
    ok = plan(snap(osac_policy, changed_files=files, team_members=TEAM, overrides=(grant,)), osac_policy)
    assert ok.state is State.READY_TO_ENQUEUE and any("waived by infra-bob" in n for n in ok.notes)
    no_label = plan(
        snap(osac_policy, labels=frozenset(), changed_files=files, team_members=TEAM, overrides=(grant,)), osac_policy
    )
    assert no_label.state is State.AWAITING_APPROVAL and "missing label: lgtm" in no_label.headline
