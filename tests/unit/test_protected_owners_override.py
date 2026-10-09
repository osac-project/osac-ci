"""A protected-path rule can take approvers from OWNERS files, and a trusted person can waive it for one commit."""

import pytest
from helpers import HEAD, snap

from osac_ci.model import OverrideGrant, Review, State
from osac_ci.planner import plan, plan_or_error
from osac_ci.policy import Override, Policy, PolicyError, ProtectedPaths, parse_policy

pytestmark = pytest.mark.unit
TEAM = "@osac-project/wg-infra"
UI_FILE = ".github/workflows/osac-ui-lint.yaml"
TEAM_MEMBERS = {"osac-project/wg-infra": frozenset({"infra-bob"})}
UI_OWNERS = {"osac-ui/OWNERS": frozenset({"rawagner", "eliorerz"})}


def review(user: str, number: int = 1, state: str = "APPROVED") -> Review:
    return Review(user, state, "User", f"2026-10-01T10:0{number}:00Z", number, HEAD)


@pytest.fixture
def shared(osac_policy: Policy) -> Policy:
    ui = ProtectedPaths(paths=(UI_FILE,), approvers=(TEAM,), approvers_from=("osac-ui/OWNERS",))
    rest = ProtectedPaths(paths=(".github/workflows/**",), exclude_paths=(UI_FILE,), approvers=(TEAM,))
    return osac_policy.model_copy(update={"protected_paths": (ui, rest), "override": Override(approvers=(TEAM,))})


def verdict(policy: Policy, files: tuple[str, ...], *reviews: Review, **extra):  # type: ignore[no-untyped-def]
    return plan(
        snap(
            policy,
            author="alice",
            changed_files=files,
            team_members=TEAM_MEMBERS,
            owner_approvers=UI_OWNERS,
            reviews=reviews,
            **extra,
        ),
        policy,
    )


# ---- approvers from OWNERS files ----------------------------------------------------------------------------------


def test_a_component_maintainer_can_approve_their_components_workflow(shared: Policy) -> None:
    assert verdict(shared, (UI_FILE,), review("RawAgner")).state is State.READY_TO_ENQUEUE


def test_the_infrastructure_group_can_approve_it_too(shared: Policy) -> None:
    assert verdict(shared, (UI_FILE,), review("infra-bob")).state is State.READY_TO_ENQUEUE


def test_somebody_else_cannot(shared: Policy) -> None:
    assert verdict(shared, (UI_FILE,), review("somebody")).state is State.AWAITING_APPROVAL


def test_a_component_maintainer_cannot_approve_the_shared_pipeline(shared: Policy) -> None:
    v = verdict(shared, (".github/workflows/unit-tests.yml",), review("rawagner"))
    assert v.state is State.AWAITING_APPROVAL and TEAM in v.headline


def test_a_pr_touching_both_needs_each_rules_approver(shared: Policy) -> None:
    files = (UI_FILE, ".github/workflows/unit-tests.yml")
    assert verdict(shared, files, review("rawagner")).state is State.AWAITING_APPROVAL  # not enough for the shared one
    assert verdict(shared, files, review("infra-bob")).state is State.READY_TO_ENQUEUE  # satisfies both
    both = verdict(shared, files, review("rawagner"), review("infra-bob", 2))
    assert both.state is State.READY_TO_ENQUEUE


def test_the_author_cannot_approve_even_as_a_component_maintainer(shared: Policy) -> None:
    s = snap(
        shared,
        author="rawagner",
        changed_files=(UI_FILE,),
        team_members=TEAM_MEMBERS,
        owner_approvers=UI_OWNERS,
        reviews=(review("rawagner"),),
    )
    assert plan(s, shared).state is State.AWAITING_APPROVAL


def test_an_unreadable_owners_file_is_an_error_never_a_pass(shared: Policy) -> None:
    s = snap(
        shared,
        author="alice",
        changed_files=(UI_FILE,),
        team_members=TEAM_MEMBERS,
        owner_approvers={"osac-ui/OWNERS": None},
        reviews=(review("infra-bob"),),
    )
    v = plan_or_error(s, shared)
    assert v.state is State.PLANNER_ERROR and "cannot read the approvers in osac-ui/OWNERS" in v.headline


def test_an_owners_file_that_was_never_read_is_an_error_too(shared: Policy) -> None:
    s = snap(
        shared, author="alice", changed_files=(UI_FILE,), team_members=TEAM_MEMBERS, reviews=(review("infra-bob"),)
    )
    v = plan_or_error(s, shared)
    assert v.state is State.PLANNER_ERROR and "cannot read the approvers in osac-ui/OWNERS" in v.headline


def test_a_file_that_names_nobody_leaves_only_the_group(shared: Policy) -> None:
    s = snap(
        shared,
        author="alice",
        changed_files=(UI_FILE,),
        team_members=TEAM_MEMBERS,
        owner_approvers={"osac-ui/OWNERS": frozenset()},
        reviews=(review("rawagner"),),
    )
    assert plan(s, shared).state is State.AWAITING_APPROVAL


# ---- the override ---------------------------------------------------------------------------------------------------


GRANT = OverrideGrant("infra-bob", "release is blocked and the author is the only infra person")


def test_a_valid_override_waives_the_protected_approval_and_says_who_and_why(shared: Policy) -> None:
    v = verdict(shared, (".github/workflows/unit-tests.yml",), overrides=(GRANT,))
    assert v.state is State.READY_TO_ENQUEUE
    assert v.notes == (f"protected-path approval waived by infra-bob: {GRANT.reason}",)


def test_an_override_does_not_hide_anything_else(shared: Policy) -> None:
    v = verdict(shared, (".github/workflows/unit-tests.yml",), overrides=(GRANT,), labels=frozenset())
    assert v.state is State.AWAITING_APPROVAL and "missing label: lgtm" in v.headline
    assert "protected files" not in v.headline


def test_an_override_without_a_protected_change_is_not_mentioned(shared: Policy) -> None:
    v = verdict(shared, ("fulfillment-service/a.go",), overrides=(GRANT,))
    assert v.state is State.READY_TO_ENQUEUE and v.notes == ()


def test_an_approval_makes_the_override_unnecessary_and_unmentioned(shared: Policy) -> None:
    v = verdict(shared, (".github/workflows/unit-tests.yml",), review("infra-bob"), overrides=(GRANT,))
    assert v.state is State.READY_TO_ENQUEUE and v.notes == ()


def test_failed_checks_still_win(shared: Policy) -> None:
    from helpers import with_check

    from osac_ci.model import CheckRun

    runs = with_check(shared, "pre-commit", CheckRun("pre-commit", "completed", "failure"))
    v = verdict(shared, (".github/workflows/unit-tests.yml",), overrides=(GRANT,), check_runs=runs)
    assert v.state is State.CHECKS_FAILED


# ---- policy shape ---------------------------------------------------------------------------------------------------

BASE = "version: 1\nrepo: o/r\njobs: {a: {check: a}}\n"


def test_the_policy_takes_owner_files_and_an_override() -> None:
    p = parse_policy(
        BASE + "protected_paths: [{paths: [x], approvers: ['@o/t'], approvers_from: [osac-ui/OWNERS]}]\n"
        "override: {approvers: ['@o/t', '@bob'], check_name: Waiver}\n"
    )
    assert p.protected_paths[0].approvers_from == ("osac-ui/OWNERS",)
    assert p.override == Override(approvers=("@o/t", "@bob"), check_name="Waiver")


@pytest.mark.parametrize("bad", ["/etc/passwd", "../OWNERS", "a/../OWNERS", "a//OWNERS", "", "a\\OWNERS"])
def test_owner_files_must_be_plain_repository_paths(bad: str) -> None:
    with pytest.raises(PolicyError):
        parse_policy(BASE + f"protected_paths: [{{paths: [x], approvers: ['@o/t'], approvers_from: ['{bad}']}}]\n")


@pytest.mark.parametrize(
    "text",
    [
        "{approvers: []}",
        "{approvers: [bob]}",
        "{approvers: ['@o/..']}",
        "{approvers: ['@o/t'], x: 1}",
        "{check_name: ''}",
    ],
)
def test_a_bad_override_is_refused(text: str) -> None:
    with pytest.raises(PolicyError):
        parse_policy(BASE + f"override: {text}\n")
