"""The mark of a fast track: who skipped the tests of a commit, and why.

It is written into the text of the ``CI Autopilot`` check of the commit, which only the OSAC CI app can write, so the
check is its own proof and nothing else needs to remember it. The mark names the commit it was made for: a new commit
of the pull request has no mark, and the mark of an older one is of no use. The reason is free text typed by a
person, so the mark carries it encoded, never as part of the page's markup.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

REASON_MIN = 5
REASON_MAX = 200
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_SHA = re.compile(r"[0-9a-f]{40}")
_MARK = re.compile(r"<!-- fast-track:([A-Za-z0-9_-]+) -->")


@dataclass(frozen=True)
class FastTrack:
    sha: str
    login: str
    reason: str

    @property
    def title(self) -> str:
        return f"Fast track: skipped by {self.login}: {self.reason}"


def clean_reason(text: str) -> str:
    """One line of at most ``REASON_MAX`` characters, without the characters that make markup."""
    one = " ".join(re.sub(r"[<>`\[\]]", " ", text).split())
    return one[:REASON_MAX].rstrip()


def encode(fast: FastTrack) -> str:
    body = json.dumps({"sha": fast.sha, "login": fast.login, "reason": fast.reason}, separators=(",", ":"))
    return f"<!-- fast-track:{base64.urlsafe_b64encode(body.encode()).decode().rstrip('=')} -->"


def decode(text: str, sha: str) -> FastTrack | None:
    """The mark in the text of a check, if it is well formed and made for ``sha``; None for anything else."""
    found = _MARK.search(text or "")
    if found is None:
        return None
    raw = found.group(1)
    try:
        data: Any = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(data, Mapping):
        return None
    mark_sha, login, reason = data.get("sha"), data.get("login"), data.get("reason")
    if not (isinstance(mark_sha, str) and isinstance(login, str) and isinstance(reason, str)):
        return None
    if mark_sha != sha or not _SHA.fullmatch(mark_sha) or not _LOGIN.fullmatch(login):
        return None
    if reason != clean_reason(reason) or len(reason) < REASON_MIN:
        return None
    return FastTrack(mark_sha, login, reason)
