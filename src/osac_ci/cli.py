"""Command line: validate a policy and explain PRs.

    osac-ci policy check policy/osac.yml
    osac-ci explain --policy policy/osac.yml --snapshot snapshot.json [--mode pr|queue]
    GH_TOKEN=... osac-ci explain-pr --policy policy/osac.yml --repo osac-project/osac --number 1438
    GH_TOKEN=... osac-ci replay --policy policy/osac.yml --repo osac-project/osac --days 60 --limit 100
    GH_TOKEN=... osac-ci report --policy policy/osac.yml --repo osac-project/osac --out-dir report

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
from osac_ci.publish import CHECK_NAME, describe, publish_pr, sweep
from osac_ci.render import render_markdown
from osac_ci.replay import load_explained, render, replay, to_json
from osac_ci.report import build_report, write_all

TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN")
ORG_TOKEN_ENV = "OSAC_CI_ORG_TOKEN"  # optional: a token that can only read the organization (membership, teams)


def snapshot_from_dict(data: dict[str, Any]) -> Snapshot:
    """Build a Snapshot from JSON. Unknown keys are rejected so a recorded fixture cannot silently drift."""
    allowed = {
        "repo", "number", "head_sha", "is_draft", "is_fork", "author", "author_is_org_member", "fork_owner",
        "fork_owner_is_org_member", "labels", "reviews", "label_events", "check_runs", "changed_files",
        "in_merge_queue", "queued_per_events", "base_ref", "codeowners", "team_members", "change_fingerprints",
    }  # fmt: skip
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown snapshot keys: {sorted(unknown)}")
    kwargs: dict[str, Any] = {
        k: v
        for k, v in data.items()
        if k not in {"labels", "reviews", "label_events", "check_runs", "changed_files", "team_members"}
    }
    return Snapshot(
        **kwargs,
        labels=frozenset(data.get("labels", ())),
        reviews=tuple(Review(**r) for r in data.get("reviews", ())),
        label_events=tuple(LabelEvent(**e) for e in data.get("label_events", ())),
        check_runs=tuple(CheckRun(**c) for c in data.get("check_runs", ())),
        changed_files=tuple(data.get("changed_files", ())),
        team_members={t: (None if m is None else frozenset(m)) for t, m in data.get("team_members", {}).items()},
    )


def build_org_client() -> GitHubClient | None:
    """A client for organization lookups only, when ``OSAC_CI_ORG_TOKEN`` is set; otherwise the main client is used."""
    token = os.environ.get(ORG_TOKEN_ENV)
    return HttpClient(token) if token else None


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

    rpt = sub.add_parser("report", help="write a table of every open PR with its state, reason and next step")
    rpt.add_argument("--policy", type=Path, required=True)
    rpt.add_argument("--repo", required=True, help="owner/name")
    rpt.add_argument("--out-dir", type=Path, default=Path("report"))
    rpt.add_argument("--formats", default="md,html,json", help="comma separated: md, html, json")
    rpt.add_argument("--include-drafts", action="store_true")
    rpt.add_argument("--limit", type=int)
    rpt.add_argument("--workers", type=int, default=6)
    rpt.add_argument("--org", help="org used for membership lookups (default: the repo owner)")
    rpt.add_argument(
        "--lookup-membership", action="store_true", help="look up org membership (needs an org-scoped token)"
    )

    pub = sub.add_parser("publish", help="post the verdict of one PR (or every open PR) as the OSAC CI check run")
    pub.add_argument("--policy", type=Path, required=True)
    pub.add_argument("--repo", required=True, help="owner/name")
    target = pub.add_mutually_exclusive_group(required=True)
    target.add_argument("--number", type=int, help="publish for this PR")
    target.add_argument("--all", action="store_true", help="publish for every open PR (the periodic sweep)")
    pub.add_argument("--check-name", default=CHECK_NAME)
    pub.add_argument("--note", default="", help="text placed above the verdict in the check summary")
    pub.add_argument("--org", help="org used for membership lookups (default: the repo owner)")
    pub.add_argument("--limit", type=int, help="with --all: stop after this many PRs")
    pub.add_argument("--dry-run", action="store_true", help="print what would be posted, post nothing")
    pub.add_argument(
        "--lookup-membership", action="store_true", help="look up org membership (needs an org-scoped token)"
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

    if args.command == "report":
        formats = [f.strip() for f in args.formats.split(",") if f.strip()]
        unknown = set(formats) - {"md", "html", "json"}
        if unknown or not formats:
            print(f"error: unknown formats {sorted(unknown)}; use md, html, json", file=sys.stderr)
            return 2
        try:
            report = build_report(
                build_client(),
                policy,
                args.repo,
                generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
                org=args.org,
                include_drafts=args.include_drafts,
                limit=args.limit,
                lookup_membership=args.lookup_membership,
                workers=args.workers,
            )
            names = write_all(report, args.out_dir, formats)
        except (GitHubError, ValueError, OSError) as exc:
            print(f"error: report failed: {exc}", file=sys.stderr)
            return 3
        summary = ", ".join(f"{n} {s.value}" for s, n in report.counts()) or "no open PRs"
        print(f"{len(report.rows)} open PRs ({summary}); wrote {', '.join(names)} to {args.out_dir}")
        return 0

    if args.command == "publish":
        common = {
            "org": args.org,
            "lookup_membership": args.lookup_membership,
            "check_name": args.check_name,
            "note": args.note,
            "dry_run": args.dry_run,
            "org_client": build_org_client(),
        }
        try:
            client = build_client()
            if args.all:
                outcomes = sweep(client, policy, args.repo, limit=args.limit, **common)
            else:
                outcomes = [publish_pr(client, policy, args.repo, args.number, **common)]
        except (GitHubError, ValueError) as exc:
            print(f"error: publish failed: {exc}", file=sys.stderr)
            return 3
        print(describe(outcomes), end="")
        return 1 if any(o.action == "failed" for o in outcomes) else 0

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
                approval=policy.approval,
                org_client=build_org_client(),
            )
        except (GitHubError, ValueError) as exc:
            print(f"error: cannot read the PR: {exc}", file=sys.stderr)
            return 3

    print(render_markdown(plan_or_error(snapshot, policy, Mode(args.mode))), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
