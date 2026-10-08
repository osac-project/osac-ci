"""Locate the real legacy sources for the differential tests.

Resolution order for each source:

1. an explicit file path from the environment (``OSAC_AUTO_QUEUE_SH`` / ``OSAC_CHECK_E2E_READINESS_SH``);
2. the file at a git ref in the sibling checkout (default ``upstream/main`` for osac, ``origin/main`` for
   osac-test-infra), so the result does not depend on which branch happens to be checked out;
3. the working-tree file of the sibling checkout.

A missing source or tool skips the test loudly instead of passing silently.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from helpers import ROOT

SIBLINGS = ROOT.parent
_CACHE = Path(tempfile.mkdtemp(prefix="osac-ci-legacy-"))


def _from_git(repo: Path, ref: str, relative: str) -> Path | None:
    if not (repo / ".git").exists():
        return None
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{relative}"], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    dest = _CACHE / ref.replace("/", "_") / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(proc.stdout)
    return dest


def resolve(env: str, repo_name: str, ref_env: str, default_ref: str, relative: str) -> Path:
    explicit = os.environ.get(env)
    if explicit:
        return Path(explicit)
    repo = SIBLINGS / repo_name
    from_git = _from_git(repo, os.environ.get(ref_env, default_ref), relative)
    return from_git or repo / relative


AUTO_QUEUE = resolve("OSAC_AUTO_QUEUE_SH", "osac", "OSAC_REF", "upstream/main", ".github/scripts/auto-queue.sh")
READINESS = resolve(
    "OSAC_CHECK_E2E_READINESS_SH",
    "osac-test-infra",
    "OSAC_TEST_INFRA_REF",
    "origin/main",
    ".github/actions/check-e2e-readiness/check-e2e-readiness.sh",
)


def _unavailable(reason: str) -> None:
    """Skip locally, but FAIL when OSAC_CI_REQUIRE_LEGACY=1 (set in CI) so a missing source can never pass silently."""
    if os.environ.get("OSAC_CI_REQUIRE_LEGACY") == "1":
        pytest.fail(f"legacy differential test cannot run: {reason}")
    pytest.skip(reason)


def require(path: Path) -> Path:
    if not shutil.which("bash") or not shutil.which("jq"):
        _unavailable("bash and jq are required for differential tests")
    if not path.is_file():
        _unavailable(f"legacy source not found: {path} (set OSAC_AUTO_QUEUE_SH / OSAC_CHECK_E2E_READINESS_SH)")
    return path
