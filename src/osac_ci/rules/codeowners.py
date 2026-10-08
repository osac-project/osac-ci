"""CODEOWNERS parsing and lookup, with GitHub's semantics.

The file is read from the *base* branch by the adapter, never from the PR head, so a PR cannot make its author an
owner. The last matching rule wins. A pattern is gitignore-like: ``*`` stays inside one path segment, ``**`` crosses
directories, a leading ``/`` anchors to the repository root, a pattern without a slash matches at any depth, and a
pattern that names a directory covers everything below it. GitHub does not support negation or character classes in
this file, so neither do we.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Rule:
    pattern: str
    owners: tuple[str, ...]


def parse(text: str) -> tuple[Rule, ...]:
    """Parse CODEOWNERS text. Blank lines and ``#`` comments are skipped; a rule with no owners clears ownership."""
    rules: list[Rule] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern, *owners = re.split(r"\s+#", line, maxsplit=1)[0].split()
        rules.append(Rule(pattern, tuple(owners)))
    return tuple(rules)


def _translate(pattern: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    body = pattern.strip("/")
    prefix = "" if anchored else "(?:.*/)?"
    return re.compile(f"{prefix}{_translate(body)}(?:/.*)?\\Z")


def owners_of(rules: tuple[Rule, ...], path: str) -> tuple[str, ...] | None:
    """Owners of ``path`` from the last matching rule; ``None`` when no rule matches (the file has no owner)."""
    for rule in reversed(rules):
        if _compile(rule.pattern).match(path):
            return rule.owners or None
    return None
