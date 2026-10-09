"""The E2E unlock policy rule: which signal lets an expensive job start."""

import pytest
from helpers import HEAD, OTHER

from osac_ci.model import LabelEvent, Review, Snapshot
from osac_ci.policy import Approval, E2EUnlock
from osac_ci.rules.e2e_unlock import decide, describe, describe_requirements

pytestmark = pytest.mark.unit

APPROVAL = Approval()
CR = "coderabbitai[bot]"
BOTH = ("human-approval", "coderabbit-approval")


def review(user: str, state: str = "APPROVED", commit: str = HEAD, at: str = "2026-10-01T10:00:00Z", **kw) -> Review:  # type: ignore[no-untyped-def]
    return Review(
        user=user, state=state, submitted_at=at, commit_id=commit, id=abs(hash((user, at, state))) % 10_000, **kw
    )


def cr(state: str = "APPROVED", commit: str = HEAD, at: str = "2026-10-01T10:00:00Z") -> Review:
    return review(CR, state, commit, at, user_type="Bot")


def snapshot(*reviews: Review, **kw) -> Snapshot:  # type: ignore[no-untyped-def]
    base = {
        "repo": "o/r",
        "number": 1,
        "head_sha": HEAD,
        "author": "author",
        "reviews": tuple(reviews),
        "change_fingerprints": {HEAD: "fp"},
    }
    base.update(kw)
    return Snapshot(**base)  # type: ignore[arg-type]


def go(s: Snapshot, signals: tuple[str, ...] = BOTH, **unlock: object):  # type: ignore[no-untyped-def]
    return decide(s, signals, E2EUnlock(mode="policy", **unlock), APPROVAL)  # type: ignore[arg-type]


# ---- human approval ---------------------------------------------------------------------------------------------


def test_a_human_approval_of_the_current_changes_unlocks() -> None:
    d = go(snapshot(review("bob")))
    assert d.allowed and d.reason == "allowed: human approval from bob"


def test_a_carried_over_approval_unlocks_but_a_stale_one_does_not() -> None:
    assert go(snapshot(review("bob", commit=OTHER), change_fingerprints={HEAD: "fp", OTHER: "fp"})).allowed
    d = go(
        snapshot(review("bob", commit=OTHER), change_fingerprints={HEAD: "fp", OTHER: "different"}), ("human-approval",)
    )
    assert not d.allowed and "out of date" in d.reason


def test_the_author_and_bots_do_not_count_as_a_human_approval() -> None:
    assert not go(snapshot(review("author")), ("human-approval",)).allowed
    assert not go(snapshot(review("dependabot[bot]", user_type="Bot")), ("human-approval",)).allowed


# ---- CodeRabbit --------------------------------------------------------------------------------------------------


def test_coderabbit_on_the_exact_head_unlocks() -> None:
    d = go(snapshot(cr()))
    assert d.allowed and d.reason == f"allowed: APPROVED review on head from {CR}"


def test_coderabbit_on_an_older_commit_does_not_unlock_and_says_so() -> None:
    d = go(snapshot(cr(commit=OTHER)), ("coderabbit-approval",))
    assert not d.allowed and f"CodeRabbit approved an older commit {OTHER[:7]}" in d.reason


def test_coderabbits_latest_decision_wins() -> None:
    reviews = (cr("APPROVED", at="2026-10-01T10:00:00Z"), cr("CHANGES_REQUESTED", at="2026-10-01T11:00:00Z"))
    assert not go(snapshot(*reviews), ("coderabbit-approval",)).allowed


# ---- changes requested -------------------------------------------------------------------------------------------


def test_a_human_changes_requested_blocks_every_signal_by_default() -> None:
    s = snapshot(review("bob", "CHANGES_REQUESTED"), review("carol"), cr())
    d = go(s)
    assert not d.allowed and d.reason == "waiting: human CHANGES_REQUESTED still open"


def test_the_block_can_be_turned_off() -> None:
    s = snapshot(review("bob", "CHANGES_REQUESTED"), cr())
    assert go(s, block_on_changes_requested=False).allowed


def test_a_later_approval_replaces_the_changes_request() -> None:
    s = snapshot(
        review("bob", "CHANGES_REQUESTED", at="2026-10-01T09:00:00Z"), review("bob", at="2026-10-01T10:00:00Z")
    )
    assert go(s).allowed


# ---- labels ------------------------------------------------------------------------------------------------------


def test_the_lgtm_label_counts_only_while_it_is_on_the_pr() -> None:
    assert go(snapshot(labels=frozenset({"lgtm"})), ("lgtm-label",)).allowed
    earlier = (LabelEvent("labeled", "lgtm", "someone"),)
    assert not go(snapshot(label_events=earlier), ("lgtm-label",)).allowed  # the legacy rule's sticky lgtm is gone


def test_the_e2e_ready_label_needs_the_trusted_actor() -> None:
    good = snapshot(
        labels=frozenset({"e2e-ready"}), label_events=(LabelEvent("labeled", "e2e-ready", "github-actions[bot]"),)
    )
    assert go(good, ("e2e-ready-label",)).allowed
    bad = snapshot(labels=frozenset({"e2e-ready"}), label_events=(LabelEvent("labeled", "e2e-ready", "mallory"),))
    d = go(bad, ("e2e-ready-label",))
    assert not d.allowed and "applied by untrusted actor" in d.reason


def test_an_untrusted_e2e_ready_label_does_not_stop_another_signal() -> None:
    # Unlike the legacy ladder, which denies at once, here each signal stands on its own.
    s = snapshot(cr(), labels=frozenset({"e2e-ready"}), label_events=(LabelEvent("labeled", "e2e-ready", "mallory"),))
    assert go(s, ("e2e-ready-label", "coderabbit-approval")).allowed


# ---- the list -----------------------------------------------------------------------------------------------------


def test_only_listed_signals_count() -> None:
    assert not go(snapshot(cr()), ("human-approval",)).allowed
    assert not go(snapshot(review("bob")), ("coderabbit-approval",)).allowed


def test_the_waiting_reason_lists_everything_that_would_unlock() -> None:
    d = go(snapshot())
    assert (
        d.reason
        == "waiting: needs a human approval of the current changes or a CodeRabbit approval on the current commit"
    )


def test_describe_joins_signals_in_order() -> None:
    assert (
        describe(("lgtm-label", "coderabbit-approval"))
        == "the lgtm label or a CodeRabbit approval on the current commit"
    )


def test_an_unrelated_review_state_changes_nothing() -> None:
    assert go(snapshot(review("bob", "COMMENTED")), ("human-approval",)).allowed is False


# ---- human-approval means "approved by the approval policy" -------------------------------------------------------


def go_with(s: Snapshot, approval_policy: Approval, signals: tuple[str, ...] = ("human-approval",), **unlock: object):  # type: ignore[no-untyped-def]
    return decide(s, signals, E2EUnlock(mode="policy", **unlock), approval_policy)  # type: ignore[arg-type]


def test_one_approval_is_not_enough_when_the_policy_asks_for_two() -> None:
    two = Approval(min_approvals=2)
    d = go_with(snapshot(review("bob")), two)
    assert not d.allowed and "needs 2 approving review(s) from someone else, has 1" in d.reason
    assert go_with(snapshot(review("bob"), review("carol")), two).allowed


def test_a_required_code_owner_must_have_approved() -> None:
    owned = {"codeowners": "* @alice\n", "changed_files": ("main.go",)}
    d = go_with(snapshot(review("bob"), **owned), Approval())
    assert not d.allowed and "needs an approval from a code owner (@alice)" in d.reason
    assert go_with(snapshot(review("alice"), **owned), Approval()).allowed
    assert go_with(snapshot(review("bob"), **owned), Approval(require_code_owners=False)).allowed


def test_the_waiting_reason_stays_short_when_nobody_has_approved_yet() -> None:
    d = go_with(snapshot(), Approval(min_approvals=2))
    assert d.reason == "waiting: needs a human approval of the current changes"


def test_the_changes_requested_knob_reaches_the_approval_decision() -> None:
    blocked = snapshot(review("bob"), review("carol", "CHANGES_REQUESTED"))
    assert not go_with(blocked, Approval()).allowed  # blocks by default, before any signal is looked at
    assert go_with(blocked, Approval(), block_on_changes_requested=False).allowed


def test_an_unreadable_owner_team_is_an_error_not_an_unlock() -> None:
    s = snapshot(review("bob"), codeowners="* @org/team\n", changed_files=("a.go",), team_members={"org/team": None})
    with pytest.raises(ValueError, match="cannot read the members"):
        go_with(s, Approval())


def test_describe_requirements_uses_and_between_groups_and_or_within_one() -> None:
    assert describe_requirements([("coderabbit-approval",)]) == "a CodeRabbit approval on the current commit"
    assert describe_requirements([("lgtm-label", "coderabbit-approval")]) == (
        "the lgtm label or a CodeRabbit approval on the current commit"
    )
    assert describe_requirements([("lgtm-label",), ("coderabbit-approval",)]) == (
        "the lgtm label and a CodeRabbit approval on the current commit"
    )
    assert describe_requirements([("lgtm-label", "coderabbit-approval"), ("lgtm-label",)]) == (
        "(the lgtm label or a CodeRabbit approval on the current commit) and the lgtm label"
    )


def test_the_reason_names_the_unmet_requirement_when_someone_has_validly_approved() -> None:
    # bob approved the current changes; dave's approval is out of date; the code owner has not approved.
    s = snapshot(
        review("bob"), review("dave", commit=OTHER),
        codeowners="* @alice\n", changed_files=("main.go",), change_fingerprints={HEAD: "fp", OTHER: "different"},
    )  # fmt: skip
    d = go_with(s, Approval())
    assert not d.allowed
    assert "needs an approval from a code owner (@alice)" in d.reason and "out of date" not in d.reason


def test_the_reason_names_the_stale_approval_when_no_approval_is_valid() -> None:
    s = snapshot(review("dave", commit=OTHER), change_fingerprints={HEAD: "fp", OTHER: "different"})
    d = go_with(s, Approval())
    assert not d.allowed and "approval from dave is out of date" in d.reason
