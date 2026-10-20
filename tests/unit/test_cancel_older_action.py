"""The cancel-older action: which runs of the E2E workflow are for an older commit of a pull request, found by the
commit their title ends with. Its shell runs with a stand-in `gh`."""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from helpers import ROOT

pytestmark = pytest.mark.unit
ACTION = ROOT / ".github" / "actions" / "cancel-older" / "action.yml"
DECIDE = ROOT / ".github" / "workflows" / "e2e-pr-decide.yml"
HEAD = "a" * 40
OLD = "b" * 40
OLDER = "c" * 40
OTHER = "d" * 40  # a commit of some other pull request

# A stand-in for `gh`: GH_COMMITS (the commits of the pull request, one per line), GH_RUNS_<status> (the JSON of
# `gh run list` for a status), GH_OTHERS_<sha> (how many other open pull requests have the commit).
GH = r"""#!/bin/bash
args="$*"
echo "${args//$'\n'/ }" >> "$GH_LOG"
case "$args" in
  "api --paginate "*"/pulls/"*"/commits "*) printf '%s\n' "$GH_COMMITS" ;;
  "run list "*) status="${args#*--status }"; status="${status%% *}"; var="GH_RUNS_${status}"; echo "${!var:-[]}" ;;
  "api repos/"*"/commits/"*"/pulls "*)
    sha="${args#*/commits/}"; sha="${sha%%/*}"; var="GH_OTHERS_${sha}"; echo "${!var:-0}" ;;
  "run cancel "*) exit "${GH_CANCEL_EXIT:-0}" ;;
  *) exit 1 ;;
esac
"""


def load() -> dict:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))


def script() -> str:
    return next(s["run"] for s in load()["runs"]["steps"] if s["name"] == "Cancel")


def runs(*items: tuple[int, str]) -> str:
    return json.dumps([{"databaseId": i, "displayTitle": t} for i, t in items])


def run(tmp_path: Path, **given: str):  # type: ignore[no-untyped-def]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_LOG": str(tmp_path / "calls"), "GH_TOKEN": "t", "REPO": "o/r",
        "NUMBER": "7", "CURRENT": HEAD, "WORKFLOW_REF": "o/r/.github/workflows/e2e-pr.yml@refs/heads/main",
        "GH_COMMITS": f"{OLDER}\n{OLD}\n{HEAD}",
    }  # fmt: skip
    env.update(given)
    done = subprocess.run(["bash", "-c", script()], env=env, capture_output=True, text=True, check=False)
    log = tmp_path / "calls"
    return done.returncode, (log.read_text().splitlines() if log.exists() else []), done.stdout + done.stderr


def cancelled(calls: list[str]) -> list[str]:
    return [c.split()[2] for c in calls if c.startswith("run cancel ")]


def test_every_input_used_is_declared_and_every_input_declared_is_used() -> None:
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", ACTION.read_text(encoding="utf-8")))
    assert used == set(load()["inputs"])


def test_nothing_is_expanded_into_the_script_and_nothing_is_checked_out() -> None:
    for step in load()["runs"]["steps"]:
        assert "${{" not in step.get("run", "")
        assert "checkout" not in step.get("uses", "")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path


def test_older_runs_are_cancelled(home: Path) -> None:
    titles = runs(
        (11, f"E2E o/r:branch @ {OLD}"),
        (12, f"E2E #7 · NO-ISSUE: fix the thing @ {OLDER}"),
        (13, f"E2E whatever it says @ {OLD}"),
    )
    code, calls, _ = run(home, GH_RUNS_in_progress=titles)
    assert code == 0 and cancelled(calls) == ["11", "12", "13"]


def test_the_run_of_the_head_commit_and_runs_of_other_pull_requests_are_left_alone(home: Path) -> None:
    titles = runs(
        (21, f"E2E #7 · the title @ {HEAD}"),  # the commit that was decided on
        (22, f"E2E #8 · another pull request @ {OTHER}"),  # not a commit of this pull request
        (23, "E2E #7 (started by hand)"),  # no commit at all
        (24, f"E2E #7 · a title that says @ {OLD} in the middle"),  # the commit must end the title
        (25, f"E2E #9 · other @ {OLD[:39]}"),  # not 40 digits
    )
    code, calls, _ = run(home, GH_RUNS_in_progress=titles)
    assert code == 0 and cancelled(calls) == []


def test_a_title_with_at_signs_in_it_is_read_by_its_last_part(home: Path) -> None:
    titles = runs((31, f"E2E #7 · notify @ everyone @ {OLD}"))
    assert cancelled(run(home, GH_RUNS_queued=titles)[1]) == ["31"]


def test_a_commit_that_belongs_to_another_open_pull_request_is_not_cancelled(home: Path) -> None:
    titles = runs((41, f"E2E x @ {OLD}"), (42, f"E2E y @ {OLDER}"))
    code, calls, out = run(home, GH_RUNS_in_progress=titles, **{f"GH_OTHERS_{OLD}": "1"})
    assert code == 0 and cancelled(calls) == ["42"]
    assert "Leaving run 41: its commit belongs to another open pull request" in out


@pytest.mark.parametrize("status", ["queued", "waiting", "pending", "in_progress"])
def test_every_status_of_a_waiting_or_running_run_is_looked_at(home: Path, status: str) -> None:
    code, calls, _ = run(home, **{f"GH_RUNS_{status}": runs((51, f"E2E z @ {OLD}"))})
    assert code == 0 and cancelled(calls) == ["51"]
    asked = [c for c in calls if c.startswith("run list ")]
    assert len(asked) == 4 and all("--workflow e2e-pr.yml" in c and "--repo o/r" in c for c in asked)


def test_a_run_that_has_already_finished_is_not_an_error(home: Path) -> None:
    code, calls, out = run(home, GH_RUNS_in_progress=runs((61, f"E2E z @ {OLD}")), GH_CANCEL_EXIT="1")
    assert code == 0 and "run 61 already finished" in out


def test_the_workflow_file_is_the_one_the_reference_names(home: Path) -> None:
    _, calls, _ = run(home, WORKFLOW_REF="o/r/.github/workflows/e2e-pr.yaml@refs/pull/7/merge")
    assert all("--workflow e2e-pr.yaml" in c for c in calls if c.startswith("run list "))


@pytest.mark.parametrize(
    "given",
    [
        {"NUMBER": "7; id"}, {"NUMBER": ""}, {"CURRENT": "xyz"}, {"CURRENT": HEAD.upper()}, {"CURRENT": ""},
        {"WORKFLOW_REF": "o/r/.github/workflows/$(id).yml@refs/heads/main"}, {"WORKFLOW_REF": ""},
        {"WORKFLOW_REF": "o/r/.github/workflows/notes.txt@refs/heads/main"},
    ],
)  # fmt: skip
def test_a_value_that_is_not_what_it_should_be_stops_the_step_before_any_call(
    home: Path, given: dict[str, str]
) -> None:
    code, calls, _ = run(home, **given)
    assert code == 1 and calls == []


def test_the_decision_workflow_calls_it_with_the_head_commit_it_decided_on() -> None:
    steps = yaml.safe_load(DECIDE.read_text(encoding="utf-8"))["jobs"]["decide"]["steps"]
    step = next(s for s in steps if s.get("uses") == "$/.github/actions/cancel-older")
    assert step["if"] == "steps.report.outputs.number != '' && steps.by-hand.outputs.manual != 'true'"
    assert step["with"] == {
        "github-token": "${{ github.token }}",
        "repo": "${{ github.repository }}",
        "number": "${{ steps.report.outputs.number }}",
        "head-sha": "${{ steps.report.outputs.sha }}",
        "workflow-ref": "${{ github.workflow_ref }}",
    }
