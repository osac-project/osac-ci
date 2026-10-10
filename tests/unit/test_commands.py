"""The /override command syntax: what is a complete command, and what reason is acceptable."""

import pytest

from osac_ci.commands import normalize_reason, parse_override

pytestmark = pytest.mark.unit
SHA = "ab" * 20
GOOD = "release is blocked"


def cmd(reason: str, sha: str = SHA) -> str:
    return f"/override {sha} {reason}"


def test_a_complete_command_gives_the_sha_and_the_reason() -> None:
    assert parse_override(cmd(GOOD)) == (SHA, GOOD)


def test_the_sha_is_lower_cased_and_surrounding_space_is_ignored() -> None:
    assert parse_override("  \n" + cmd(GOOD, SHA.upper()) + "  \n") == (SHA, GOOD)


@pytest.mark.parametrize("sha", [SHA[:7], SHA[:39], SHA + "a", "g" * 40, ""])
def test_only_a_full_hex_sha_is_accepted(sha: str) -> None:
    assert parse_override(cmd(GOOD, sha)) is None


@pytest.mark.parametrize(
    "text", ["", "/override", f"/override {SHA}", f"/override {SHA}   ", f"/overrides {SHA} {GOOD}"]
)
def test_incomplete_commands_are_refused(text: str) -> None:
    assert parse_override(text) is None


def test_the_reason_must_be_on_one_line() -> None:
    assert parse_override(cmd(GOOD) + "\nand more") is None
    assert parse_override(f"/override {SHA}\n{GOOD}") is None


# ---- the review case: the length is judged after normalization --------------------------------------------------


def test_padding_around_a_short_reason_does_not_make_it_long_enough() -> None:
    assert parse_override(cmd("x        y")) is None  # ten characters raw, three after collapsing
    assert parse_override(cmd("x" + " " * 30 + "y")) is None
    assert normalize_reason("x        y") is None


def test_tabs_and_runs_of_spaces_collapse_to_one_space() -> None:
    assert normalize_reason("two\t\t words   here   now") == "two words here now"


@pytest.mark.parametrize(("length", "ok"), [(9, False), (10, True), (140, True), (141, False)])
def test_the_limits_apply_to_the_normalized_reason(length: int, ok: bool) -> None:
    assert (parse_override(cmd("a" * length)) is not None) is ok


def test_a_reason_that_is_long_only_because_of_padding_is_refused_after_collapsing() -> None:
    assert parse_override(cmd("abc " + " " * 400 + "def")) is None  # 7 real characters


def test_a_reason_that_is_short_raw_but_long_enough_is_accepted() -> None:
    assert parse_override(cmd("ab  cd  ef  gh")) == (SHA, "ab cd ef gh")


@pytest.mark.parametrize(
    "bad",
    [
        "release\x07is blocked now",  # a control character
        "release\x00is blocked now",
        "release\x1bis blocked now",
        "release\u200bis blocked now",  # zero width space
        "release\u200dis blocked now",  # zero width joiner
        "release\u202eis blocked now",  # a text direction override
        "release\u2066is blocked now",  # a text direction isolate
        "release\ufeffis blocked now",  # a byte order mark
        "release\U000e0041is blocked now",  # a tag character
        "release\ue000is blocked now",  # a private use character
    ],
)
def test_invisible_and_control_characters_are_refused(bad: str) -> None:
    assert normalize_reason(bad) is None
    assert parse_override(cmd(bad)) is None


@pytest.mark.parametrize("space", ["\u00a0", "\u2003", "\u3000", "\u2028", "\u2029"])
def test_unusual_spaces_become_ordinary_ones(space: str) -> None:
    assert normalize_reason(f"release{space}is{space}blocked now") == "release is blocked now"


def test_compatibility_forms_are_normalized() -> None:
    assert normalize_reason("ＡＢＣＤ ＥＦＧＨＩ") == "ABCD EFGHI"  # full-width letters, a no-break space


def test_non_ascii_letters_are_fine() -> None:
    assert normalize_reason("שחרור חסום עד מחר בבוקר") == "שחרור חסום עד מחר בבוקר"


def test_a_huge_comment_is_refused_quickly() -> None:
    import time

    started = time.perf_counter()
    assert parse_override(cmd("a" * 65_000)) is None
    assert parse_override(cmd("a b " * 16_000)) is None
    assert parse_override(cmd(" " * 65_000 + "x")) is None
    assert time.perf_counter() - started < 1.0  # linear work, no backtracking
