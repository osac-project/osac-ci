"""Approvers from a Prow-style OWNERS file.

OSAC components list their maintainers in an OWNERS file, either as a top-level ``approvers`` list or, per path
pattern, under ``filters``. For "may this person approve a change to the component's own files" every approver counts,
whatever pattern lists them. ``reviewers`` and aliases are not approvers and are ignored. Logins are compared in lower
case, as GitHub does.
"""

from __future__ import annotations

from typing import Any

import yaml

from osac_ci import yamlio


def approvers(text: str) -> frozenset[str]:
    """The approvers named in an OWNERS file. Raises ``ValueError`` when the file has an unknown shape."""
    try:
        raw: Any = yamlio.load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"OWNERS is not valid YAML: {exc}") from exc
    if raw is None:
        return frozenset()
    if not isinstance(raw, dict):
        raise ValueError("OWNERS must be a YAML mapping")
    lists: list[Any] = [raw.get("approvers")]
    filters = raw.get("filters")
    if filters is not None:
        if not isinstance(filters, dict):
            raise ValueError("OWNERS filters must be a mapping of pattern to owners")
        for section in filters.values():
            if not isinstance(section, dict):
                raise ValueError("each OWNERS filter must be a mapping")
            lists.append(section.get("approvers"))
    found: set[str] = set()
    for entries in lists:
        if entries is None:
            continue
        if not isinstance(entries, list) or not all(isinstance(e, str) and e.strip() for e in entries):
            raise ValueError("OWNERS approvers must be a list of logins")
        found |= {e.strip().lower() for e in entries}
    return frozenset(found)
