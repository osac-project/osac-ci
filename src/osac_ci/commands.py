"""The syntax of the comment commands, shared by the handler that records them and the code that reads them back."""

from __future__ import annotations

import re

OVERRIDE_COMMAND = "/override"
OVERRIDE_START = re.compile(rf"^{re.escape(OVERRIDE_COMMAND)}(?:\s|$)")
_OVERRIDE = re.compile(
    rf"^{re.escape(OVERRIDE_COMMAND)}[ \t]+(?P<sha>[0-9a-fA-F]{{40}})[ \t]+(?P<reason>\S[^\r\n]{{9,139}})[ \t]*$"
)


def parse_override(text: str) -> tuple[str, str] | None:
    """``(full sha in lower case, reason)`` of a complete ``/override <full sha> <reason>`` comment, else ``None``.
    The reason is one line of 10 to 140 characters; a short SHA never matches (a 7-digit prefix can be forged)."""
    found = _OVERRIDE.match(text.strip())
    if found is None:
        return None
    return found["sha"].lower(), " ".join(found["reason"].split())
