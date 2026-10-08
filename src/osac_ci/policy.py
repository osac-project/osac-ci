"""Policy: the reviewable YAML that says which jobs exist, when they block, and which labels gate a merge.

Editing a policy changes merge behavior, so `policy/` and this schema are owned by the infra group (CODEOWNERS).
The model rejects unknown keys, so a typo is an error, not a silently ignored rule.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from osac_ci.rules.labels import QUEUE_BLOCKING_LABELS, REQUIRED_LABELS

_JOB_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class PolicyError(ValueError):
    """The policy file is missing, unreadable or invalid. The planner turns this into `planner-error`."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Job(_Strict):
    kind: Literal["cheap", "e2e"] = "cheap"
    check: str = Field(min_length=1, description="Name of the required context this job reports")
    required_at: tuple[Literal["pr", "queue"], ...] = ("pr", "queue")
    paths: tuple[str, ...] = Field(default=(), description="Include globs; empty means always applicable")
    exclude_paths: tuple[str, ...] = ()
    needs_readiness: bool = Field(default=False, description="Expensive job gated by the E2E unlock signals")
    suite: str | None = None
    markers: str | None = None

    @field_validator("required_at")
    @classmethod
    def _non_empty_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("required_at must list at least one of: pr, queue")
        if len(set(value)) != len(value):
            raise ValueError("required_at must not repeat a value")
        return value

    @model_validator(mode="after")
    def _readiness_only_for_e2e(self) -> Job:
        if self.needs_readiness and self.kind != "e2e":
            raise ValueError("needs_readiness is only valid for kind: e2e")
        return self


class Merge(_Strict):
    required_labels: tuple[str, ...] = REQUIRED_LABELS
    blocking_labels: tuple[str, ...] = QUEUE_BLOCKING_LABELS


class Approval(_Strict):
    """Native approval from code owners (see rules/approval.py). Absent from the policy means label-based approval."""

    min_approvals: int = Field(default=1, ge=1, description="Distinct people other than the author who must approve")
    require_code_owners: bool = Field(default=True, description="Every changed file with owners needs one of them")
    carry_over: Literal["never", "trivial-rebase"] = Field(
        default="trivial-rebase",
        description="Keep an approval after a rebase when the PR's own changes are unchanged",
    )


class Policy(_Strict):
    version: Literal[1]
    repo: str = Field(min_length=3)
    merge: Merge = Merge()
    approval: Approval | None = None
    jobs: dict[str, Job]

    @field_validator("jobs")
    @classmethod
    def _valid_ids_and_unique_checks(cls, jobs: dict[str, Job]) -> dict[str, Job]:
        if not jobs:
            raise ValueError("jobs must not be empty")
        seen: dict[str, str] = {}
        for job_id, job in jobs.items():
            if not _JOB_ID.match(job_id):
                raise ValueError(f"job id {job_id!r} must match {_JOB_ID.pattern}")
            if job.check in seen:
                raise ValueError(f"jobs {seen[job.check]!r} and {job_id!r} report the same check {job.check!r}")
            seen[job.check] = job_id
        return jobs


def parse_policy(text: str) -> Policy:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError("policy must be a YAML mapping")
    try:
        return Policy.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise PolicyError(f"invalid policy: {problems}") from exc


def load_policy(path: Path) -> Policy:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PolicyError(f"cannot read policy {path}: {exc}") from exc
    return parse_policy(text)
