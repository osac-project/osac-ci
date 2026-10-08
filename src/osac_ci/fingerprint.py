"""A fingerprint of what a pull request changes, independent of where the base branch is.

Two commits of a PR that were rebased onto a newer base have different SHAs and different surrounding context but
the same fingerprint, as long as the lines the PR adds and removes are the same. Context lines, hunk line numbers and
blob ids are left out; file names, modes, renames, the added and removed lines and binary payloads are kept.
Whitespace is *not* normalized: re-indenting changes meaning in YAML and Python, so it must count as a change.
"""

from __future__ import annotations

import hashlib


def _mode_of_index_line(line: str) -> str:
    """``index 1111111..2222222 100644`` -> ``index 100644``: blob ids depend on the base, the file mode does not."""
    parts = line.split()
    return f"index {parts[2]}" if len(parts) > 2 else "index"


def fingerprint(diff: str) -> str:
    """SHA-1 hex digest of the normalized changes in a unified diff (as returned by ``git diff`` or the GitHub API)."""
    kept: list[str] = []
    in_hunk = False
    in_binary = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = in_binary = False
            kept.append(line)
        elif in_binary:
            kept.append(line)
        elif line.startswith("GIT binary patch"):
            in_binary = True
            kept.append(line)
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk:
            # context lines (leading space) are not part of the change; "\ No newline at end of file" is
            if line[:1] in ("+", "-") or line.startswith("\\"):
                kept.append(line)
        elif line.startswith("index "):
            kept.append(_mode_of_index_line(line))
        elif not line.startswith("similarity index"):
            kept.append(line)
    return hashlib.sha1("\n".join(kept).encode("utf-8", "surrogateescape"), usedforsecurity=False).hexdigest()
