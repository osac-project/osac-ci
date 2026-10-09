"""Policy files refuse a repeated key, so a later line cannot quietly cancel an earlier one."""

import pytest
import yaml

from osac_ci import yamlio
from osac_ci.policy import PolicyError, load_policy, parse_policy
from osac_ci.replay import load_explained

pytestmark = pytest.mark.unit

BASE = "version: 1\nrepo: o/r\njobs: {a: {check: a}}\n"
RULE = "protected_paths:\n  - {paths: ['.github/**'], approvers: ['@o/infra']}\n"


def test_a_repeated_top_level_key_is_an_error() -> None:
    """The case from review: a later empty list must not override the configured rule."""
    with pytest.raises(PolicyError, match="duplicate key 'protected_paths'"):
        parse_policy(BASE + RULE + "protected_paths: []\n")


def test_the_same_policy_without_the_repeat_keeps_its_rule() -> None:
    assert len(parse_policy(BASE + RULE).protected_paths) == 1


@pytest.mark.parametrize(
    "text",
    [
        "a: 1\na: 2\n",
        "outer:\n  b: 1\n  b: 2\n",
        "list:\n  - {x: 1, x: 2}\n",
        "flow: {k: 1, k: 2}\n",
        "1: a\n1: b\n",  # equal after parsing, whatever the spelling
        "'a': 1\na: 2\n",
    ],
)
def test_a_repeated_key_is_found_at_any_depth(text: str) -> None:
    with pytest.raises(yaml.YAMLError, match="duplicate key"):
        yamlio.load(text)


def test_different_keys_and_the_same_key_in_different_mappings_are_fine() -> None:
    assert yamlio.load("a: {k: 1}\nb: {k: 2}\n") == {"a": {"k": 1}, "b": {"k": 2}}


def test_a_key_that_overrides_a_merge_is_not_a_duplicate() -> None:
    text = "base: &b {x: 1, y: 2}\nuse:\n  <<: *b\n  x: 9\n"
    assert yamlio.load(text)["use"] == {"x": 9, "y": 2}


def test_values_keep_their_safe_types_and_anchors_work() -> None:
    assert yamlio.load("a: &v [1, 2]\nb: *v\nc: true\nd: ~\n") == {"a": [1, 2], "b": [1, 2], "c": True, "d": None}


def test_python_object_tags_are_still_refused() -> None:
    with pytest.raises(yaml.YAMLError):
        yamlio.load("x: !!python/object/apply:os.system ['true']\n")


def test_a_repeated_path_filter_in_the_filters_file_is_an_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "filters.yml").write_text("code:\n  - 'a/**'\ncode:\n  - 'b/**'\n")
    (tmp_path / "p.yml").write_text(BASE + "path_filters: {file: filters.yml}\n")
    with pytest.raises(PolicyError, match="duplicate key 'code'"):
        load_policy(tmp_path / "p.yml")


def test_a_repeated_pr_in_the_explained_list_is_an_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "explained.yaml"
    path.write_text("12: first reason\n12: second reason\n")
    with pytest.raises(ValueError, match="duplicate key 12"):
        load_explained(path)
