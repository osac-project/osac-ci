"""E2E cost-gate readiness.

Faithful port of osac-test-infra `.github/actions/check-e2e-readiness/check-e2e-readiness.sh`
(``decide_e2e_readiness`` and ``explain_e2e_wait``). The order matters and is checked against the real bash
function by tests/legacy/test_readiness_differential.py:

1. current ``lgtm`` label -> allow
2. ``lgtm`` was ever applied and no human has an open CHANGES_REQUESTED -> allow
3. ``e2e-ready`` label present -> allow only if its latest ``labeled`` event was by github-actions[bot];
   otherwise DENY immediately (it does not fall through to step 4)
4. CodeRabbit's latest decision is APPROVED on exactly the head SHA and no human has CHANGES_REQUESTED -> allow
5. otherwise wait
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from osac_ci.model import LabelEvent, Review

CODERABBIT_LOGIN = "coderabbitai[bot]"
E2E_READY_TRUSTED_ACTOR = "github-actions[bot]"
_DECISION_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})


@dataclass(frozen=True)
class Readiness:
    allowed: bool
    reason: str  # for people; its wording may change
    code: str = ""  # for code: "" when allowed or not classified, else one of the CODE_* values below


CODE_CHANGES_REQUESTED = "changes-requested"  # an open human change request locks every signal
CODE_NEEDS_SIGNAL = "needs-signal"  # no listed signal holds yet


def _order_key(review: Review) -> tuple[str, int]:
    # jq: max_by([(.submitted_at // ""), (.id // 0)])
    return (review.submitted_at or "", review.id or 0)


def _is_human(review: Review) -> bool:
    return not review.user.endswith("[bot]") and review.user_type != "Bot"


def human_has_changes_requested(reviews: Sequence[Review]) -> bool:
    latest: dict[str, Review] = {}
    for review in reviews:
        if not _is_human(review) or review.state not in _DECISION_STATES:
            continue
        current = latest.get(review.user)
        if current is None or _order_key(review) > _order_key(current):
            latest[review.user] = review
    return any(review.state == "CHANGES_REQUESTED" for review in latest.values())


def e2e_ready_applied_by_trusted_actor(events: Sequence[LabelEvent]) -> bool:
    labeled = [event for event in events if event.event == "labeled" and event.label == "e2e-ready"]
    return bool(labeled) and labeled[-1].actor == E2E_READY_TRUSTED_ACTOR


def pr_ever_had_lgtm(events: Sequence[LabelEvent]) -> bool:
    return any(event.event == "labeled" and event.label == "lgtm" for event in events)


def coderabbit_latest(reviews: Sequence[Review]) -> Review | None:
    candidates = [r for r in reviews if r.user == CODERABBIT_LOGIN and r.state in _DECISION_STATES]
    return max(candidates, key=_order_key) if candidates else None


def coderabbit_approves_head(reviews: Sequence[Review], head_sha: str) -> bool:
    if not head_sha:
        return False
    latest = coderabbit_latest(reviews)
    if latest is None or latest.state != "APPROVED" or latest.commit_id is None or latest.commit_id != head_sha:
        return False
    return not human_has_changes_requested(reviews)


def explain_wait(
    labels: Collection[str], reviews: Sequence[Review], head_sha: str, events: Sequence[LabelEvent]
) -> str:
    """Port of ``explain_e2e_wait``: one line saying why the PR is still locked."""
    if "e2e-ready" in labels and not e2e_ready_applied_by_trusted_actor(events):
        return "denied: e2e-ready label present but applied by untrusted actor"
    if human_has_changes_requested(reviews):
        return "waiting: human CHANGES_REQUESTED still open"
    latest = coderabbit_latest(reviews)
    if latest is not None and latest.state == "APPROVED" and latest.commit_id and latest.commit_id != head_sha:
        return f"waiting: CR APPROVED on older SHA {latest.commit_id[:7]}"
    return "waiting: no CR APPROVED on this SHA"


def decide(
    labels: Collection[str],
    reviews: Sequence[Review],
    head_sha: str,
    events: Sequence[LabelEvent] = (),
) -> Readiness:
    if "lgtm" in labels:
        return Readiness(True, "allowed: lgtm label present")
    if pr_ever_had_lgtm(events) and not human_has_changes_requested(reviews):
        return Readiness(True, "allowed: lgtm was applied earlier")
    if "e2e-ready" in labels:
        if e2e_ready_applied_by_trusted_actor(events):
            return Readiness(True, "allowed: e2e-ready label present (applied by trusted actor)")
        return Readiness(False, "denied: e2e-ready label present but applied by untrusted actor")
    if coderabbit_approves_head(reviews, head_sha):
        return Readiness(True, f"allowed: APPROVED review on head from {CODERABBIT_LOGIN}")
    return Readiness(False, explain_wait(labels, reviews, head_sha, events))
