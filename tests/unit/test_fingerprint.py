import pytest

from osac_ci.fingerprint import fingerprint

pytestmark = pytest.mark.unit

DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -10,4 +10,5 @@ def main():
     keep()
-    old()
+    new()
+    more()
     keep_too()
"""


def test_context_line_numbers_and_blob_ids_do_not_matter() -> None:
    rebased = (
        DIFF.replace("@@ -10,4 +10,5 @@", "@@ -42,4 +43,5 @@")
        .replace("1111111..2222222", "aaaaaaa..bbbbbbb")
        .replace("    keep()\n", "    something_else_now_above()\n")
    )
    assert fingerprint(rebased) == fingerprint(DIFF)


@pytest.mark.parametrize(
    "changed",
    [
        DIFF.replace("+    new()", "+    newer()"),
        DIFF.replace("+    new()", "+  new()"),  # re-indenting is a change: it can alter meaning
        DIFF.replace("-    old()\n", ""),
        DIFF.replace("b/app.py", "b/other.py"),
        DIFF.replace("100644", "100755"),
        DIFF + "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
    ],
)
def test_any_change_to_the_changes_is_a_different_fingerprint(changed: str) -> None:
    assert fingerprint(changed) != fingerprint(DIFF)


def test_hunk_order_inside_the_diff_is_kept() -> None:
    other = DIFF.replace("-    old()\n+    new()\n", "+    new()\n-    old()\n")
    assert fingerprint(other) != fingerprint(DIFF)


def test_binary_payload_counts() -> None:
    a = "diff --git a/i.png b/i.png\nGIT binary patch\nliteral 3\nAbc\n"
    assert fingerprint(a) != fingerprint(a.replace("Abc", "Abd"))


def test_empty_diff_is_stable() -> None:
    assert fingerprint("") == fingerprint("")
