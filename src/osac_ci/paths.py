"""Glob matching for changed files, the way `dorny/paths-filter` evaluates OSAC's `ci-filters.yml`.

The rules below were taken from the real action (picomatch with ``dot: true`` and ``predicate-quantifier: every``)
by running it over a corpus of paths in the sandbox, and are held to it by ``tests/contract/test_paths_oracle.py``:

* ``*`` and ``?`` stay inside one path segment and also match dotfiles.
* ``**`` as a whole segment crosses directories. In the middle or at the start of a pattern it matches zero or more
  directories; at the end (``docs/**``) it also matches ``docs`` itself.
* ``{a,b}`` is alternation. A ``**/`` at the very start of a braced pattern needs at least one directory, so
  ``{**/*.yaml,x}`` does not match a root-level ``a.yaml``. This differs from a bare ``**/*.yaml``, which does.
* Under ``every`` a file matches a filter when it matches every positive pattern and none of the ``!`` patterns.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache


def expand_braces(pattern: str) -> list[str]:
    """Expand ``{a,b}`` groups (nested too) into plain patterns."""
    found = re.search(r"\{([^{}]*)\}", pattern)
    if not found:
        return [pattern]
    out: list[str] = []
    for alternative in found.group(1).split(","):
        out += expand_braces(pattern[: found.start()] + alternative + pattern[found.end() :])
    return out


def _segment(text: str) -> str:
    return "".join("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch) for ch in re.sub(r"\*+", "*", text))


@lru_cache(maxsize=2048)
def _compile(glob: str, braced: bool) -> re.Pattern[str]:
    segments = glob.split("/")
    parts: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            if last and parts:
                parts[-1] = parts[-1].rstrip("/") + "(?:/.*)?"
            elif last:
                parts.append(".*")
            elif index == 0 and braced:
                parts.append("(?:[^/]+/)+")
            else:
                parts.append("(?:[^/]+/)*")
            continue
        parts.append(_segment(segment) + ("" if last else "/"))
    return re.compile("".join(parts) + r"\Z")


def matches(pattern: str, path: str) -> bool:
    """True when ``path`` matches the glob ``pattern`` (braces allowed, no ``!`` prefix)."""
    braced = "{" in pattern
    return any(_compile(glob, braced).match(path) is not None for glob in expand_braces(pattern))


def any_match(patterns: Iterable[str], path: str) -> bool:
    return any(matches(p, path) for p in patterns)


def filter_matches(patterns: Sequence[str], path: str) -> bool:
    """One path against one named filter under ``predicate-quantifier: every``."""
    for pattern in patterns:
        if pattern.startswith("!"):
            if matches(pattern[1:], path):
                return False
        elif not matches(pattern, path):
            return False
    return True


def matching_filters(filters: Mapping[str, Sequence[str]], path: str) -> list[str]:
    return sorted(name for name, patterns in filters.items() if filter_matches(patterns, path))


def filter_applies(files: Iterable[str], patterns: Sequence[str]) -> bool:
    """A named filter is true when at least one changed file matches it."""
    return any(filter_matches(patterns, f) for f in files)


def filters_hold(
    files: Sequence[str], filters: Mapping[str, Sequence[str]], all_of: Sequence[str], any_of: Sequence[str]
) -> bool:
    """Do the named filters of a job hold for these changed files? Every name in ``all_of`` and, when ``any_of`` is
    given, at least one of its names must match some changed file (how the workflows combine their gates)."""
    return all(filter_applies(files, filters[n]) for n in all_of) and (
        not any_of or any(filter_applies(files, filters[n]) for n in any_of)
    )


def applicable(files: Iterable[str], include: Iterable[str], exclude: Iterable[str]) -> bool:
    """True when at least one changed file is included and not excluded. No include globs means always applicable."""
    include = tuple(include)
    exclude = tuple(exclude)
    if not include:
        return True
    return any(any_match(include, f) and not any_match(exclude, f) for f in files)
