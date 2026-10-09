"""Protected paths: a change to the pipeline's own files needs an approval from named people."""

import pytest
from helpers import HEAD, OTHER, snap

from osac_ci.model import Mode, Review, State
from osac_ci.planner import plan, plan_or_error
from osac_ci.policy import Policy, PolicyError, ProtectedPaths, parse_policy

pytestmark = pytest.mark.unit

TEAM = "@osac-project/wg-infra"
WORKFLOW = ".github/workflows/unit-tests.yml"


@pytest.fixture
def guarded(osac_policy: Policy) -> Policy:
    rule = ProtectedPaths(paths=(".github/**", "CODEOWNERS"), approvers=(TEAM,))
    return osac_policy.model_copy(update={"protected_paths": (rule,)})


def approved_by(user: str, commit: str = HEAD, *, number: int = 1) -> Review:
    return Review(user, "APPROVED", "User", f"2026-10-01T10:0{number}:00Z", number, commit)


TEAM_MEMBERS = {"osac-project/wg-infra": frozenset({"infra-bob"})}


def test_policy_parses_the_section_and_rejects_bad_input() -> None:
    ok = parse_policy(
        "version: 1\nrepo: o/r\nprotected_paths:\n  - {paths: ['.github/**'], approvers: ['@o/infra', '@bob']}\n"
        "jobs: {a: {check: a}}\n"
    )
    assert ok.protected_paths[0].approvers == ("@o/infra", "@bob") and ok.protected_paths[0].carry_over == "never"
    for bad in (
        "{paths: [], approvers: ['@o/t']}",
        "{paths: ['!x'], approvers: ['@o/t']}",
        "{paths: ['x'], exclude_paths: ['!y'], approvers: ['@o/t']}",
        "{paths: ['x'], exclude_paths: [''], approvers: ['@o/t']}",
        "{paths: ['x'], approvers: []}",
        "{paths: ['x'], approvers: ['bob']}",
        "{paths: ['x'], approvers: ['@o/t/extra']}",
        "{paths: ['x'], approvers: ['@o/..']}",
        "{paths: ['x'], approvers: ['@o/.']}",
        "{paths: ['x'], approvers: ['@o/t'], surprise: 1}",
    ):
        with pytest.raises(PolicyError):
            parse_policy(f"version: 1\nrepo: o/r\nprotected_paths: [{bad}]\njobs: {{a: {{check: a}}}}\n")


def test_without_the_section_nothing_changes(osac_policy: Policy) -> None:
    unguarded = osac_policy.model_copy(update={"protected_paths": ()})
    v = plan(snap(unguarded, changed_files=(WORKFLOW,)), unguarded)
    assert v.state is State.READY_TO_ENQUEUE


def test_a_change_elsewhere_is_not_affected(guarded: Policy) -> None:
    assert plan(snap(guarded, changed_files=("fulfillment-service/a.go",)), guarded).state is State.READY_TO_ENQUEUE


def test_a_protected_change_waits_for_the_named_approvers(guarded: Policy) -> None:
    v = plan(snap(guarded, changed_files=("a.go", WORKFLOW), team_members=TEAM_MEMBERS), guarded)
    assert v.state is State.AWAITING_APPROVAL
    assert f"changes protected files ({WORKFLOW}): needs an approval from {TEAM}" in v.headline
    assert v.next_action == f"get an approval from {TEAM} for the protected files"
    assert v.who_must_act == "protected-path approver"


def test_an_approval_from_someone_else_does_not_count(guarded: Policy) -> None:
    s = snap(guarded, changed_files=(WORKFLOW,), team_members=TEAM_MEMBERS, reviews=(approved_by("somebody"),))
    assert plan(s, guarded).state is State.AWAITING_APPROVAL


def test_an_approval_from_a_team_member_unlocks_it(guarded: Policy) -> None:
    s = snap(guarded, changed_files=(WORKFLOW,), team_members=TEAM_MEMBERS, reviews=(approved_by("Infra-Bob"),))
    assert plan(s, guarded).state is State.READY_TO_ENQUEUE


def test_the_author_cannot_approve_their_own_protected_change(guarded: Policy) -> None:
    s = snap(
        guarded,
        author="infra-bob",
        changed_files=(WORKFLOW,),
        team_members=TEAM_MEMBERS,
        reviews=(approved_by("infra-bob"),),
    )
    assert plan(s, guarded).state is State.AWAITING_APPROVAL


def test_an_approval_of_older_code_is_stale_and_says_so(guarded: Policy) -> None:
    s = snap(
        guarded,
        changed_files=(WORKFLOW,),
        team_members=TEAM_MEMBERS,
        reviews=(approved_by("infra-bob", commit=OTHER),),
    )
    v = plan(s, guarded)
    assert v.state is State.AWAITING_APPROVAL and "out of date" in v.headline


def test_changes_requested_by_an_approver_is_not_an_approval(guarded: Policy) -> None:
    review = Review("infra-bob", "CHANGES_REQUESTED", "User", "2026-10-01T10:00:00Z", 1, HEAD)
    s = snap(guarded, changed_files=(WORKFLOW,), team_members=TEAM_MEMBERS, reviews=(review,))
    assert plan(s, guarded).state is State.AWAITING_APPROVAL


def test_a_login_approver_needs_no_team_lookup(osac_policy: Policy) -> None:
    rule = ProtectedPaths(paths=("CODEOWNERS",), approvers=("@carol",))
    policy = osac_policy.model_copy(update={"protected_paths": (rule,)})
    s = snap(policy, changed_files=("CODEOWNERS",), reviews=(approved_by("Carol"),))
    assert plan(s, policy).state is State.READY_TO_ENQUEUE


def test_an_unreadable_team_is_an_error_never_a_pass(guarded: Policy) -> None:
    s = snap(
        guarded, changed_files=(WORKFLOW,), team_members={"osac-project/wg-infra": None}, reviews=(approved_by("x"),)
    )
    assert plan_or_error(s, guarded).state is State.PLANNER_ERROR


def test_carry_over_needs_the_rule_to_allow_it(osac_policy: Policy) -> None:
    def policy_with(carry: str) -> Policy:
        rule = ProtectedPaths(paths=(".github/**",), approvers=(TEAM,), carry_over=carry)  # type: ignore[arg-type]
        return osac_policy.model_copy(update={"protected_paths": (rule,)})

    review = approved_by("infra-bob", commit=OTHER)
    fingerprints = {HEAD: "same", OTHER: "same"}
    for carry, expected in (("never", State.AWAITING_APPROVAL), ("trivial-rebase", State.READY_TO_ENQUEUE)):
        policy = policy_with(carry)
        s = snap(
            policy,
            changed_files=(WORKFLOW,),
            team_members=TEAM_MEMBERS,
            reviews=(review,),
            change_fingerprints=fingerprints,
        )
        assert plan(s, policy).state is expected, carry


def test_every_matching_rule_needs_its_own_approver(osac_policy: Policy) -> None:
    rules = (
        ProtectedPaths(paths=(".github/**",), approvers=(TEAM,)),
        ProtectedPaths(paths=("tools/**",), approvers=("@carol",)),
    )
    policy = osac_policy.model_copy(update={"protected_paths": rules})
    files = (WORKFLOW, "tools/x.sh")
    one = snap(policy, changed_files=files, team_members=TEAM_MEMBERS, reviews=(approved_by("infra-bob"),))
    assert "tools/x.sh" in plan(one, policy).headline
    both = snap(
        policy,
        changed_files=files,
        team_members=TEAM_MEMBERS,
        reviews=(approved_by("infra-bob"), approved_by("carol", number=2)),
    )
    assert plan(both, policy).state is State.READY_TO_ENQUEUE


def test_failed_checks_are_still_shown_before_the_approval_wait(guarded: Policy) -> None:
    from helpers import with_check

    from osac_ci.model import CheckRun

    runs = with_check(guarded, "pre-commit", CheckRun("pre-commit", "completed", "failure"))
    s = snap(guarded, changed_files=(WORKFLOW,), team_members=TEAM_MEMBERS, check_runs=runs)
    assert plan(s, guarded).state is State.CHECKS_FAILED


def test_the_queue_commit_is_not_judged_by_reviews(guarded: Policy) -> None:
    s = snap(guarded, changed_files=(WORKFLOW,), team_members=TEAM_MEMBERS)
    assert plan(s, guarded, Mode.QUEUE).state is State.QUEUE_PASSED


def test_a_protected_change_keeps_its_own_next_step_under_native_approval(guarded: Policy) -> None:
    """Native approval is satisfied, the protected rule is not: the next step must name the protected approvers."""
    from osac_ci.policy import Approval

    policy = guarded.model_copy(update={"approval": Approval(require_code_owners=False)})
    s = snap(policy, changed_files=(WORKFLOW,), team_members=TEAM_MEMBERS, reviews=(approved_by("somebody"),))
    v = plan(s, policy)
    assert (
        v.state is State.AWAITING_APPROVAL and v.next_action == f"get an approval from {TEAM} for the protected files"
    )
    assert v.who_must_act == "protected-path approver"


# ---- which files a rule covers ----------------------------------------------------------------------------------


def covered(patterns: tuple[str, ...], *files: str) -> list[str]:
    from osac_ci.rules.protected import matched_files

    rule = ProtectedPaths(paths=patterns, approvers=(TEAM,))
    return matched_files(snap_for(files), rule)


def snap_for(files: tuple[str, ...]):  # type: ignore[no-untyped-def]
    from osac_ci.model import Snapshot

    return Snapshot(repo="o/r", number=1, head_sha=HEAD, changed_files=files)


@pytest.mark.parametrize(
    ("patterns", "file", "expected"),
    [
        ((".github/**",), ".github/workflows/unit-tests.yml", True),  # a dot folder is matched like any other
        ((".github/**",), ".github/filters/ci-filters.yml", True),
        ((".github/**",), "github/workflows/x.yml", False),
        ((".github/**",), "docs/.github/x.yml", False),  # anchored: only the root folder
        (("CODEOWNERS",), "CODEOWNERS", True),
        (("CODEOWNERS",), "docs/CODEOWNERS", False),
        (("CODEOWNERS",), "CODEOWNERS.md", False),
        (("**/Makefile",), "Makefile", True),
        (("**/Makefile",), "a/b/Makefile", True),
        (("**/Makefile",), "a/Makefile.old", False),
        ((".github/workflows/*.yml",), ".github/workflows/a.yml", True),
        ((".github/workflows/*.yml",), ".github/workflows/sub/a.yml", False),
        (("tools/{bootstrap,ci}.sh",), "tools/ci.sh", True),
        (("tools/{bootstrap,ci}.sh",), "tools/other.sh", False),
        ((".github/**", "CODEOWNERS"), "CODEOWNERS", True),  # any of the globs
    ],
)
def test_a_rule_covers_the_files_its_globs_match(patterns: tuple[str, ...], file: str, expected: bool) -> None:
    assert bool(covered(patterns, file)) is expected


# ---- renames and deletions --------------------------------------------------------------------------------------


def test_moving_a_protected_file_out_of_its_folder_still_needs_the_approvers(guarded: Policy) -> None:
    """The adapter lists both names of a rename; the old one is what the rule must see."""
    s = snap(guarded, changed_files=("tools/unit-tests.yml", WORKFLOW), team_members=TEAM_MEMBERS)
    assert plan(s, guarded).state is State.AWAITING_APPROVAL


def test_a_deleted_protected_file_is_a_change_too(guarded: Policy) -> None:
    s = snap(guarded, changed_files=(".github/workflows/old.yml",), team_members=TEAM_MEMBERS)
    assert plan(s, guarded).state is State.AWAITING_APPROVAL


# ---- who counts -------------------------------------------------------------------------------------------------


def review(user: str, state: str, number: int, *, user_type: str = "User", commit: str = HEAD) -> Review:
    return Review(user, state, user_type, f"2026-10-01T10:{number:02d}:00Z", number, commit)


@pytest.mark.parametrize("state", ["COMMENTED", "DISMISSED", "CHANGES_REQUESTED", "PENDING"])
def test_only_an_approval_counts(guarded: Policy, state: str) -> None:
    s = snap(guarded, changed_files=(WORKFLOW,), team_members=TEAM_MEMBERS, reviews=(review("infra-bob", state, 1),))
    assert plan(s, guarded).state is State.AWAITING_APPROVAL


def test_the_latest_review_of_an_approver_decides(guarded: Policy) -> None:
    approve, object_ = review("infra-bob", "APPROVED", 1), review("infra-bob", "CHANGES_REQUESTED", 2)
    s = snap(guarded, changed_files=(WORKFLOW,), team_members=TEAM_MEMBERS, reviews=(approve, object_))
    assert plan(s, guarded).state is State.AWAITING_APPROVAL
    again = review("infra-bob", "APPROVED", 3)
    s = snap(guarded, changed_files=(WORKFLOW,), team_members=TEAM_MEMBERS, reviews=(object_, again))
    assert plan(s, guarded).state is State.READY_TO_ENQUEUE


def test_a_bot_cannot_be_named_as_an_approver() -> None:
    with pytest.raises(PolicyError):
        parse_policy(
            "version: 1\nrepo: o/r\nprotected_paths: [{paths: ['x'], approvers: ['@coderabbit[bot]']}]\n"
            "jobs: {a: {check: a}}\n"
        )


def test_a_bot_account_approval_does_not_count(guarded: Policy) -> None:
    s = snap(
        guarded,
        changed_files=(WORKFLOW,),
        team_members={"osac-project/wg-infra": frozenset({"infra-bot"})},
        reviews=(review("infra-bot", "APPROVED", 1, user_type="Bot"),),
    )
    assert plan(s, guarded).state is State.AWAITING_APPROVAL


def test_any_one_of_several_approvers_is_enough(osac_policy: Policy) -> None:
    rule = ProtectedPaths(paths=(".github/**",), approvers=("@carol", "@dave", TEAM))
    policy = osac_policy.model_copy(update={"protected_paths": (rule,)})
    s = snap(policy, changed_files=(WORKFLOW,), team_members=TEAM_MEMBERS, reviews=(review("dave", "APPROVED", 1),))
    assert plan(s, policy).state is State.READY_TO_ENQUEUE


# ---- how it combines with the rest of the verdict -----------------------------------------------------------------


def test_missing_labels_and_a_protected_change_are_both_reported(guarded: Policy) -> None:
    s = snap(guarded, labels=frozenset(), changed_files=(WORKFLOW,), team_members=TEAM_MEMBERS)
    v = plan(s, guarded)
    assert "missing label: lgtm" in v.headline and "changes protected files" in v.headline
    assert len(v.blockers) == len(v.headline.split("; "))


def test_a_draft_is_a_draft_before_it_is_anything_else(guarded: Policy) -> None:
    s = snap(guarded, is_draft=True, changed_files=(WORKFLOW,), team_members=TEAM_MEMBERS)
    assert plan(s, guarded).state is State.DRAFT


def test_the_enqueue_gate_is_not_changed_by_protected_paths(guarded: Policy) -> None:
    """label_gate_ok answers 'would today's script have let it in'; the new rule is not part of today's script."""
    s = snap(guarded, changed_files=(WORKFLOW,), team_members=TEAM_MEMBERS)
    assert plan(s, guarded).label_gate_ok is True


def test_a_carried_over_approval_is_noted_in_the_verdict(osac_policy: Policy) -> None:
    rule = ProtectedPaths(paths=(".github/**",), approvers=(TEAM,), carry_over="trivial-rebase")
    policy = osac_policy.model_copy(update={"protected_paths": (rule,)})
    s = snap(
        policy,
        changed_files=(WORKFLOW,),
        team_members=TEAM_MEMBERS,
        reviews=(review("infra-bob", "APPROVED", 1, commit=OTHER),),
        change_fingerprints={HEAD: "same", OTHER: "same"},
    )
    v = plan(s, policy)
    assert v.state is State.READY_TO_ENQUEUE and any("carried over" in n for n in v.notes)


# ---- exclusions ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("file", "expected"),
    [
        (".github/workflows/a.yml", True),
        (".github/workflows/README.md", False),
        (".github/workflows/sub/notes.md", False),
        (".github/workflows/a.md.yml", True),
        ("CODEOWNERS", True),
    ],
)
def test_exclusions_take_files_out_of_the_rule(file: str, expected: bool) -> None:
    rule = ProtectedPaths(paths=(".github/**", "CODEOWNERS"), exclude_paths=("**/*.md",), approvers=(TEAM,))
    assert rule.covers(file) is expected


def test_without_exclusions_every_matching_file_is_covered() -> None:
    rule = ProtectedPaths(paths=(".github/**",), approvers=(TEAM,))
    assert rule.covers(".github/workflows/README.md")


def test_a_pr_that_only_changes_excluded_files_is_not_held(osac_policy: Policy) -> None:
    rule = ProtectedPaths(paths=(".github/**",), exclude_paths=("**/*.md",), approvers=(TEAM,))
    policy = osac_policy.model_copy(update={"protected_paths": (rule,)})
    s = snap(policy, changed_files=(".github/workflows/README.md",), team_members=TEAM_MEMBERS)
    assert plan(s, policy).state is State.READY_TO_ENQUEUE
