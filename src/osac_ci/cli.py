"""Command line: validate a policy and explain a PR from a snapshot file.

    osac-ci policy check policy/osac.yml
    osac-ci explain --policy policy/osac.yml --snapshot snapshot.json [--mode pr|queue]

`explain` is read-only and offline. It is the slice 0 "explainer": it turns a recorded PR snapshot into the
verdict the control plane would publish, which is also how the replay harness will use the planner.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from osac_ci.model import CheckRun, LabelEvent, Mode, Review, Snapshot
from osac_ci.planner import plan_or_error
from osac_ci.policy import PolicyError, load_policy
from osac_ci.render import render_markdown


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="osac-ci", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("policy", help="policy tools").add_subparsers(dest="policy_command", required=True)
    check_cmd = check.add_parser("check", help="validate a policy file")
    check_cmd.add_argument("policy", type=Path)

    explain = sub.add_parser("explain", help="print the verdict for a recorded snapshot")
    explain.add_argument("--policy", type=Path, required=True)
    explain.add_argument("--snapshot", type=Path, required=True)
    explain.add_argument("--mode", choices=[m.value for m in Mode], default=Mode.PR.value)

    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
    except PolicyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.command == "policy":
        print(f"OK: {policy.repo}: {len(policy.jobs)} jobs")
        return 0

    try:
        snapshot = snapshot_from_dict(json.loads(args.snapshot.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        print(f"error: cannot load snapshot: {exc}", file=sys.stderr)
        return 2
    print(render_markdown(plan_or_error(snapshot, policy, Mode(args.mode))), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
