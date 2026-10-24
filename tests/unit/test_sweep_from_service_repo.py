"""The sweeps and the cleaner that run from osac-ci for other repositories: their shape, and the shell that names the
repository to sweep, run with a stand-in environment."""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from helpers import ROOT

pytestmark = pytest.mark.unit
WF = ROOT / ".github" / "workflows"
SWEEP = WF / "bot-sweep.yml"
BOTS = WF / "sweep-bots.yml"
CLEAN = WF / "clean-service-runs.yml"
TOKEN_ACTION = "actions/create-github-app-token@"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def target_script() -> str:
    steps = load(SWEEP)["jobs"]["list"]["steps"]
    return next(s["run"] for s in steps if s.get("id") == "target")


def run_target(tmp_path: Path, repo_input: str, caller: str = "o/caller") -> tuple[int, dict[str, str]]:
    out = tmp_path / "out"
    out.write_text("")
    env = {"PATH": os.environ["PATH"], "REPO_INPUT": repo_input, "CALLER": caller, "GITHUB_OUTPUT": str(out)}
    done = subprocess.run(["bash", "-c", target_script()], env=env, capture_output=True, text=True, check=False)
    pairs = [line.split("=", 1) for line in out.read_text().splitlines() if "=" in line]
    return done.returncode, {k: v for k, v in pairs}


def test_with_no_repository_the_sweep_is_of_the_caller_as_it_always_was(tmp_path: Path) -> None:
    code, got = run_target(tmp_path, "")
    assert code == 0 and got == {"repo": "o/caller", "owner": "o", "name": "caller", "same": "true"}


def test_a_given_repository_is_the_one_swept_and_is_not_the_callers(tmp_path: Path) -> None:
    code, got = run_target(tmp_path, "osac-project/osac")
    assert code == 0 and got == {"repo": "osac-project/osac", "owner": "osac-project", "name": "osac", "same": "false"}
    assert run_target(tmp_path, "o/caller")[1]["same"] == "true", (
        "naming the caller itself is the same as naming nothing"
    )


@pytest.mark.parametrize(
    "bad", ["osac", "a/b/c", "a b/c", "a/b c", "/x", "x/", "a/b;id", "$(id)/x", "a/b\nc/d", "-a/b"]
)
def test_anything_that_is_not_owner_slash_name_is_refused_before_a_token_is_made(tmp_path: Path, bad: str) -> None:
    code, got = run_target(tmp_path, bad)
    assert code != 0 and got == {}


def test_every_token_is_made_for_the_swept_repository_only() -> None:
    steps = [
        s
        for job in load(SWEEP)["jobs"].values()
        for s in job["steps"]
        if str(s.get("uses", "")).startswith(TOKEN_ACTION)
    ]
    assert len(steps) == 4  # reads (two jobs), preparation, approval
    for step in steps:
        assert step["with"]["owner"] == "${{ steps.target.outputs.owner }}"
        assert step["with"]["repositories"] == "${{ steps.target.outputs.name }}"


def test_the_reads_use_the_apps_read_token_only_when_swept_from_another_repository() -> None:
    jobs = load(SWEEP)["jobs"]
    for name in ("list", "approve"):
        read = next(s for s in jobs[name]["steps"] if s.get("id") == "read-token")
        assert read["if"] == "steps.target.outputs.same != 'true'"
        assert (
            read["with"]["permission-pull-requests"] == "read"
            and len([k for k in read["with"] if k.startswith("permission-")]) == 1
        )
    text = SWEEP.read_text(encoding="utf-8")
    assert text.count("${{ steps.read-token.outputs.token || github.token }}") == 3, "list, decide, approve read"


def test_the_sweep_names_no_repository_but_the_target_outside_the_step_that_makes_it() -> None:
    text = SWEEP.read_text(encoding="utf-8")
    assert len(re.findall(r"github\.repository", text)) == 2, "only the two target steps read the caller"
    assert "inputs.repo" not in re.sub(r"REPO_INPUT: \$\{\{ inputs\.repo \}\}", "", text).replace("inputs.repo\n", "")


def test_the_input_is_optional_and_empty_by_default() -> None:
    given = load(SWEEP)[True]["workflow_call"]["inputs"]["repo"]
    assert given["default"] == "" and given["type"] == "string"


def test_the_service_repository_sweeps_osac_with_the_konflux_preparation_and_test_infra_without() -> None:
    jobs = load(BOTS)["jobs"]
    assert set(jobs) == {"osac", "osac-test-infra"}
    assert jobs["osac"]["with"]["repo"] == "osac-project/osac" and jobs["osac"]["with"]["konflux-prep"] is True
    assert jobs["osac-test-infra"]["with"]["repo"] == "osac-project/osac-test-infra"
    assert "konflux-prep" not in jobs["osac-test-infra"]["with"]
    for job in jobs.values():
        assert job["uses"] == "./.github/workflows/bot-sweep.yml"
        assert job["secrets"] == {"OSAC_CI_APP_PRIVATE_KEY": "${{ secrets.OSAC_CI_APP_PRIVATE_KEY }}"}, "never inherit"
        assert job["permissions"] == {"contents": "read", "pull-requests": "read"}
        assert "github.repository == 'osac-project/osac-ci'" in job["if"], "a copy of this file elsewhere does nothing"


def test_the_schedule_is_the_one_the_repositories_had_and_one_sweep_runs_at_a_time() -> None:
    wf = load(BOTS)
    assert wf[True]["schedule"] == [{"cron": "3,18,33,48 * * * *"}]
    assert wf["concurrency"] == {"group": "sweep-bots", "cancel-in-progress": False} and wf["permissions"] == {}


@pytest.mark.parametrize(
    ("repository", "number", "osac", "infra"),
    [
        ("all", "", True, True),
        ("all", "7", False, False),
        ("osac", "7", True, False),
        ("osac-test-infra", "7", False, True),
    ],
)
def test_a_manual_run_with_a_number_needs_one_repository(repository: str, number: str, osac: bool, infra: bool) -> None:
    """The conditions, evaluated for a manual run (a schedule has neither input and runs both)."""
    jobs = load(BOTS)["jobs"]

    def holds(condition: str) -> bool:
        text = " ".join(condition.split())
        text = text.replace("github.repository == 'osac-project/osac-ci'", "True").replace(
            "github.event_name == 'schedule'", "False"
        )
        text = re.sub(r"inputs\.repository == '([a-z-]+)'", lambda m: str(repository == m.group(1)), text)
        text = text.replace("inputs.number == ''", str(number == "")).replace("&&", " and ").replace("||", " or ")
        return truth(ast.parse(text, mode="eval").body)

    assert (holds(jobs["osac"]["if"]), holds(jobs["osac-test-infra"]["if"])) == (osac, infra)


def truth(node: ast.expr) -> bool:
    """The value of a condition made of True, False, ``and``, ``or`` and parentheses: no code is run."""
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BoolOp):
        values = [truth(v) for v in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    raise AssertionError(f"unexpected {ast.dump(node)}")


def test_the_cleaner_makes_a_token_for_one_repository_with_the_one_permission_it_needs() -> None:
    job = load(CLEAN)["jobs"]["clean"]
    token = next(s for s in job["steps"] if str(s.get("uses", "")).startswith(TOKEN_ACTION))["with"]
    assert token["repositories"] == "${{ matrix.repo }}" and token["owner"] == "osac-project"
    assert [k for k in token if k.startswith("permission-")] == ["permission-actions"]
    assert token["permission-actions"] == "write"
    assert job["permissions"] == {"contents": "read"} and load(CLEAN)["permissions"] == {}
    assert "github.repository == 'osac-project/osac-ci'" in job["if"]


def test_the_cleaner_runs_the_command_with_the_service_workflows_and_nothing_from_the_repository_is_run() -> None:
    steps = load(CLEAN)["jobs"]["clean"]["steps"]
    script = next(s["run"] for s in steps if s.get("name") == "Delete the skipped runs")
    assert "osac-ci clean-runs" in script
    for workflow in ("e2e-pr.yml", "e2e-comment.yml", "e2e-review-relay.yml"):
        assert f"--workflow {workflow}" in script
    assert "${{" not in script, "nothing is expanded into the script"
    assert not [s for s in steps if "repository" in str(s.get("with", {})) and "checkout" in str(s.get("uses", ""))]
