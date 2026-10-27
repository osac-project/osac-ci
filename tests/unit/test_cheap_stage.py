"""The cheap stage started and followed by the app: what is dispatched, what the lines and the CI check say, and what is
refused before anything is written."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import pytest

from osac_ci import cheap_stage
from osac_ci.github.api import Response
from osac_ci.policy import parse_policy

pytestmark = pytest.mark.unit
HEAD = "a" * 40
OLD = "b" * 40
POLICY = parse_policy("""
version: 1
repo: o/r
path_filters:
  mode: enforce
  filters: {code: ["**/*.go"], docs: ["docs/**"]}
jobs:
  lint:
    kind: cheap
    check: "CI / lint"
    workflow: ci-lint.yml
  unit:
    kind: cheap
    check: "CI / unit"
    workflow: ci-unit.yml
    filters_any: [code]
  not-dispatched:
    kind: cheap
    check: "something else"
  e2e-alpha:
    kind: e2e
    check: "E2E / Alpha"
    suite: alpha
    workflow: e2e-alpha.yml
""")


class Gh:
    """A pull request, the workflows that can be dispatched, the runs and what is written (check runs, newest last)."""

    def __init__(self, files: list[str] | None = None, head: str = HEAD, state: str = "open") -> None:
        self.files = files if files is not None else ["main.go"]
        self.head, self.state = head, state
        self.dispatched: list[tuple[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.runs: dict[int, tuple[str, str]] = {}
        self.next_run = 500

    def line(self, name: str, status: str, conclusion: str | None, title: str, run: int) -> None:
        self.checks.append(
            {"id": len(self.checks) + 1, "name": name, "status": status, "conclusion": conclusion,
             "started_at": f"2026-10-10T10:{len(self.checks):02d}:00Z", "app": {"slug": "osac-ci"},
             "output": {"title": title}, "details_url": f"https://github.com/o/r/actions/runs/{run}/job/1"}
        )  # fmt: skip

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path == "/repos/o/r":
            return Response(200, {"default_branch": "main"}, {})
        if method == "GET" and path == "/repos/o/r/pulls/7":
            return Response(200, {"state": self.state, "head": {"sha": self.head, "repo": {"full_name": "o/r"}}}, {})
        if method == "GET" and path == "/repos/o/r/pulls/7/files":
            return Response(200, [{"filename": f} for f in self.files], {})
        if method == "GET" and path.endswith("/check-runs"):
            name = (params or {}).get("check_name")
            return Response(200, {"check_runs": [c for c in self.checks if name in (None, c["name"])]}, {})
        if method == "GET" and path.endswith("/statuses"):
            return Response(200, [], {})
        if method == "GET" and path.endswith("/jobs"):
            return Response(200, {"jobs": []}, {})
        if method == "GET" and "/actions/runs/" in path:
            status, conclusion = self.runs[int(path.rsplit("/", 1)[1])]
            return Response(200, {"status": status, "conclusion": conclusion or None}, {})
        if method == "POST" and "/dispatches" in path:
            self.dispatched.append((path.split("/workflows/")[1].split("/")[0], body))
            self.next_run += 1
            self.runs[self.next_run] = ("queued", "")
            return Response(200, {"workflow_run_id": self.next_run}, {})
        if method == "POST" and path.endswith("/check-runs"):
            self.checks.append({**body, "id": len(self.checks) + 1, "app": {"slug": "osac-ci"},
                                "started_at": f"2026-10-10T11:{len(self.checks):02d}:00Z"})  # fmt: skip
            return Response(201, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")

    def latest(self, name: str) -> tuple[str, str] | None:
        mine = [c for c in self.checks if c["name"] == name]
        if not mine:
            return None
        last = mine[-1]
        return (last.get("conclusion") or last["status"], last["output"]["title"])


def start(gh: Gh) -> cheap_stage.Outcome:
    return cheap_stage.start(gh, "o/r", 7, POLICY, lines_kind="checks")  # type: ignore[arg-type]


def test_only_the_cheap_jobs_that_name_a_workflow_and_that_the_change_needs_are_started() -> None:
    gh = Gh(["main.go"])
    done = start(gh)
    assert [f for f, _ in gh.dispatched] == ["ci-lint.yml", "ci-unit.yml"]
    assert [s.suite for s in done.started] == ["lint", "unit"] and done.failed == ()
    assert gh.dispatched[0][1]["inputs"] == {
        "repo": "o/r", "sha": HEAD, "pr-number": "7", "fork-pr-author": "", "fork-pr-author-association": "",
    }  # fmt: skip
    assert gh.dispatched[0][1]["return_run_details"] is True


def test_a_change_that_no_filter_matches_starts_only_the_jobs_without_one() -> None:
    gh = Gh(["docs/readme.md"])
    start(gh)
    assert [f for f, _ in gh.dispatched] == ["ci-lint.yml"]


def test_the_lines_wait_for_a_runner_and_the_ci_check_waits_for_the_jobs() -> None:
    gh = Gh()
    done = start(gh)
    assert gh.latest("CI / lint") == ("queued", "Waiting for a runner")
    assert gh.latest("CI / unit") == ("queued", "Waiting for a runner")
    assert gh.latest("CI") == ("in_progress", "Running: lint, unit") and done.sum == "pending"


def test_starting_again_for_the_same_state_does_not_pile_up_ci_checks() -> None:
    gh = Gh()
    start(gh)
    first = len([c for c in gh.checks if c["name"] == "CI"])
    cheap_stage.add_up(gh, "o/r", HEAD, {"lint": "CI / lint", "unit": "CI / unit"}, "CI", "", "checks")  # type: ignore[arg-type]
    assert len([c for c in gh.checks if c["name"] == "CI"]) == first


def test_a_pull_request_that_needs_no_cheap_job_gets_a_passed_ci_check() -> None:
    policy = parse_policy("version: 1\nrepo: o/r\njobs:\n  e2e-a:\n    kind: e2e\n    check: x\n    suite: a\n")
    gh = Gh()
    done = cheap_stage.start(gh, "o/r", 7, policy, lines_kind="checks")  # type: ignore[arg-type]
    assert gh.dispatched == [] and gh.latest("CI") == ("success", cheap_stage.NOT_AFFECTED) and done.sum == "success"


def test_a_closed_pull_request_starts_nothing() -> None:
    gh = Gh(state="closed")
    assert "not open" in start(gh).sum and gh.dispatched == [] and gh.checks == []


def test_a_policy_names_the_sum_check() -> None:
    policy = parse_policy(
        "version: 1\nrepo: o/r\nci_check: Cheap\njobs:\n  a:\n    kind: cheap\n    check: l\n    workflow: a.yml\n"
    )
    gh = Gh()
    cheap_stage.start(gh, "o/r", 7, policy, lines_kind="checks")  # type: ignore[arg-type]
    assert gh.latest("Cheap") == ("in_progress", "Running: a")


RUN_LINT, RUN_UNIT = 501, 502
TITLE = f"CI job · lint · #7 · {HEAD}"
FILE = ".github/workflows/ci-lint.yml"


def ended(gh: Gh, title: str = TITLE, path: str = FILE, run: int = RUN_LINT) -> str:
    return cheap_stage.ended(gh, "o/r", run, title, path, POLICY, lines_kind="checks")  # type: ignore[arg-type]


def two_jobs(gh: Gh, lint: tuple[str, str], unit: tuple[str, str]) -> None:
    gh.runs[RUN_LINT], gh.runs[RUN_UNIT] = lint, unit
    gh.line("CI / lint", "queued", None, "Waiting for a runner", RUN_LINT)
    gh.line("CI / unit", "queued", None, "Waiting for a runner", RUN_UNIT)


def test_the_last_job_to_end_with_all_passed_makes_the_ci_check_green() -> None:
    gh = Gh()
    two_jobs(gh, ("completed", "success"), ("completed", "success"))
    gh.checks[1] = {**gh.checks[1], "status": "completed", "conclusion": "success", "output": {"title": "Passed"}}
    ended(gh)
    assert gh.latest("CI / lint") == ("success", "Passed")
    assert gh.latest("CI") == ("success", "Passed: lint, unit")


def test_a_job_that_ended_while_another_still_goes_leaves_the_ci_check_running() -> None:
    gh = Gh()
    two_jobs(gh, ("completed", "success"), ("in_progress", ""))
    ended(gh)
    assert gh.latest("CI") == ("in_progress", "Running: unit")


def test_a_failed_job_makes_the_ci_check_fail_once_nothing_goes() -> None:
    gh = Gh()
    two_jobs(gh, ("completed", "failure"), ("completed", "success"))
    gh.checks[1] = {**gh.checks[1], "status": "completed", "conclusion": "success"}
    ended(gh)
    assert gh.latest("CI / lint") == ("failure", "Failed")
    assert gh.latest("CI") == ("failure", "Failed: lint")


def test_a_job_that_has_not_ended_changes_nothing() -> None:
    gh = Gh()
    two_jobs(gh, ("in_progress", ""), ("queued", ""))
    before = len(gh.checks)
    assert "has not ended" in ended(gh) and len(gh.checks) == before


@pytest.mark.parametrize(
    ("title", "path", "says"),
    [
        ("E2E suite · alpha · #7 · " + HEAD, FILE, "not the run of a cheap job"),
        (f"CI job · lint · #7 · {HEAD}", ".github/workflows/ci-unit.yml", "not the workflow of the cheap job lint"),
        (f"CI job · nope · #7 · {HEAD}", FILE, "not the workflow of the cheap job nope"),
        (f"CI job · not-dispatched · #7 · {HEAD}", FILE, "not the workflow of the cheap job not-dispatched"),
        (f"CI job · lint · #7 · {OLD}", FILE, "moved on to another commit"),
    ],
)
def test_an_event_that_does_not_match_the_policy_or_the_pull_request_is_not_believed(
    title: str, path: str, says: str
) -> None:
    gh = Gh()
    two_jobs(gh, ("completed", "success"), ("completed", "success"))
    before = len(gh.checks)
    assert says in ended(gh, title, path) and len(gh.checks) == before


def test_a_line_that_a_newer_run_of_the_job_owns_is_left_alone() -> None:
    gh = Gh()
    two_jobs(gh, ("completed", "failure"), ("completed", "success"))
    gh.line("CI / lint", "in_progress", None, "Running", 999)
    gh.runs[999] = ("in_progress", "")
    before = len(gh.checks)
    assert "newer run" in ended(gh) and len(gh.checks) == before


def test_the_name_of_a_run_is_what_the_wrappers_give_it() -> None:
    assert cheap_stage.run_name("lint", 7, HEAD) == TITLE
    assert cheap_stage.read_run_name(TITLE) == ("lint", 7, HEAD)
    for bad in (
        "CI job · Lint · #7 · " + HEAD,
        "CI job · lint · #7 · abc",
        "E2E suite · lint · #7 · " + HEAD,
        TITLE + "x",
    ):
        assert cheap_stage.read_run_name(bad) is None
    assert re.fullmatch(cheap_stage.RUN_NAME, TITLE)


def test_a_needed_job_with_no_line_yet_keeps_the_ci_check_from_passing() -> None:
    gh = Gh()
    gh.line("CI / lint", "completed", "success", "Passed", RUN_LINT)
    cheap_stage.add_up(gh, "o/r", HEAD, {"lint": "CI / lint", "unit": "CI / unit"}, "CI", "", "checks")  # type: ignore[arg-type]
    assert gh.latest("CI") == ("in_progress", "Running: unit")


def _workflow(name: str) -> dict[str, Any]:
    import yaml
    from helpers import ROOT

    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_the_front_door_runs_no_code_of_the_pull_request_and_asks_the_app_for_what_it_needs_only() -> None:
    wf = _workflow("cheap-start.yml")
    job = wf["jobs"]["start"]
    assert wf["permissions"] == {} and job["permissions"] == {"contents": "read"}
    for step in job["steps"]:
        assert "ref" not in (step.get("with") or {}), "nothing of the pull request is checked out"
        assert "${{" not in step.get("run", "")
    token = next(s for s in job["steps"] if s.get("id") == "app-token")["with"]
    assert {k: v for k, v in token.items() if k.startswith("permission-")} == {
        "permission-actions": "write",
        "permission-checks": "write",
        "permission-statuses": "${{ inputs.lines-kind == 'statuses' && 'write' || '' }}",
        "permission-pull-requests": "read",
    }
    assert "pull_request_target" in job["if"] and job["concurrency"]["cancel-in-progress"] is True
    decide = _workflow("e2e-pr-decide.yml")["jobs"]
    assert "cheap-start" not in decide, "the pull request must not get a skipped row for every job of the decision"
    assert {"cheap-ended-find", "cheap-ended"} <= set(decide)


def test_the_end_of_the_last_job_says_to_wake_the_e2e_decision_and_an_end_that_settles_nothing_does_not() -> None:
    gh = Gh()
    two_jobs(gh, ("completed", "success"), ("completed", "success"))
    gh.checks[1] = {**gh.checks[1], "status": "completed", "conclusion": "success"}
    said, number, summed = cheap_stage.ended_detail(gh, "o/r", RUN_LINT, TITLE, FILE, POLICY, lines_kind="checks")  # type: ignore[arg-type]
    assert (number, summed) == (7, "success") and said == "CI check: success"
    gh2 = Gh()
    two_jobs(gh2, ("completed", "success"), ("in_progress", ""))
    assert cheap_stage.ended_detail(gh2, "o/r", RUN_LINT, TITLE, FILE, POLICY, lines_kind="checks")[2] == "pending"  # type: ignore[arg-type]
    assert cheap_stage.ended_detail(gh2, "o/r", RUN_LINT, "not mine", FILE, POLICY)[1:] == (None, "")  # type: ignore[arg-type]


def test_the_command_tells_the_workflow_to_wake_the_decision_only_when_ci_got_its_answer(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from osac_ci import cli

    answers = iter([("done", 7, "success"), ("done", 7, "failure"), ("done", 7, "pending"), ("no", None, "")])
    monkeypatch.setattr(cheap_stage, "ended_detail", lambda *a, **k: next(answers))
    monkeypatch.setattr(cli, "build_client", lambda **_: object())
    monkeypatch.setattr(cli, "load_policy", lambda _p: POLICY)
    out = tmp_path / "out.json"
    argv = ["cheap-ended", "--policy", "p", "--repo", "o/r", "--run-id", "1", "--title", "t", "--workflow-path", "w"]
    seen = []
    for _ in range(4):
        assert cli.main([*argv, "--json-file", str(out)]) == 0
        seen.append(json.loads(out.read_text()))
    assert [s["wake"] for s in seen] == [True, True, False, False]


def test_the_wake_step_runs_only_on_an_answer_with_a_digits_only_number_and_cannot_fail_the_job() -> None:
    steps = _workflow("e2e-pr-decide.yml")["jobs"]["cheap-ended"]["steps"]
    wake = next(s for s in steps if s.get("name") == "Wake the E2E decision")
    assert wake["if"] == "steps.ended.outputs.wake == 'true'" and wake["continue-on-error"] is True
    assert "*[!0-9]*" in wake["run"] and "${{" not in wake["run"]
    assert wake["env"]["CALLER"] == "${{ github.workflow_ref }}", (
        "the caller's own workflow, not a name given by anyone"
    )
    token = next(s for s in steps if s.get("id") == "app-token")["with"]
    assert token["permission-actions"].startswith("write")
