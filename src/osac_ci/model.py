"""Domain model: what the planner reads (Snapshot) and what it answers (Verdict)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class Mode(StrEnum):
    """Which checks are being evaluated: the PR head or the merge-queue candidate."""

    PR = "pr"
    QUEUE = "queue"


class State(StrEnum):
    """Exactly one state describes a PR at any moment. Derived from the snapshot, never accumulated."""

    DRAFT = "draft"
    NEEDS_AUTHORIZATION = "needs-authorization"
    CHECKS_RUNNING = "checks-running"
    CHECKS_FAILED = "checks-failed"
    AWAITING_APPROVAL = "awaiting-approval"
    AWAITING_E2E_SIGNAL = "awaiting-e2e-signal"
    E2E_RUNNING = "e2e-running"
    E2E_FAILED = "e2e-failed"
    READY_TO_ENQUEUE = "ready-to-enqueue"
    IN_QUEUE = "in-queue"
    QUEUE_CHECKS_RUNNING = "queue-checks-running"
    QUEUE_FAILED = "queue-failed"
    QUEUE_PASSED = "queue-passed"
    PLANNER_ERROR = "planner-error"


class JobStatus(StrEnum):
    NOT_APPLICABLE = "not-applicable"
    WAITING = "waiting"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True)
class Review:
    user: str
    state: str
    user_type: str = "User"
    submitted_at: str | None = None
    id: int | None = None
    commit_id: str | None = None


@dataclass(frozen=True)
class LabelEvent:
    """One entry of the issue events API. Only `labeled` events are consumed today."""

    event: str
    label: str
    actor: str
    at: str = ""  # when it happened (ISO 8601, UTC): lets a replay rebuild the labels as of a moment


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str  # queued | in_progress | completed
    conclusion: str | None = None
    started_at: str | None = None
    external_id: str = ""  # set by whoever posted the check run; used to read back an authorization
    app: str = ""  # slug of the app that posted it, for example github-actions
    completed_at: str | None = None  # lets a replay tell that a check had not finished yet at a given moment


@dataclass(frozen=True)
class Snapshot:
    """Everything the planner may look at. Built by an adapter; the planner never does I/O."""

    repo: str
    number: int
    head_sha: str
    is_draft: bool = False
    is_fork: bool = False
    author: str = ""
    author_is_org_member: bool = False
    fork_owner: str = ""
    fork_owner_is_org_member: bool = False
    labels: frozenset[str] = frozenset()
    reviews: tuple[Review, ...] = ()
    label_events: tuple[LabelEvent, ...] = ()
    check_runs: tuple[CheckRun, ...] = ()
    changed_files: tuple[str, ...] = ()
    # False when the list could not be read completely (a merge-queue commit with more files than the compare API
    # returns). Path rules then cannot narrow anything: every job counts, never skipped on a guess.
    changed_files_known: bool = True
    in_merge_queue: bool = False
    # Replaying the issue events in order up to the merge: was the PR still in the queue when it merged? True means
    # the queue merged it; False means a direct merge (a bypass), even if it was queued earlier.
    queued_per_events: bool = False
    # When the queue entry that led to the merge was created (empty when the PR was not queue-merged). Together with the
    # timestamps on reviews, label events and check runs it lets a replay judge the PR as it stood at that moment.
    enqueued_at: str = ""
    # Native-approval inputs (see rules/approval.py). All empty unless the policy has an `approval:` section.
    base_ref: str = ""
    codeowners: str | None = None  # CODEOWNERS text from the base branch; None when the repository has none
    team_members: Mapping[str, frozenset[str] | None] = field(
        default_factory=dict
    )  # "org/team" -> logins; None = unreadable
    change_fingerprints: Mapping[str, str] = field(default_factory=dict)  # commit sha -> fingerprint of its changes
    # Login of an org member who authorized exactly this head commit for secrets and E2E (sha-bound trust mode only).
    authorized_by: str = ""
    # Every verified authorizer of this head, newest first (``authorized_by`` is the first). Kept so that a replay can
    # tell who had authorized at an earlier moment, when the newest one had not yet.
    authorizers: tuple[str, ...] = ()


@dataclass(frozen=True)
class JobEntry:
    job_id: str
    check: str
    status: JobStatus
    detail: str
    code: str = ""  # machine-readable reason for a waiting E2E job (see rules/readiness.py), "" otherwise
    note: str = ""  # where the path filters and what this check actually did disagree (shadow mode), "" otherwise


@dataclass(frozen=True)
class Verdict:
    state: State
    headline: str
    next_action: str
    who_must_act: str
    mode: Mode
    blockers: tuple[str, ...] = ()
    jobs: tuple[JobEntry, ...] = ()
    notes: tuple[str, ...] = ()  # things worth knowing that do not block, for example an approval carried over
    # Would today's enqueue rule (auto-queue.sh) have let this PR in? Not a draft, and the labels or approval are in
    # order. It ignores check results, which that rule does not read. Always true in queue mode.
    label_gate_ok: bool = True
