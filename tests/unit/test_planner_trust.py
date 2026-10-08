"""The planner with a `trust:` policy: who may start expensive jobs from a fork."""

import pytest
from helpers import HEAD, snap, with_check

from osac_ci.model import State
from osac_ci.planner import plan
from osac_ci.policy import Policy, Trust, parse_policy

pytestmark = pytest.mark.unit


@pytest.fixture
def sha_bound(osac_policy: Policy) -> Policy:
    return osac_policy.model_copy(update={"trust": Trust(authorization="sha-bound")})


def gate_missing(policy: Policy):  # type: ignore[no-untyped-def]
    gate = next(j.check for j in policy.jobs.values() if j.needs_readiness)
    return with_check(policy, gate, None)


def test_policy_parses_a_trust_section() -> None:
    p = parse_policy(
        'version: 1\nrepo: o/r\ntrust: {authorization: sha-bound, trusted_bots: ["dependabot[bot]"]}\n'
        "jobs: {a: {check: a}}\n"
    )
    assert p.trust == Trust(
        authorization="sha-bound", trusted_bots=("dependabot[bot]",), check_name="OSAC CI authorization"
    )


def test_trust_defaults_to_the_label_behavior(osac_policy: Policy) -> None:
    assert osac_policy.trust == Trust() and osac_policy.trust.authorization == "label"


@pytest.mark.parametrize("bad", ["authorization: always", "surprise: 1", "check_name: ''"])
def test_trust_rejects_unknown_values(bad: str) -> None:
    from osac_ci.policy import PolicyError

    with pytest.raises(PolicyError):
        parse_policy(f"version: 1\nrepo: o/r\ntrust: {{{bad}}}\njobs: {{a: {{check: a}}}}\n")


def test_legacy_mode_keeps_the_old_next_action(osac_policy: Policy) -> None:
    v = plan(
        snap(osac_policy, is_fork=True, author="x", fork_owner="x", check_runs=gate_missing(osac_policy)), osac_policy
    )
    assert v.state is State.NEEDS_AUTHORIZATION
    assert v.next_action == "an org member comments /ok-to-test"


def test_sha_bound_mode_names_the_exact_command(sha_bound: Policy) -> None:
    s = snap(sha_bound, is_fork=True, author="x", fork_owner="x", check_runs=gate_missing(sha_bound))
    v = plan(s, sha_bound)
    assert v.state is State.NEEDS_AUTHORIZATION
    assert f"`/ok-to-test {HEAD[:7]}`" in v.next_action and "exactly this commit" in v.next_action
    assert v.who_must_act == "org member"


def test_the_label_does_not_unlock_in_sha_bound_mode(sha_bound: Policy) -> None:
    labels = snap(sha_bound).labels | {"ok-to-test"}
    s = snap(sha_bound, is_fork=True, author="x", fork_owner="x", labels=labels, check_runs=gate_missing(sha_bound))
    assert plan(s, sha_bound).state is State.NEEDS_AUTHORIZATION


def test_an_authorization_of_this_commit_lets_the_pr_proceed(sha_bound: Policy) -> None:
    s = snap(
        sha_bound,
        is_fork=True,
        author="x",
        fork_owner="x",
        authorized_by="reviewer",
        check_runs=gate_missing(sha_bound),
    )
    assert plan(s, sha_bound).state is not State.NEEDS_AUTHORIZATION
