"""Glob matching for changed files (the subset the OSAC path filters use).

``**`` crosses directories, ``*`` and ``?`` stay inside one path segment. This is deliberately small; its fidelity
to the real `dorny/paths-filter` evaluation of `.github/filters/ci-filters.yml` is a tracked parity item
(oracle corpus generated in the sandbox), not an assumption.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches(pattern: str, path: str) -> bool:
    return _compile(pattern).match(path) is not None


def any_match(patterns: Iterable[str], path: str) -> bool:
    return any(matches(p, path) for p in patterns)


def applicable(files: Iterable[str], include: Iterable[str], exclude: Iterable[str]) -> bool:
    """True when at least one changed file is included and not excluded. No include globs means always applicable."""
    include = tuple(include)
    exclude = tuple(exclude)
    if not include:
        return True
    return any(any_match(include, f) and not any_match(exclude, f) for f in files)
