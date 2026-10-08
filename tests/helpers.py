"""Test helpers: build snapshots and green check sets without repeating boilerplate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from osac_ci.model import CheckRun, Snapshot
from osac_ci.policy import Policy

ROOT = Path(__file__).resolve().parents[1]
OK_LABELS = frozenset({"lgtm", "approved", "jira/valid-reference"})
HEAD = "a" * 40
OTHER = "b" * 40


def green(policy: Policy) -> tuple[CheckRun, ...]:
    return tuple(CheckRun(job.check, "completed", "success") for job in policy.jobs.values())


def snap(policy: Policy, **overrides: Any) -> Snapshot:
    base: dict[str, Any] = {
        "repo": policy.repo,
        "number": 1,
        "head_sha": HEAD,
        "labels": OK_LABELS,
        "check_runs": green(policy),
        "changed_files": ("a.go",),
    }
    base.update(overrides)
    return Snapshot(**base)


def with_check(policy: Policy, check: str, run: CheckRun | None) -> tuple[CheckRun, ...]:
    """Green checks with one context replaced (or removed when run is None)."""
    runs = [r for r in green(policy) if r.name != check]
    if run is not None:
        runs.append(run)
    return tuple(runs)
