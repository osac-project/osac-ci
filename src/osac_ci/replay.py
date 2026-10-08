"""Replay: does the planner agree with what actually happened to recently merged PRs?

This is the evidence for the parity gate (design decision D7). For every PR merged in the window the planner reads
the PR's final state and must say it was ready to merge. Any other verdict is a DISAGREEMENT. A disagreement is
either explained (listed with a reason in an explained-ledger that the infra group signs off) or unexplained;
only unexplained ones fail the gate.

Limitation, stated on purpose: this reads the PR's FINAL state (labels and checks as they are now), not the state
at the moment it was enqueued. It can prove the planner never calls a legitimately merged PR blocked on today's
data; a decision-time replay needs the event timeline and is a later step.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from osac_ci.github.api import GitHubClient, check_repo, paginate
from osac_ci.github.snapshot import fetch_snapshot
from osac_ci.model import Mode, State
from osac_ci.planner import plan_or_error
from osac_ci.policy import Policy

EXPECTED = State.READY_TO_ENQUEUE


@dataclass(frozen=True)
class Row:
    number: int
    merged_at: str
    state: State
    headline: str
    agrees: bool
    explanation: str | None = None

    @property
    def unexplained(self) -> bool:
        return not self.agrees and self.explanation is None


@dataclass(frozen=True)
class Report:
    repo: str
    days: int
    rows: tuple[Row, ...]

    @property
    def disagreements(self) -> tuple[Row, ...]:
        return tuple(r for r in self.rows if not r.agrees)

    @property
    def unexplained(self) -> tuple[Row, ...]:
        return tuple(r for r in self.rows if r.unexplained)

    @property
    def agreement(self) -> float:
        return 1.0 if not self.rows else sum(r.agrees for r in self.rows) / len(self.rows)


def load_explained(path: Path | None) -> dict[int, str]:
    """`{pr_number: reason}`. Every entry is a signed-off decision; keep the file small and reviewed."""
    if path is None:
        return {}
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or not all(isinstance(v, str) and v.strip() for v in raw.values()):
        raise ValueError(f"{path}: expected a mapping of PR number to a non-empty reason")
    return {int(k): v for k, v in raw.items()}


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def merged_prs(client: GitHubClient, repo: str, *, now: datetime, days: int, limit: int) -> list[dict[str, Any]]:
    """Merged PRs from the last ``days`` days, newest first. Listing is by updated time, descending, so the scan can
    stop at the first PR last touched before the cutoff (updated_at is never earlier than merged_at)."""
    cutoff = now - timedelta(days=days)
    found: list[dict[str, Any]] = []
    for pr in paginate(
        client,
        f"/repos/{check_repo(repo)}/pulls",
        params={"state": "closed", "sort": "updated", "direction": "desc"},
    ):
        if _parse(pr["updated_at"]) < cutoff:
            break
        if pr.get("merged_at") and _parse(pr["merged_at"]) >= cutoff:
            found.append(pr)
            if len(found) >= limit:
                break
    return found


def replay(
    client: GitHubClient,
    policy: Policy,
    repo: str,
    *,
    now: datetime,
    days: int = 60,
    limit: int = 100,
    org: str | None = None,
    explained: Mapping[int, str] | None = None,
    lookup_membership: bool = True,
) -> Report:
    explained = explained or {}
    org = org or repo.split("/", 1)[0]
    rows = []
    for pr in merged_prs(client, repo, now=now, days=days, limit=limit):
        snapshot = fetch_snapshot(client, repo, pr["number"], org=org, lookup_membership=lookup_membership)
        verdict = plan_or_error(snapshot, policy, Mode.PR)
        agrees = verdict.state is EXPECTED
        rows.append(
            Row(
                number=pr["number"],
                merged_at=pr["merged_at"],
                state=verdict.state,
                headline=verdict.headline,
                agrees=agrees,
                explanation=None if agrees else explained.get(pr["number"]),
            )
        )
    return Report(repo=repo, days=days, rows=tuple(rows))


def render(report: Report) -> str:
    n, bad = len(report.rows), report.disagreements
    lines = [
        f"# Replay: {report.repo}, merged in the last {report.days} days",
        "",
        f"- merged PRs replayed: {n}",
        f"- planner agrees (ready to merge): {n - len(bad)} ({report.agreement:.1%})",
        f"- disagreements: {len(bad)} (explained: {len(bad) - len(report.unexplained)}, "
        f"UNEXPLAINED: {len(report.unexplained)})",
        "",
        "Reads each PR's final state, not its state when it was enqueued.",
    ]
    by_state = Counter(r.state.value for r in report.rows)
    lines += ["", "| Verdict | PRs |", "|---|---|", *[f"| {s} | {c} |" for s, c in by_state.most_common()]]
    if bad:
        lines += ["", "## Disagreements", "", "| PR | Verdict | Why | Explained |", "|---|---|---|---|"]
        lines += [
            f"| #{r.number} | {r.state.value} | {r.headline} | {r.explanation or '**no**'} |"
            for r in sorted(bad, key=lambda r: r.number)
        ]
    return "\n".join(lines) + "\n"


def to_json(report: Report) -> str:
    return json.dumps(
        {
            "repo": report.repo,
            "days": report.days,
            "replayed": len(report.rows),
            "agreement": report.agreement,
            "unexplained": [r.number for r in report.unexplained],
            "rows": [
                {
                    "number": r.number,
                    "merged_at": r.merged_at,
                    "state": r.state.value,
                    "headline": r.headline,
                    "agrees": r.agrees,
                    "explanation": r.explanation,
                }
                for r in report.rows
            ],
        },
        indent=2,
    )
