import pytest
from helpers import HEAD, OTHER

from osac_ci.model import Review, Snapshot
from osac_ci.policy import Approval
from osac_ci.rules.approval import evaluate

pytestmark = pytest.mark.unit

OLD = "c" * 40
FP = "fp-same"


def review(user: str, state: str = "APPROVED", at: str = "2026-10-01T10:00:00Z", commit: str = HEAD, **kw) -> Review:  # type: ignore[no-untyped-def]
    return Review(user=user, state=state, submitted_at=at, commit_id=commit, id=hash((user, at)) % 10_000, **kw)


def snapshot(*reviews: Review, **kw) -> Snapshot:  # type: ignore[no-untyped-def]
    base = {
        "repo": "o/r", "number": 1, "head_sha": HEAD, "author": "author", "reviews": tuple(reviews),
        "changed_files": ("a.go",), "codeowners": None, "team_members": {}, "change_fingerprints": {HEAD: FP},
    }  # fmt: skip
    base.update(kw)
    return Snapshot(**base)  # type: ignore[arg-type]


POLICY = Approval()


def test_no_reviews_needs_an_approval() -> None:
    d = evaluate(snapshot(), POLICY)
    assert not d.approved and d.problems == ("needs 1 approving review(s) from someone else, has 0",)


def test_approval_on_the_current_head_is_enough_without_codeowners() -> None:
    assert evaluate(snapshot(review("bob")), POLICY).approved


def test_the_author_cannot_approve_their_own_pr() -> None:
    assert not evaluate(snapshot(review("author")), POLICY).approved


def test_bot_approvals_do_not_count() -> None:
    assert not evaluate(snapshot(review("coderabbitai[bot]", user_type="Bot")), POLICY).approved


def test_comments_do_not_change_an_earlier_approval() -> None:
    d = evaluate(snapshot(review("bob"), review("bob", "COMMENTED", at="2026-10-01T11:00:00Z")), POLICY)
    assert d.approved


def test_dismissed_approval_no_longer_counts() -> None:
    assert not evaluate(snapshot(review("bob"), review("bob", "DISMISSED", at="2026-10-01T11:00:00Z")), POLICY).approved


def test_changes_requested_blocks_even_with_another_approval() -> None:
    d = evaluate(snapshot(review("bob"), review("carol", "CHANGES_REQUESTED")), POLICY)
    assert not d.approved and "changes requested by carol" in d.problems[0]


def test_a_later_approval_replaces_changes_requested_by_the_same_person() -> None:
    d = evaluate(snapshot(review("bob", "CHANGES_REQUESTED"), review("bob", at="2026-10-01T12:00:00Z")), POLICY)
    assert d.approved


def test_min_approvals_counts_distinct_people() -> None:
    two = Approval(min_approvals=2)
    assert not evaluate(snapshot(review("bob")), two).approved
    assert evaluate(snapshot(review("bob"), review("carol")), two).approved


# ---- freshness -------------------------------------------------------------------------------------------------


def test_approval_of_different_changes_is_stale() -> None:
    d = evaluate(snapshot(review("bob", commit=OLD), change_fingerprints={HEAD: FP, OLD: "fp-other"}), POLICY)
    assert not d.approved
    assert "out of date" in d.problems[0] and OLD[:9] in d.problems[0]


def test_approval_survives_a_rebase_when_the_changes_are_identical() -> None:
    d = evaluate(snapshot(review("bob", commit=OLD), change_fingerprints={HEAD: FP, OLD: FP}), POLICY)
    assert d.approved and d.notes == (f"approval from bob carried over from {OLD[:9]}: same changes after a rebase",)


def test_carry_over_can_be_turned_off() -> None:
    d = evaluate(
        snapshot(review("bob", commit=OLD), change_fingerprints={HEAD: FP, OLD: FP}), Approval(carry_over="never")
    )
    assert not d.approved


@pytest.mark.parametrize("fingerprints", [{HEAD: FP}, {OLD: FP}, {}])
def test_unknown_fingerprint_is_never_a_carry_over(fingerprints: dict[str, str]) -> None:
    assert not evaluate(snapshot(review("bob", commit=OLD), change_fingerprints=fingerprints), POLICY).approved


def test_approval_without_a_commit_is_not_carried() -> None:
    r = Review(user="bob", state="APPROVED", submitted_at="2026-10-01T10:00:00Z", commit_id=None)
    assert not evaluate(snapshot(r), POLICY).approved


# ---- code owners -----------------------------------------------------------------------------------------------

OWNERS = "*  @alice\n/db/  @dba @org/data\n"


def test_a_code_owner_must_approve_files_they_own() -> None:
    s = snapshot(review("bob"), codeowners=OWNERS, changed_files=("main.go",))
    d = evaluate(s, POLICY)
    assert not d.approved and "code owner (@alice) for: main.go" in d.problems[0]
    assert evaluate(snapshot(review("alice"), codeowners=OWNERS, changed_files=("main.go",)), POLICY).approved


def test_every_owned_area_needs_its_own_owner() -> None:
    s = snapshot(
        review("alice"),
        codeowners=OWNERS,
        changed_files=("main.go", "db/schema.sql"),
        team_members={"org/data": frozenset({"erin"})},
    )
    d = evaluate(s, POLICY)
    assert not d.approved and "db/schema.sql" in d.problems[0] and "@dba, @org/data" in d.problems[0]
    both = snapshot(
        review("alice"), review("erin"), codeowners=OWNERS, changed_files=("main.go", "db/schema.sql"),
        team_members={"org/data": frozenset({"Erin"})},
    )  # fmt: skip
    assert evaluate(both, POLICY).approved  # team membership is case-insensitive


def test_files_without_an_owner_only_need_the_minimum() -> None:
    s = snapshot(review("bob"), codeowners="/db/ @dba\n", changed_files=("main.go",))
    assert evaluate(s, POLICY).approved


def test_no_codeowners_file_means_no_owner_requirement() -> None:
    assert evaluate(snapshot(review("bob"), codeowners=None), POLICY).approved


def test_owner_requirement_can_be_turned_off() -> None:
    s = snapshot(review("bob"), codeowners=OWNERS, changed_files=("main.go",))
    assert evaluate(s, Approval(require_code_owners=False)).approved


def test_an_unreadable_team_fails_closed() -> None:
    s = snapshot(review("bob"), codeowners=OWNERS, changed_files=("db/x.sql",), team_members={"org/data": None})
    with pytest.raises(ValueError, match="cannot read the members of code owner team @org/data"):
        evaluate(s, POLICY)


def test_an_owner_approval_given_on_old_code_does_not_count_for_the_owned_files() -> None:
    s = snapshot(
        review("alice", commit=OLD),
        codeowners=OWNERS,
        changed_files=("main.go",),
        change_fingerprints={HEAD: FP, OLD: "x"},
    )
    assert not evaluate(s, POLICY).approved


def test_a_stale_approval_is_only_a_note_when_fresh_ones_already_satisfy_the_policy() -> None:
    s = snapshot(
        review("alice", commit=OTHER), review("dba"), codeowners="/db/ @alice @dba\n", changed_files=("db/x.sql",),
        change_fingerprints={HEAD: FP, OTHER: "x"},
    )  # fmt: skip
    d = evaluate(s, POLICY)
    assert d.approved and d.problems == () and "alice is out of date" in d.notes[0]


def test_a_stale_approval_is_listed_first_when_it_is_why_the_pr_is_blocked() -> None:
    s = snapshot(review("bob", commit=OLD), change_fingerprints={HEAD: FP, OLD: "x"})
    assert "bob is out of date" in evaluate(s, POLICY).problems[0]


def test_an_email_owner_cannot_be_matched_to_a_login() -> None:
    s = snapshot(review("bob"), codeowners="* someone@example.com\n", changed_files=("a.go",))
    d = evaluate(s, POLICY)
    assert not d.approved and "someone@example.com" in d.problems[0]


def test_an_open_change_request_can_be_let_through_when_the_caller_says_so() -> None:
    s = snapshot(review("bob"), review("carol", "CHANGES_REQUESTED"))
    assert not evaluate(s, POLICY).approved
    assert evaluate(s, POLICY, block_on_changes_requested=False).approved
