import pytest

from osac_ci.paths import applicable, matches

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("docs/**", "docs/a/b.md", True),
        ("docs/**", "docs", True),  # picomatch: a trailing /** also matches the name itself
        ("docs/**", "other/docs/a.md", False),
        ("**/*.md", "README.md", True),
        ("**/*.md", "a/b/c.md", True),
        ("**/*.md", "a/b/c.txt", False),
        ("*.go", "main.go", True),
        ("*.go", "cmd/main.go", False),
        ("a?c", "abc", True),
        ("a?c", "a/c", False),
        ("osac-ui/**", "osac-ui/pnpm-lock.yaml", True),
        ("a.b", "aXb", False),
        ("{a/**,b/**}", "b/x/y", True),
        ("{**/*.yaml,z}", "x.yaml", False),
        ("{**/*.yaml,z}", "d/x.yaml", True),
        ("**/*.yaml", "x.yaml", True),
        ("a/**/b", "a/b", True),
        ("*.yaml", ".yaml", True),
    ],
)
def test_matches(pattern: str, path: str, expected: bool) -> None:
    assert matches(pattern, path) is expected


def test_no_include_globs_means_always_applicable() -> None:
    assert applicable(["x"], [], [])
    assert applicable([], [], [])


def test_include_and_exclude() -> None:
    assert applicable(["docs/a.md"], ["docs/**"], [])
    assert not applicable(["docs/a.md"], ["docs/**"], ["**/*.md"])
    assert applicable(["docs/a.md", "docs/b.txt"], ["docs/**"], ["**/*.md"])
    assert not applicable([], ["docs/**"], [])
