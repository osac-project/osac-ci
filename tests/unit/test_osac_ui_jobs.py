"""osac-ui checks are mandatory exactly when osac-ui changes, and never otherwise."""

import pytest
from helpers import snap, with_check

from osac_ci.model import CheckRun, JobStatus, Mode, State
from osac_ci.planner import plan
from osac_ci.policy import Policy

pytestmark = pytest.mark.unit

UI_CHECKS = ("Lint", "Lint proxy (Go)", "Typecheck", "Run unit tests (osac-ui)")
UI_FILE = "osac-ui/src/App.tsx"


def entries(policy: Policy, files: tuple[str, ...], runs: tuple[CheckRun, ...], mode: Mode = Mode.PR):  # type: ignore[no-untyped-def]
    verdict = plan(snap(policy, changed_files=files, check_runs=runs), policy, mode)
    return verdict, {e.check: e for e in verdict.jobs}


def without(policy: Policy, *checks: str) -> tuple[CheckRun, ...]:
    return tuple(r for r in snap(policy).check_runs if r.name not in checks)


def test_a_pr_that_changes_osac_ui_waits_for_its_checks(osac_policy: Policy) -> None:
    verdict, jobs = entries(osac_policy, (UI_FILE,), without(osac_policy, *UI_CHECKS))
    assert all(jobs[c].status is JobStatus.WAITING for c in UI_CHECKS)
    assert verdict.state is State.CHECKS_RUNNING


def test_a_failed_osac_ui_check_blocks_the_pr(osac_policy: Policy) -> None:
    runs = with_check(osac_policy, "Typecheck", CheckRun("Typecheck", "completed", "failure"))
    verdict, jobs = entries(osac_policy, (UI_FILE, "docs/a.md"), runs)
    assert jobs["Typecheck"].status is JobStatus.FAILED and verdict.state is State.CHECKS_FAILED


def test_a_green_osac_ui_pr_is_ready(osac_policy: Policy) -> None:
    verdict, jobs = entries(osac_policy, (UI_FILE,), snap(osac_policy).check_runs)
    assert all(jobs[c].status is JobStatus.PASSED for c in UI_CHECKS) and verdict.state is State.READY_TO_ENQUEUE


def test_a_pr_that_does_not_touch_osac_ui_never_waits_for_them(osac_policy: Policy) -> None:
    verdict, jobs = entries(osac_policy, ("fulfillment-service/a.go",), without(osac_policy, *UI_CHECKS))
    assert all(jobs[c].status is JobStatus.NOT_APPLICABLE for c in UI_CHECKS)
    assert verdict.state is State.READY_TO_ENQUEUE


def test_the_merge_queue_does_not_wait_for_workflows_that_never_run_there(osac_policy: Policy) -> None:
    verdict, jobs = entries(osac_policy, (UI_FILE,), without(osac_policy, *UI_CHECKS), Mode.QUEUE)
    assert not set(UI_CHECKS) & set(jobs) and verdict.state is State.QUEUE_PASSED


# ---- mixed changes, look-alike folders, the command line -----------------------------------------------------------


def ui_entries(policy: Policy, *files: str) -> dict[str, JobStatus]:
    verdict = plan(snap(policy, changed_files=files, check_runs=without(policy, *UI_CHECKS)), policy)
    return {e.check: e.status for e in verdict.jobs if e.check in UI_CHECKS}


def test_a_mixed_pr_waits_for_the_osac_ui_checks_and_the_backend_ones(osac_policy: Policy) -> None:
    runs = without(osac_policy, *UI_CHECKS, "Run unit tests")
    verdict = plan(snap(osac_policy, changed_files=(UI_FILE, "fulfillment-service/a.go"), check_runs=runs), osac_policy)
    waiting = {e.check for e in verdict.jobs if e.status is JobStatus.WAITING}
    assert set(UI_CHECKS) <= waiting and "Run unit tests" in waiting


@pytest.mark.parametrize(
    "file",
    ["osac-ui-docs/a.md", "docs/osac-ui/a.md", "OSAC-UI/a.ts", "osac-uix/a.ts", "tools/osac-ui.sh", "osac-ui.md"],
)
def test_look_alike_paths_do_not_make_the_osac_ui_checks_mandatory(osac_policy: Policy, file: str) -> None:
    assert set(ui_entries(osac_policy, file).values()) == {JobStatus.NOT_APPLICABLE}


@pytest.mark.parametrize("file", ["osac-ui/a.ts", "osac-ui/deep/er/path/.hidden", "osac-ui/package.json"])
def test_every_file_under_osac_ui_makes_them_mandatory(osac_policy: Policy, file: str) -> None:
    assert set(ui_entries(osac_policy, file).values()) == {JobStatus.WAITING}


def test_a_failed_run_with_no_osac_ui_change_is_ignored(osac_policy: Policy) -> None:
    """A stray failure of a check with one of these names must not block a PR that has nothing to do with osac-ui."""
    runs = with_check(osac_policy, "Lint", CheckRun("Lint", "completed", "failure"))
    verdict = plan(snap(osac_policy, changed_files=("fulfillment-service/a.go",), check_runs=runs), osac_policy)
    assert verdict.state is State.READY_TO_ENQUEUE


def test_a_still_running_osac_ui_check_keeps_the_pr_in_checks_running(osac_policy: Policy) -> None:
    runs = with_check(osac_policy, "Typecheck", CheckRun("Typecheck", "in_progress"))
    verdict = plan(snap(osac_policy, changed_files=(UI_FILE,), check_runs=runs), osac_policy)
    assert verdict.state is State.CHECKS_RUNNING and "Typecheck" in verdict.headline


def test_the_folder_scoped_jobs_are_cheap_ungated_and_unique(osac_policy: Policy) -> None:
    scoped = [j for j in osac_policy.jobs.values() if j.check in UI_CHECKS]
    assert len(scoped) == 4 == len({j.check for j in scoped})
    assert all(j.kind == "cheap" and not j.needs_readiness and not j.exclude_paths for j in scoped)


def test_explain_reports_the_osac_ui_checks_for_an_osac_ui_change(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    import json

    from helpers import ROOT

    from osac_ci.cli import main

    snapshot = tmp_path / "s.json"
    snapshot.write_text(
        json.dumps(
            {
                "repo": "osac-project/osac",
                "number": 5,
                "head_sha": "a" * 40,
                "labels": ["lgtm", "approved", "jira/valid-reference"],
                "changed_files": [UI_FILE],
                "check_runs": [],
            }
        )
    )
    assert main(["explain", "--policy", str(ROOT / "policy" / "osac.yml"), "--snapshot", str(snapshot)]) == 0
    out = capsys.readouterr().out
    assert "| osac-ui-typecheck | Typecheck | waiting |" in out and "| osac-ui-lint | Lint | waiting |" in out


def test_moving_a_file_out_of_osac_ui_still_makes_the_checks_mandatory(osac_policy: Policy) -> None:
    """The adapter lists both names of a rename, so the folder that lost a file is a changed folder."""
    assert set(ui_entries(osac_policy, "shared/App.tsx", UI_FILE).values()) == {JobStatus.WAITING}
