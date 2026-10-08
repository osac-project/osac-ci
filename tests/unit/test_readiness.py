"""Hand-written readiness cases. The differential test (tests/legacy) is the stronger check against the bash source."""

import pytest
from helpers import HEAD, OTHER

from osac_ci.model import LabelEvent, Review
from osac_ci.rules.readiness import (
    CODERABBIT_LOGIN,
    decide,
    e2e_ready_applied_by_trusted_actor,
    human_has_changes_requested,
)

pytestmark = pytest.mark.unit

BOT = "github-actions[bot]"


def ev(label: str, actor: str = BOT) -> LabelEvent:
    return LabelEvent("labeled", label, actor)


def cr(state: str, commit: str | None = HEAD, id: int = 1, at: str = "2026-01-01T00:00:01Z") -> Review:
    return Review(CODERABBIT_LOGIN, state, "Bot", at, id, commit)


def human(login: str, state: str, id: int, at: str = "2026-01-01T00:00:01Z") -> Review:
    return Review(login, state, "User", at, id, HEAD)


def test_current_lgtm_allows_even_with_open_changes_requested() -> None:
    d = decide({"lgtm"}, [human("alice", "CHANGES_REQUESTED", 1)], HEAD)
    assert d.allowed and d.reason == "allowed: lgtm label present"


def test_earlier_lgtm_allows_when_no_human_changes_requested() -> None:
    d = decide(set(), [], HEAD, [ev("lgtm", "alice")])
    assert d.allowed and d.reason == "allowed: lgtm was applied earlier"


def test_earlier_lgtm_does_not_beat_open_changes_requested() -> None:
    d = decide(set(), [human("alice", "CHANGES_REQUESTED", 1)], HEAD, [ev("lgtm", "alice")])
    assert not d.allowed and d.reason == "waiting: human CHANGES_REQUESTED still open"


def test_e2e_ready_trusted_actor_allows() -> None:
    assert decide({"e2e-ready"}, [], HEAD, [ev("e2e-ready")]).allowed


def test_e2e_ready_untrusted_actor_denies_without_falling_through_to_coderabbit() -> None:
    d = decide({"e2e-ready"}, [cr("APPROVED")], HEAD, [ev("e2e-ready", "alice")])
    assert not d.allowed and d.reason == "denied: e2e-ready label present but applied by untrusted actor"


def test_only_the_latest_e2e_ready_event_counts() -> None:
    events = [ev("e2e-ready"), ev("e2e-ready", "alice")]
    assert not e2e_ready_applied_by_trusted_actor(events)


def test_coderabbit_approval_on_head_allows() -> None:
    d = decide(set(), [cr("APPROVED")], HEAD)
    assert d.allowed and d.reason == f"allowed: APPROVED review on head from {CODERABBIT_LOGIN}"


def test_coderabbit_approval_on_older_sha_waits_and_says_so() -> None:
    d = decide(set(), [cr("APPROVED", OTHER)], HEAD)
    assert not d.allowed and d.reason == f"waiting: CR APPROVED on older SHA {OTHER[:7]}"


def test_coderabbit_latest_decision_wins() -> None:
    reviews = [
        cr("APPROVED", id=1, at="2026-01-01T00:00:01Z"),
        cr("CHANGES_REQUESTED", id=2, at="2026-01-01T00:00:02Z"),
    ]
    assert not decide(set(), reviews, HEAD).allowed


def test_human_changes_requested_blocks_coderabbit_approval() -> None:
    d = decide(set(), [cr("APPROVED"), human("alice", "CHANGES_REQUESTED", 2)], HEAD)
    assert not d.allowed and d.reason == "waiting: human CHANGES_REQUESTED still open"


def test_human_approval_alone_does_not_unlock() -> None:
    d = decide(set(), [human("alice", "APPROVED", 1)], HEAD)
    assert not d.allowed and d.reason == "waiting: no CR APPROVED on this SHA"


def test_bots_never_count_as_human_reviewers() -> None:
    assert not human_has_changes_requested([Review("dependabot[bot]", "CHANGES_REQUESTED", "Bot", "x", 1)])
    assert not human_has_changes_requested([Review("some-app", "CHANGES_REQUESTED", "Bot", "x", 1)])


def test_commented_review_does_not_clear_changes_requested() -> None:
    reviews = [
        human("alice", "CHANGES_REQUESTED", 1, "2026-01-01T00:00:01Z"),
        human("alice", "COMMENTED", 2, "2026-01-01T00:00:02Z"),
    ]
    assert human_has_changes_requested(reviews)


def test_dismissal_clears_changes_requested() -> None:
    reviews = [
        human("alice", "CHANGES_REQUESTED", 1, "2026-01-01T00:00:01Z"),
        human("alice", "DISMISSED", 2, "2026-01-01T00:00:02Z"),
    ]
    assert not human_has_changes_requested(reviews)


def test_empty_head_never_matches_coderabbit() -> None:
    assert not decide(set(), [cr("APPROVED", "")], "").allowed
