"""Policy: the reviewable YAML that says which jobs exist, when they block, and which labels gate a merge.

Editing a policy changes merge behavior, so `policy/` and this schema are owned by the infra group (CODEOWNERS).
The model rejects unknown keys, so a typo is an error, not a silently ignored rule.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

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
    filters: tuple[str, ...] = Field(
        default=(), description="Named path filters that must ALL match some changed file (see path_filters)"
    )
    filters_any: tuple[str, ...] = Field(
        default=(), description="Named path filters of which at least ONE must match some changed file"
    )
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


class ProtectedPaths(_Strict):
    """Files whose change needs approval from named people, whatever the rest of the approval rules say.

    A pull request may change the very workflows and scripts that produce the checks it is judged by, so a check that
    reports success proves little for such a change. Changing a file that matches ``paths`` therefore needs an approval
    of the current changes from one of ``approvers`` (see rules/protected.py). The policy is not part of the pull
    request, so the PR cannot change who has to approve."""

    paths: tuple[str, ...] = Field(min_length=1, description="Globs of the protected files")
    approvers: tuple[str, ...] = Field(min_length=1, description="'@login' or '@org/team'; one of them must approve")
    carry_over: Literal["never", "trivial-rebase"] = Field(
        default="never", description="Keep an approval after a rebase that leaves the PR's own changes unchanged"
    )

    @field_validator("paths")
    @classmethod
    def _positive_globs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not p or p.startswith("!") for p in value):
            raise ValueError("paths must be non-empty globs without a '!' prefix")
        return value

    @field_validator("approvers")
    @classmethod
    def _handles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"@[A-Za-z0-9][A-Za-z0-9-]*(/[A-Za-z0-9._-]+)?", a) for a in value):
            raise ValueError("approvers must look like @login or @org/team")
        return value


Signal = Literal["human-approval", "coderabbit-approval", "lgtm-label", "e2e-ready-label"]


class E2EUnlock(_Strict):
    """What lets an expensive E2E job start (see rules/e2e_unlock.py).

    ``legacy`` reproduces today's rules exactly (the label and review rules ported from osac-test-infra). ``policy``
    unlocks when any one of the listed signals is present, per suite if configured. Unlike the legacy rules there is
    no sticky ``lgtm``: a signal must hold for the commit that is about to be tested."""

    mode: Literal["legacy", "policy"] = "legacy"
    any_of: tuple[Signal, ...] = Field(default=("human-approval", "coderabbit-approval"), min_length=1)
    per_suite: dict[str, tuple[Signal, ...]] = Field(default_factory=dict, description="Signals for one suite")
    block_on_changes_requested: bool = Field(
        default=True, description="An outstanding human 'changes requested' blocks every signal"
    )

    @field_validator("any_of")
    @classmethod
    def _unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("signals must not repeat")
        return value

    @field_validator("per_suite")
    @classmethod
    def _non_empty_unique(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        for suite, signals in value.items():
            if not signals or len(set(signals)) != len(signals):
                raise ValueError(f"suite {suite!r}: list at least one signal, without repeats")
        return value

    def signals_for(self, suite: str | None) -> tuple[str, ...]:
        return self.per_suite.get(suite or "", self.any_of)


class E2E(_Strict):
    unlock: E2EUnlock = E2EUnlock()


class Trust(_Strict):
    """Who may use secrets and start expensive jobs from a pull request (see rules/fork.py)."""

    authorization: Literal["label", "sha-bound"] = Field(
        default="label",
        description="label: the ok-to-test label (today). sha-bound: an org member authorizes one exact commit.",
    )
    trusted_bots: tuple[str, ...] = Field(default=(), description="Bot logins whose fork PRs need no authorization")
    check_name: str = Field(default="OSAC CI authorization", min_length=1)


class PathFilters(_Strict):
    """Named path filters in the format of osac's ``.github/filters/ci-filters.yml``.

    Each filter is a list of globs evaluated the way ``dorny/paths-filter`` does with ``predicate-quantifier: every``
    (see osac_ci/paths.py). A job names the filters its workflow gates on, and runs when every name in ``filters``
    and at least one name in ``filters_any`` holds, which is how the workflows combine them today.

    ``shadow`` (default) keeps trusting each check's own outcome and only reports where the filters and the outcome
    disagree: evidence for the mapping, with no effect on a verdict. ``enforce`` makes the filters decide: a job whose
    filters do not hold is not applicable, so nothing waits for it."""

    mode: Literal["shadow", "enforce"] = "shadow"
    skipped_applicable: Literal["pass", "fail"] = Field(
        default="pass",
        description=(
            "enforce only. What a skipped check means for a job whose filters say it applies: GitHub counts it as "
            "passed ('pass'); 'fail' reads it as a job that did not do its work, for example a workflow changed to "
            "skip itself"
        ),
    )
    file: str | None = Field(default=None, description="A ci-filters.yml-style file, relative to the policy file")
    filters: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class Policy(_Strict):
    version: Literal[1]
    repo: str = Field(min_length=3)
    merge: Merge = Merge()
    approval: Approval | None = None
    trust: Trust = Trust()
    e2e: E2E = E2E()
    path_filters: PathFilters = PathFilters()
    protected_paths: tuple[ProtectedPaths, ...] = ()
    jobs: dict[str, Job]

    @model_validator(mode="after")
    def _e2e_unlock_is_consistent(self) -> Policy:
        unlock = self.e2e.unlock
        if unlock.mode != "policy":
            return self
        suites = {job.suite for job in self.jobs.values() if job.suite}
        unknown = sorted(set(unlock.per_suite) - suites)
        if unknown:
            raise ValueError(f"e2e.unlock.per_suite names suites no job has: {unknown}; jobs have {sorted(suites)}")
        used = set(unlock.any_of).union(*unlock.per_suite.values())
        if "human-approval" in used and self.approval is None:
            raise ValueError("e2e.unlock uses human-approval, which needs an approval: section")
        return self

    @model_validator(mode="after")
    def _skipped_rule_needs_enforce(self) -> Policy:
        if self.path_filters.skipped_applicable == "fail" and self.path_filters.mode != "enforce":
            raise ValueError("path_filters.skipped_applicable: fail needs path_filters.mode: enforce")
        return self

    @model_validator(mode="after")
    def _jobs_use_known_filters(self) -> Policy:
        known = set(self.path_filters.filters)
        for job_id, job in self.jobs.items():
            named = (*job.filters, *job.filters_any)
            unknown = sorted(set(named) - known)
            if unknown:
                raise ValueError(f"job {job_id!r} names path filters that are not defined: {unknown}")
            if named and (job.paths or job.exclude_paths):
                raise ValueError(f"job {job_id!r} uses both globs (paths) and named filters; use one")
        return self

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


def _inside(base: Path, name: str) -> Path:
    """``name`` resolved against ``base``, which it may not leave: no absolute path, no ``..`` out of the directory,
    no symlink pointing out of it (the path is resolved before the check)."""
    if Path(name).is_absolute():
        raise PolicyError(f"path_filters.file must be relative to the policy file, got {name!r}")
    resolved = (base / name).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError:
        raise PolicyError(f"path_filters.file {name!r} leaves the policy directory") from None
    return resolved


def _load_filters_file(path: Path) -> dict[str, tuple[str, ...]]:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"cannot read path filters file {path}: {exc}") from exc
    ok = isinstance(raw, dict) and all(
        isinstance(k, str) and isinstance(v, list) and v and all(isinstance(p, str) for p in v) for k, v in raw.items()
    )
    if not ok:
        raise PolicyError(f"{path}: expected a mapping of filter name to a non-empty list of patterns")
    return {k: tuple(v) for k, v in raw.items()}


def parse_policy(text: str, *, base: Path | None = None) -> Policy:
    """Parse and validate a policy. ``base`` is the directory of the policy file, for ``path_filters.file``."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyError("policy must be a YAML mapping")
    section = raw.get("path_filters")
    if isinstance(section, dict) and section.get("file"):
        if base is None:
            raise PolicyError("path_filters.file needs the policy to be loaded from a file (it is relative to it)")
        loaded = _load_filters_file(_inside(base, str(section["file"])))
        inline = section.get("filters") or {}
        clash = sorted(set(loaded) & set(inline))
        if clash:
            raise PolicyError(f"path filters defined both inline and in {section['file']}: {clash}")
        raw["path_filters"] = {**section, "filters": {**inline, **loaded}}
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
    return parse_policy(text, base=path.parent)
