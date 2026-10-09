"""The planner with an `e2e.unlock` policy section."""

import pytest
import yaml
from helpers import HEAD, ROOT, snap, with_check

from osac_ci.model import JobStatus, LabelEvent, Review, State
from osac_ci.planner import plan
from osac_ci.policy import Policy, PolicyError, parse_policy

pytestmark = pytest.mark.unit
CR = Review("coderabbitai[bot]", "APPROVED", "Bot", "2026-10-01T10:00:00Z", 1, HEAD)
HUMAN = Review("bob", "APPROVED", "User", "2026-10-01T10:00:00Z", 2, HEAD)
JIRA = frozenset({"jira/valid-reference"})  # the only label these policies need: lgtm is not part of the merge rule


def with_sections(**sections: object) -> Policy:
    raw = yaml.safe_load((ROOT / "policy" / "osac.yml").read_text(encoding="utf-8"))
    raw.update(sections)
    return parse_policy(yaml.safe_dump(raw), base=ROOT / "policy")  # path_filters.file is relative to the policy


def e2e(**unlock: object) -> Policy:
    """Policy mode. An approval section is added only when a human-approval signal needs one."""
    raw: dict[str, object] = {
        "e2e": {"unlock": {"mode": "policy", **unlock}},
        "merge": {"required_labels": ["jira/valid-reference"]},
    }
    signals = [
        *(unlock.get("any_of") or ["human-approval"]),
        *[x for v in (unlock.get("per_suite") or {}).values() for x in v],
    ]  # type: ignore[union-attr]
    if "human-approval" in signals:
        raw["approval"] = {"min_approvals": 1, "require_code_owners": False}
    return with_sections(**raw)


def gates(policy: Policy) -> list[str]:
    return [j.check for j in policy.jobs.values() if j.needs_readiness]


def without_gates(policy: Policy):  # type: ignore[no-untyped-def]
    runs = snap(policy).check_runs
    return tuple(r for r in runs if r.name not in gates(policy))


# ---- the policy file -----------------------------------------------------------------------------------------------


def test_the_default_is_the_legacy_ladder(osac_policy: Policy) -> None:
    assert osac_policy.e2e.unlock.mode == "legacy"


def test_a_policy_section_parses() -> None:
    p = e2e(any_of=["coderabbit-approval"], per_suite={"bmaas/sanity": ["human-approval"]})
    assert p.e2e.unlock.signals_for("bmaas/sanity") == ("human-approval",)
    assert p.e2e.unlock.signals_for("vmaas") == ("coderabbit-approval",)
    assert p.e2e.unlock.signals_for(None) == ("coderabbit-approval",)


@pytest.mark.parametrize(
    "unlock",
    [
        {"any_of": []},
        {"any_of": ["lgtm-label", "lgtm-label"]},
        {"any_of": ["magic"]},
        {"per_suite": {"bmaas/sanity": []}},
        {"per_suite": {"no-such-suite": ["lgtm-label"]}},
        {"surprise": 1},
    ],
)
def test_invalid_unlock_sections_are_rejected(unlock: dict[str, object]) -> None:
    with pytest.raises(PolicyError):
        e2e(**unlock)


def test_human_approval_needs_an_approval_section() -> None:
    with pytest.raises(PolicyError, match="needs an approval: section"):
        with_sections(e2e={"unlock": {"mode": "policy", "any_of": ["human-approval"]}})
    assert with_sections(e2e={"unlock": {"mode": "policy", "any_of": ["coderabbit-approval"]}})  # no approval needed


# ---- the planner -----------------------------------------------------------------------------------------------------


def test_nothing_unlocks_a_pr_with_no_signal() -> None:
    p = e2e(any_of=["coderabbit-approval", "lgtm-label"])
    v = plan(snap(p, labels=JIRA, check_runs=without_gates(p)), p)
    assert v.state is State.AWAITING_E2E_SIGNAL
    assert v.next_action == "get a CodeRabbit approval on the current commit or the lgtm label"
    assert v.who_must_act == "reviewer"


def test_coderabbit_on_head_unlocks() -> None:
    p = e2e(any_of=["coderabbit-approval"])
    assert (
        plan(snap(p, labels=JIRA, reviews=(CR,), check_runs=without_gates(p)), p).state is not State.AWAITING_E2E_SIGNAL
    )


def test_a_human_approval_unlocks() -> None:
    p = e2e()
    assert (
        plan(snap(p, labels=JIRA, reviews=(HUMAN,), check_runs=without_gates(p)), p).state
        is not State.AWAITING_E2E_SIGNAL
    )


def test_the_sticky_lgtm_of_the_legacy_ladder_no_longer_unlocks(osac_policy: Policy) -> None:
    p = e2e(any_of=["coderabbit-approval"])
    sticky = {"labels": JIRA, "label_events": (LabelEvent("labeled", "lgtm", "someone"),)}
    osac_policy = with_sections(merge={"required_labels": ["jira/valid-reference"]})  # legacy ladder, same merge rule
    legacy = plan(snap(osac_policy, check_runs=without_gates(osac_policy), **sticky), osac_policy)
    assert legacy.state is not State.AWAITING_E2E_SIGNAL  # the ladder: lgtm was applied once
    assert plan(snap(p, check_runs=without_gates(p), **sticky), p).state is State.AWAITING_E2E_SIGNAL


def test_the_synthetic_lgtm_of_native_approval_is_not_a_signal_in_policy_mode() -> None:
    # In legacy mode an approved PR unlocks like lgtm; in policy mode only the listed signals count.
    raw = {
        "approval": {"min_approvals": 1, "require_code_owners": False},
        "merge": {"required_labels": ["jira/valid-reference"]},
    }
    p = with_sections(e2e={"unlock": {"mode": "policy", "any_of": ["coderabbit-approval"]}}, **raw)
    v = plan(snap(p, labels=frozenset({"jira/valid-reference"}), reviews=(HUMAN,), check_runs=without_gates(p)), p)
    assert v.state is State.AWAITING_E2E_SIGNAL


def test_a_suite_can_have_its_own_signal() -> None:
    p = e2e(any_of=["coderabbit-approval"], per_suite={"bmaas/sanity": ["lgtm-label"]})
    v = plan(snap(p, labels=JIRA, reviews=(CR,), check_runs=without_gates(p)), p)
    bmaas = next(j.check for j in p.jobs.values() if j.suite == "bmaas/sanity" and j.needs_readiness)
    detail = {e.check: e.detail for e in v.jobs}
    assert detail[bmaas] == "waiting: needs the lgtm label"
    assert v.state is State.AWAITING_E2E_SIGNAL and v.next_action == "get the lgtm label"
    others = [c for c in gates(p) if c != bmaas]
    assert others and not any(detail[c].startswith("waiting:") for c in others)


def test_changes_requested_still_blocks() -> None:
    p = e2e(any_of=["coderabbit-approval"])
    blocked = Review("carol", "CHANGES_REQUESTED", "User", "2026-10-01T11:00:00Z", 3, HEAD)
    v = plan(snap(p, labels=JIRA, reviews=(CR, blocked), check_runs=without_gates(p)), p)
    assert v.state is State.AWAITING_E2E_SIGNAL and "CHANGES_REQUESTED" in v.headline


def test_a_finished_gate_needs_no_unlock() -> None:
    p = e2e(any_of=["coderabbit-approval"])
    assert plan(snap(p, labels=JIRA), p).state is State.READY_TO_ENQUEUE  # green gates: unchanged behavior
    assert with_check(p, gates(p)[0], None)


def test_an_open_change_request_gets_its_own_next_action_not_the_signal_list() -> None:
    p = e2e(any_of=["coderabbit-approval"])
    blocked = Review("carol", "CHANGES_REQUESTED", "User", "2026-10-01T11:00:00Z", 3, HEAD)
    v = plan(snap(p, labels=JIRA, reviews=(CR, blocked), check_runs=without_gates(p)), p)
    assert v.state is State.AWAITING_E2E_SIGNAL
    assert v.next_action == "address the requested changes; the reviewer approves again or dismisses their review"
    assert v.who_must_act == "author and the reviewer who requested changes"
    assert "approval" not in v.next_action.replace("approves", "")  # an approval cannot unlock it now
    assert {e.code for e in v.jobs if e.status is JobStatus.WAITING and e.detail.startswith("waiting:")} == {
        "changes-requested"
    }


def test_a_missing_signal_keeps_the_signal_next_action_and_its_own_code() -> None:
    p = e2e(any_of=["coderabbit-approval"])
    v = plan(snap(p, labels=JIRA, check_runs=without_gates(p)), p)
    assert v.next_action == "get a CodeRabbit approval on the current commit"
    assert {e.code for e in v.jobs if e.detail.startswith("waiting:")} == {"needs-signal"}


def test_the_legacy_ladder_is_untouched_by_the_reason_codes() -> None:
    legacy = with_sections(merge={"required_labels": ["jira/valid-reference"]})  # legacy ladder, no unlock section
    v = plan(snap(legacy, labels=JIRA, check_runs=without_gates(legacy)), legacy)
    assert v.state is State.AWAITING_E2E_SIGNAL
    assert v.next_action == "get /lgtm, /e2e-ready, or a CodeRabbit approval on the current head"
    assert {e.code for e in v.jobs} == {""}


def test_the_next_action_joins_different_suites_with_and_never_or() -> None:
    p = e2e(any_of=["coderabbit-approval"], per_suite={"bmaas/sanity": ["lgtm-label"]})
    v = plan(snap(p, labels=JIRA, check_runs=without_gates(p)), p)
    # vmaas and caas need CodeRabbit, bmaas needs the label: the PR needs BOTH, so "or" would mislead. vmaas and
    # caas have the same requirement, listed once. (Order follows the jobs, which the helper's YAML round trip sorts.)
    parts = v.next_action.removeprefix("get ").split(" and ")
    assert sorted(parts) == ["a CodeRabbit approval on the current commit", "the lgtm label"]
    assert " or " not in v.next_action


def test_alternatives_of_one_suite_stay_an_or_inside_parentheses_when_other_suites_add_requirements() -> None:
    p = e2e(any_of=["coderabbit-approval", "lgtm-label"], per_suite={"bmaas/sanity": ["lgtm-label"]})
    v = plan(snap(p, labels=JIRA, check_runs=without_gates(p)), p)
    assert sorted(v.next_action.removeprefix("get ").split(" and ")) == [
        "(a CodeRabbit approval on the current commit or the lgtm label)",
        "the lgtm label",
    ]


def test_a_single_requirement_has_no_parentheses() -> None:
    p = e2e(any_of=["coderabbit-approval", "lgtm-label"])
    v = plan(snap(p, labels=JIRA, check_runs=without_gates(p)), p)
    assert v.next_action == "get a CodeRabbit approval on the current commit or the lgtm label"


def test_the_next_action_follows_only_the_suites_that_are_still_locked() -> None:
    p = e2e(any_of=["coderabbit-approval"], per_suite={"bmaas/sanity": ["lgtm-label"]})
    v = plan(snap(p, labels=JIRA, reviews=(CR,), check_runs=without_gates(p)), p)  # CodeRabbit unlocks vmaas and caas
    assert v.next_action == "get the lgtm label"
