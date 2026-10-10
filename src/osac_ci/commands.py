"""The syntax of the comment commands, shared by the handler that records them and the code that reads them back."""

from __future__ import annotations

import re
import unicodedata

OVERRIDE_COMMAND = "/override"
OVERRIDE_START = re.compile(rf"^{re.escape(OVERRIDE_COMMAND)}(?:\s|$)")
REASON_MIN, REASON_MAX = 10, 140
_OVERRIDE = re.compile(rf"^{re.escape(OVERRIDE_COMMAND)}[ \t]+(?P<sha>[0-9a-fA-F]{{40}})[ \t]+(?P<reason>\S[^\r\n]*)$")


def normalize_reason(raw: str) -> str | None:
    """The reason as it will be stored and shown, or ``None`` if it is not acceptable.

    It is normalized first (NFKC, whitespace collapsed to single spaces) and judged after: ten characters of padding
    around three real ones are three characters. Only printable characters are allowed, so control characters,
    zero-width characters and text-direction overrides are refused, and the result is 10 to 140 characters."""
    text = " ".join(unicodedata.normalize("NFKC", raw).split())
    if not text.isprintable() or not REASON_MIN <= len(text) <= REASON_MAX:
        return None
    return text


def parse_override(text: str) -> tuple[str, str] | None:
    """``(full sha in lower case, reason)`` of a complete ``/override <full sha> <reason>`` comment, else ``None``.
    The reason is one acceptable line (see ``normalize_reason``); a short SHA never matches (a 7-digit prefix can be
    forged)."""
    found = _OVERRIDE.match(text.strip())
    if found is None:
        return None
    reason = normalize_reason(found["reason"])
    return None if reason is None else (found["sha"].lower(), reason)
