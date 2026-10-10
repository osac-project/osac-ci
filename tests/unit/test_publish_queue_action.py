"""The publish-queue action: its shape, and its shell run for real with a stand-in `uv`."""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from helpers import ROOT

pytestmark = pytest.mark.unit
ACTION = ROOT / ".github" / "actions" / "publish-queue" / "action.yml"
SHA = "a" * 40
BRANCH = f"gh-readonly-queue/main/pr-12-{SHA}"


def action() -> dict:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def script() -> str:
    return next(s["run"] for s in action()["runs"]["steps"] if s.get("name") == "Post the verdict on the queue commit")


def test_every_input_used_is_declared_and_every_input_declared_is_used() -> None:
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", ACTION.read_text(encoding="utf-8")))
    assert used == set(action()["inputs"])


def test_nothing_is_expanded_into_a_script_and_nothing_is_checked_out() -> None:
    for step in action()["runs"]["steps"]:
        assert "${{" not in step.get("run", ""), step.get("name")
        assert "checkout" not in step.get("uses", "")


def test_the_default_policy_exists_in_this_repository() -> None:
    assert (ROOT / action()["inputs"]["policy"]["default"]).is_file()


def run(tmp_path: Path, *, branch: str = BRANCH, note: str = "", uv_exit: int = 0):  # type: ignore[no-untyped-def]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    uv = bin_dir / "uv"
    uv.write_text(f"#!/bin/bash\necho \"$@\" >> '{tmp_path}/uv-args'\nexit {uv_exit}\n")
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_TOKEN": "t", "REPO": "o/r", "SHA": SHA, "BRANCH": branch,
        "POLICY": "policy/osac.yml", "NOTE": note, "ACTION_PATH": "/a/b/c",
    }  # fmt: skip
    done = subprocess.run(["bash", "-c", script()], env=env, capture_output=True, text=True, check=False)
    args = (tmp_path / "uv-args").read_text() if (tmp_path / "uv-args").exists() else ""
    return done.returncode, args, done.stdout + done.stderr


def test_the_program_gets_the_commit_and_the_branch_as_arguments(tmp_path: Path) -> None:
    code, args, _ = run(tmp_path)
    assert code == 0
    assert f"publish-queue --policy /a/b/c/../../../policy/osac.yml --repo o/r --sha {SHA} --branch {BRANCH}" in args
    assert "--note" not in args


def test_a_note_is_passed_as_one_argument(tmp_path: Path) -> None:
    _, args, _ = run(tmp_path, note="Informational; $(id) `x`")
    assert "--note Informational; $(id) `x`" in args


@pytest.mark.parametrize("branch", ["main", "feature/x", f"refs/heads/gh-readonly-queue/main/pr-1-{SHA}", ""])
def test_anything_but_a_queue_branch_is_refused_before_the_program_runs(tmp_path: Path, branch: str) -> None:
    code, args, log = run(tmp_path, branch=branch)
    assert code == 1 and args == "" and "not a merge-queue branch" in log


def test_a_failing_program_fails_the_step(tmp_path: Path) -> None:
    code, _, _ = run(tmp_path, uv_exit=3)
    assert code != 0
