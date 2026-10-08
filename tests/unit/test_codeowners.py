import pytest

from osac_ci.rules.codeowners import owners_of, parse

pytestmark = pytest.mark.unit

TEXT = """
# default owners
*                      @org/everyone
/docs/                 @docs-team   # trailing comment
*.go                   @gophers @org/backend
/api/**/schema.json    @api-owners
internal/secret        @security
/vendor/
"""


def owners(path: str) -> tuple[str, ...] | None:
    return owners_of(parse(TEXT), path)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("README.md", ("@org/everyone",)),
        ("docs/guide.md", ("@docs-team",)),
        ("docs/deep/er/guide.md", ("@docs-team",)),
        ("sub/docs/guide.md", ("@org/everyone",)),  # /docs/ is anchored to the root
        ("main.go", ("@gophers", "@org/backend")),
        ("cmd/tool/main.go", ("@gophers", "@org/backend")),  # last matching rule wins over '*'
        ("api/v1/schema.json", ("@api-owners",)),
        ("api/schema.json", ("@api-owners",)),
        ("internal/secret/key.txt", ("@security",)),
    ],
)
def test_last_matching_rule_wins(path: str, expected: tuple[str, ...] | None) -> None:
    assert owners(path) == expected


def test_a_slash_in_the_pattern_anchors_it_to_the_root() -> None:
    assert owners("a/internal/secret") == ("@org/everyone",)


def test_a_rule_without_owners_removes_ownership() -> None:
    assert owners("vendor/lib/x.go") is None


def test_file_with_no_matching_rule_has_no_owner() -> None:
    assert owners_of(parse("/only/here @a"), "elsewhere/x") is None


def test_comments_and_blank_lines_are_skipped() -> None:
    assert parse("\n# nothing\n\n") == ()


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("/src/**/gen/", "src/a/b/gen/x.go", True),
        ("/src/**/gen/", "src/gen/x.go", True),
        ("/src/**/gen/", "src/other/x.go", False),
        ("**/vendor", "a/b/vendor/lib.go", True),
        ("/a/**", "a/b/c", True),
        ("file?.txt", "file1.txt", True),
        ("file?.txt", "file12.txt", False),
        ("/*.md", "README.md", True),
        ("/*.md", "docs/README.md", False),
    ],
)
def test_pattern_forms(pattern: str, path: str, expected: bool) -> None:
    assert (owners_of(parse(f"{pattern} @x"), path) == ("@x",)) is expected
