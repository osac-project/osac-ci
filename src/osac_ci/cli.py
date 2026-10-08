"""Command line: validate a policy and explain PRs.

    osac-ci policy check policy/osac.yml
    osac-ci explain --policy policy/osac.yml --snapshot snapshot.json [--mode pr|queue]
    GH_TOKEN=... osac-ci explain-pr --policy policy/osac.yml --repo osac-project/osac --number 1438
    GH_TOKEN=... osac-ci replay --policy policy/osac.yml --repo osac-project/osac --days 60 --limit 100

`explain` is offline: it turns a recorded snapshot into the verdict the control plane would publish.
`explain-pr` reads one live PR (read-only) and does the same. `replay` reads recently merged PRs and reports
whether the planner agrees they were ready (the parity evidence). None of them writes anything to GitHub.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from osac_ci.github.api import GitHubClient, GitHubError, HttpClient
from osac_ci.github.snapshot import fetch_snapshot
from osac_ci.model import CheckRun, LabelEvent, Mode, Review, Snapshot
from osac_ci.planner import plan_or_error
from osac_ci.policy import PolicyError, load_policy
from osac_ci.render import render_markdown
from osac_ci.replay import load_explained, render, replay, to_json

TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN")


def snapshot_from_dict(data: dict[str, Any]) -> Snapshot:
    """Build a Snapshot from JSON. Unknown keys are rejected so a recorded fixture cannot silently drift."""
    allowed = {
        "repo", "number", "head_sha", "is_draft", "is_fork", "author", "author_is_org_member", "fork_owner",
        "fork_owner_is_org_member", "labels", "reviews", "label_events", "check_runs", "changed_files",
        "in_merge_queue",
    }  # fmt: skip
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown snapshot keys: {sorted(unknown)}")
    kwargs: dict[str, Any] = {
        k: v for k, v in data.items() if k not in {"labels", "reviews", "label_events", "check_runs", "changed_files"}
    }
    return Snapshot(
        **kwargs,
        labels=frozenset(data.get("labels", ())),
        reviews=tuple(Review(**r) for r in data.get("reviews", ())),
        label_events=tuple(LabelEvent(**e) for e in data.get("label_events", ())),
        check_runs=tuple(CheckRun(**c) for c in data.get("check_runs", ())),
        changed_files=tuple(data.get("changed_files", ())),
    )


def build_client() -> GitHubClient:
    """The real client, with the token taken from the environment (never from an argument or a file)."""
    for name in TOKEN_ENV:
        if token := os.environ.get(name):
            return HttpClient(token)
    raise SystemExit(f"error: set one of {' or '.join(TOKEN_ENV)} (for example GH_TOKEN=$(gh auth token))")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="osac-ci", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("policy", help="policy tools").add_subparsers(dest="policy_command", required=True)
    check.add_parser("check", help="validate a policy file").add_argument("policy", type=Path)

    explain = sub.add_parser("explain", help="print the verdict for a recorded snapshot")
    explain.add_argument("--policy", type=Path, required=True)
    explain.add_argument("--snapshot", type=Path, required=True)
    explain.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.PR.value)

    live = sub.add_parser("explain-pr", help="read one live PR (read-only) and print its verdict")
    live.add_argument("--policy", type=Path, required=True)
    live.add_argument("--repo", required=True, help="owner/name")
    live.add_argument("--number", type=int, required=True)
    live.add_argument("--org", help="org used for membership lookups (default: the repo owner)")
    live.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.PR.value)
    live.add_argument(
        "--no-membership-lookup",
        action="store_true",
        help="approximate org membership from author_association (needs no org-scoped token)",
    )

    rep = sub.add_parser("replay", help="check the planner against recently merged PRs (parity evidence)")
    rep.add_argument("--policy", type=Path, required=True)
    rep.add_argument("--repo", required=True, help="owner/name")
    rep.add_argument("--days", type=int, default=60)
    rep.add_argument("--limit", type=int, default=100)
    rep.add_argument("--org", help="org used for membership lookups (default: the repo owner)")
    rep.add_argument("--explained", type=Path, help="YAML ledger of signed-off disagreements (PR number: reason)")
    rep.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    rep.add_argument("--no-membership-lookup", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        policy = load_policy(args.policy)
    except PolicyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "policy":
        print(f"OK: {policy.repo}: {len(policy.jobs)} jobs")
        return 0

    if args.command == "replay":
        try:
            report = replay(
                build_client(),
                policy,
                args.repo,
                now=datetime.now(UTC),
                days=args.days,
                limit=args.limit,
                org=args.org,
                explained=load_explained(args.explained),
                lookup_membership=not args.no_membership_lookup,
            )
        except (GitHubError, ValueError, OSError) as exc:
            print(f"error: replay failed: {exc}", file=sys.stderr)
            return 3
        print(to_json(report) if args.json else render(report), end="")
        return 1 if report.unexplained else 0

    if args.command == "explain":
        try:
            snapshot = snapshot_from_dict(json.loads(args.snapshot.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            print(f"error: cannot load snapshot: {exc}", file=sys.stderr)
            return 2
    else:
        try:
            snapshot = fetch_snapshot(
                build_client(),
                args.repo,
                args.number,
                org=args.org or args.repo.split("/", 1)[0],
                lookup_membership=not args.no_membership_lookup,
            )
        except (GitHubError, ValueError) as exc:
            print(f"error: cannot read the PR: {exc}", file=sys.stderr)
            return 3

    print(render_markdown(plan_or_error(snapshot, policy, Mode(args.mode))), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
