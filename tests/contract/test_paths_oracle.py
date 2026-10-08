"""The planner's path matching must give the same answers as the real dorny/paths-filter.

``data/paths-oracle.json`` was produced by running the real action (picomatch, ``predicate-quantifier: every``) over
2,658 paths against every filter in ``data/ci-filters.yml``: real changed files of recent OSAC PRs, paths generated from
each pattern, and hand-written edge cases. This test replays that corpus.
"""

import json
from pathlib import Path

import pytest
import yaml

from osac_ci.paths import filter_applies, filter_matches, matching_filters

pytestmark = pytest.mark.contract

DATA = Path(__file__).parent / "data"
ORACLE = json.loads((DATA / "paths-oracle.json").read_text(encoding="utf-8"))
FILTERS: dict[str, list[str]] = yaml.safe_load((DATA / "ci-filters.yml").read_text(encoding="utf-8"))


def test_oracle_covers_every_filter() -> None:
    assert sorted(ORACLE["filters"]) == sorted(FILTERS)
    assert len(ORACLE["files"]) > 2000


def test_every_path_matches_the_same_filters_as_the_real_action() -> None:
    wrong = {
        path: {"real": ORACLE["matches"].get(path, []), "ours": matching_filters(FILTERS, path)}
        for path in ORACLE["files"]
        if matching_filters(FILTERS, path) != sorted(ORACLE["matches"].get(path, []))
    }
    assert wrong == {}


def test_each_filter_is_true_for_a_pr_exactly_when_the_real_action_says_so() -> None:
    # A filter is true for a PR when any changed file matches; check it on the paths of whole directories at once.
    files = ORACLE["files"]
    for name, patterns in FILTERS.items():
        real = {f for f in files if name in ORACLE["matches"].get(f, [])}
        assert filter_applies(files, patterns) is bool(real)
        assert filter_applies(sorted(set(files) - real), patterns) is False


@pytest.mark.parametrize(
    ("filter_name", "path", "expected"),
    [
        # docs-only and generated-code changes must not trigger the code filters
        ("code", "docs/guide.md", False),
        ("code", "README.md", False),
        ("code", "fulfillment-service/internal/server.go", True),
        ("code", "osac-ui/src/app.ts", False),
        ("ui", "osac-ui/src/app.ts", True),
        # a negation is a veto: the same path must satisfy every entry
        ("ambiguous-shared", "proto/gen/go/x.pb.go", False),
        ("ambiguous-shared", "proto/private/osac/private/v1/subnet.proto", True),
        ("vmaas-clear", "tests/e2e/references/test_cluster_baremetal_references.py", False),
        ("caas-clear", "tests/e2e/references/test_cluster_baremetal_references.py", True),
        # a leading ** inside braces needs a directory: root-level config files do not match
        ("config-yaml-json", "x.yaml", False),
        ("config-yaml-json", "deploy/x.yaml", True),
        # a trailing /** also matches the name itself
        ("code", "a/docs", False),
    ],
)
def test_known_decisions(filter_name: str, path: str, expected: bool) -> None:
    assert filter_matches(FILTERS[filter_name], path) is expected
