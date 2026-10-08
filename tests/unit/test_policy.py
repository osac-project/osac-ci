from pathlib import Path

import pytest

from osac_ci.policy import PolicyError, load_policy, parse_policy

pytestmark = pytest.mark.unit

MINIMAL = """
version: 1
repo: o/r
jobs:
  a: {check: A}
"""


def test_minimal_policy_gets_defaults() -> None:
    policy = parse_policy(MINIMAL)
    job = policy.jobs["a"]
    assert job.kind == "cheap" and job.required_at == ("pr", "queue")
    assert "lgtm" in policy.merge.required_labels


def test_toy_policy_is_valid_and_not_osac(toy_policy) -> None:  # type: ignore[no-untyped-def]
    assert toy_policy.repo == "example/toy"
    assert toy_policy.merge.required_labels == ("approved",)


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        (MINIMAL.replace("jobs:", "extra: 1\njobs:"), "extra"),
        (MINIMAL.replace("version: 1", "version: 2"), "version"),
        (MINIMAL.replace("a: {check: A}", "A_Bad: {check: A}"), "must match"),
        (MINIMAL.replace("a: {check: A}", "a: {check: A}\n  b: {check: A}"), "same check"),
        (MINIMAL.replace("{check: A}", "{check: A, needs_readiness: true}"), "kind: e2e"),
        (MINIMAL.replace("{check: A}", "{check: A, required_at: []}"), "at least one"),
        (MINIMAL.replace("{check: A}", "{check: A, required_at: [pr, pr]}"), "repeat"),
        (MINIMAL.replace("{check: A}", "{check: A, required_at: [merge]}"), "required_at"),
        (MINIMAL.replace("a: {check: A}", "{}"), "jobs"),
        (MINIMAL.replace("{check: A}", "{check: ''}"), "check"),
        (MINIMAL.replace("{check: A}", "{check: A, typo_field: 1}"), "typo_field"),
    ],
)
def test_invalid_policies_are_rejected_with_a_useful_message(text: str, fragment: str) -> None:
    with pytest.raises(PolicyError) as err:
        parse_policy(text)
    assert fragment in str(err.value)


def test_not_yaml_and_not_a_mapping() -> None:
    with pytest.raises(PolicyError, match="not valid YAML"):
        parse_policy("a: [unclosed")
    with pytest.raises(PolicyError, match="mapping"):
        parse_policy("- just\n- a list\n")


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="cannot read"):
        load_policy(tmp_path / "nope.yml")
