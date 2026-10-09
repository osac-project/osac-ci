"""Approvers from a Prow-style OWNERS file, in the shapes the OSAC components use."""

import pytest

from osac_ci.rules import owners

pytestmark = pytest.mark.unit

PLAIN = "approvers:\n- eliorerz\n- RawAgner\nreviewers:\n- someone-else\n"
FILTERS = """\
filters:
  "[^.]":
    approvers:
    - jhernand
    - Adriengentil
    reviewers:
    - reviewer-only
  ".*\\\\.md$":
    approvers:
    - docs-person
"""


def test_a_plain_approvers_list() -> None:
    assert owners.approvers(PLAIN) == {"eliorerz", "rawagner"}  # lower case, reviewers ignored


def test_approvers_under_every_filter_count() -> None:
    assert owners.approvers(FILTERS) == {"jhernand", "adriengentil", "docs-person"}


def test_both_shapes_together() -> None:
    assert owners.approvers(PLAIN + FILTERS) == {"eliorerz", "rawagner", "jhernand", "adriengentil", "docs-person"}


@pytest.mark.parametrize(
    "text", ["", "\n", "reviewers:\n- only\n", "approvers: []\n", "filters: {}\n", "options: {x: 1}\n"]
)
def test_a_file_without_approvers_names_nobody(text: str) -> None:
    assert owners.approvers(text) == frozenset()


@pytest.mark.parametrize(
    "text",
    [
        "- just\n- a list\n",
        "approvers: bob\n",
        "approvers: [1, 2]\n",
        "approvers: ['']\n",
        "filters: [a, b]\n",
        "filters: {x: [a]}\n",
        "approvers: [a\n",
        "approvers: [a]\napprovers: [b]\n",  # a repeated key
    ],
)
def test_a_file_of_an_unknown_shape_is_an_error(text: str) -> None:
    with pytest.raises(ValueError):
        owners.approvers(text)


def test_whitespace_around_a_login_is_ignored() -> None:
    assert owners.approvers("approvers:\n- ' bob '\n") == {"bob"}


def test_the_real_osac_owners_files_parse() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "osac"
    for name in ("osac-ui/OWNERS", "osac-operator/OWNERS", "OWNERS", "bare-metal-fulfillment-operator/OWNERS"):
        path = root / name
        if path.is_file():
            assert owners.approvers(path.read_text())  # every component lists someone
