"""The step of the bot sweep that makes a Konflux pull request ready (title, Jira label, held runs), run with a stand-in
`gh` and the real `jq`: what it touches, and what it leaves alone."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from helpers import ROOT

pytestmark = pytest.mark.unit
SWEEP = ROOT / ".github" / "workflows" / "bot-sweep.yml"
KONFLUX = "red-hat-konflux-kflux-prd-rh02[bot]"

# A stand-in for `gh`: logs every call; `api repos/o/r/pulls/7` answers with PULL_JSON; the held runs listing is empty.
GH = r"""#!/bin/bash
echo "$*" >> "$GH_LOG"
case "$*" in
  "api repos/o/r/pulls/7") printf '%s' "$PULL_JSON" ;;
  *"--paginate"*) ;;
esac
exit 0
"""


def script() -> str:
    steps = yaml.safe_load(SWEEP.read_text(encoding="utf-8"))["jobs"]["approve"]["steps"]
    return next(s["run"] for s in steps if s.get("name") == "Make a Konflux pull request ready")


def pull(**changes: object) -> dict:
    base = {
        "user": {"login": KONFLUX},
        "state": "open",
        "draft": False,
        "title": "Update dependency x",
        "labels": [],
        "head": {"sha": "a" * 40, "repo": {"full_name": "o/r"}},
        "base": {"repo": {"full_name": "o/r"}},
    }
    return {**base, **changes}


def run(tmp_path: Path, given: dict) -> list[str]:
    """Run the step; returns the calls made to gh."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls"
    log.unlink(missing_ok=True)  # one run per call of this function
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_LOG": str(log), "PULL_JSON": json.dumps(given),
        "REPO": "o/r", "NUMBER": "7", "KONFLUX": KONFLUX, "DECISION_FILE": "", "GH_TOKEN": "t",
    }  # fmt: skip
    done = subprocess.run(["bash", "-c", script()], env=env, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return log.read_text().splitlines() if log.exists() else []


def edits(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("pr edit")]


def test_an_open_konflux_pull_request_without_a_prefix_or_label_gets_both(tmp_path: Path) -> None:
    calls = edits(run(tmp_path, pull()))
    assert calls == [
        "pr edit 7 --repo o/r --title NO-ISSUE: Update dependency x",
        "pr edit 7 --repo o/r --add-label jira/valid-reference",
    ]


def test_one_that_has_both_is_left_alone(tmp_path: Path) -> None:
    ready = pull(title="NO-ISSUE: x", labels=[{"name": "jira/valid-reference"}])
    assert edits(run(tmp_path, ready)) == []


@pytest.mark.parametrize(
    "changes",
    [{"state": "closed"}, {"draft": True}, {"state": "closed", "draft": True}, {"state": "merged"}],
)
def test_a_closed_or_draft_pull_request_gets_no_title_label_or_approval(tmp_path: Path, changes: dict) -> None:
    calls = run(tmp_path, pull(**changes))
    assert calls == ["api repos/o/r/pulls/7"], "it was read, and nothing more"


def test_a_pull_request_of_another_author_or_a_fork_is_still_left_alone(tmp_path: Path) -> None:
    assert run(tmp_path, pull(user={"login": "someone"})) == ["api repos/o/r/pulls/7"]
    fork = pull(head={"sha": "a" * 40, "repo": {"full_name": "x/r"}})
    assert run(tmp_path, fork) == ["api repos/o/r/pulls/7"]
