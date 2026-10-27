"""``/test ci-<job>`` runs a failed job of the repository's own workflows again: what the decision says about it, and
what the program that does it checks first."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from osac_ci import cheap_jobs, cheap_stage, cli
from osac_ci.cheap_jobs import CheapJob
from osac_ci.e2e_start import NEEDED, Action, Inputs, Review, Suite, decide, to_json
from osac_ci.github.api import Response
from osac_ci.policy import parse_policy

pytestmark = pytest.mark.unit
HEAD = "a" * 40
REPO = "o/r"
SUITES = (Suite("vmaas", "E2E / VMaaS", (), ("code",)),)


def job(name: str, state: str, *, run_done: bool = True, job_id: int = 1) -> CheapJob:
    return CheapJob(
        name, state, f"https://x/{job_id}", job_id, 10, run_done, state == "active", "CI", cheap_jobs.command_of(name)
    )


def inputs(*jobs: CheapJob, requested: tuple[str, ...], suites: tuple[Suite, ...] = SUITES, **more: Any) -> Inputs:
    base = Inputs(
        number=7, head_sha=HEAD, author="alice", state="open", draft=False,
        reviews=(Review("bob", "APPROVED", "MEMBER", HEAD, permission="write"),),
        check_runs=(), changed_files=("svc/main.go",), changed_files_known=True, cheap=(), suites=suites,
        filters={"code": ["**/*.go"]}, requested=requested, requested_by="carol", cheap_jobs=jobs,
    )  # fmt: skip
    return replace(base, **more)


def test_a_failed_job_of_a_finished_run_is_run_again_and_no_suite_starts() -> None:
    d = decide(inputs(job("unit", "failed", job_id=5), job("lint", "passed", job_id=6), requested=("ci-unit",)))
    assert d.action is Action.NOTHING_TO_START and d.suites == () and d.manual
    assert d.reruns == (("unit", 5),)
    assert d.notes == (("ci-unit", "unit was run again by carol"),)
    assert json.loads(to_json(d))["reruns"] == [{"name": "unit", "job": 5}]


def test_a_cancelled_job_is_run_again_too() -> None:
    assert decide(inputs(job("unit", "cancelled", job_id=5), requested=("ci-unit",))).reruns == (("unit", 5),)


@pytest.mark.parametrize(
    ("state", "run_done", "says"),
    [
        ("passed", True, "already passed on this commit"),
        ("active", False, "is already running on this commit"),
        ("failed", False, "cannot be run again while the rest of its run still goes"),
    ],
)
def test_a_job_that_cannot_be_run_again_says_why_and_nothing_is_run(state: str, run_done: bool, says: str) -> None:
    d = decide(inputs(job("unit", state, run_done=run_done), requested=("ci-unit",)))
    assert d.reruns == () and d.suites == ()
    assert says in d.notes[0][1]


def test_a_job_and_a_suite_asked_for_together_both_happen() -> None:
    d = decide(inputs(job("unit", "failed", job_id=5), requested=("ci-unit", "vmaas")))
    assert d.action is Action.START and d.suites == ("vmaas",) and d.reruns == (("unit", 5),)


def test_a_name_that_is_neither_a_suite_nor_a_job_is_still_not_a_suite() -> None:
    d = decide(inputs(job("unit", "failed"), requested=("ci-nope",)))
    assert d.reruns == () and "is not a suite of this repository" in d.notes[0][1]


def test_a_suite_with_the_name_of_a_job_command_is_the_suite() -> None:
    suites = (*SUITES, Suite("ci-unit", "E2E / Odd", (), ("code",)))
    d = decide(inputs(job("unit", "failed", job_id=5), requested=("ci-unit",), suites=suites))
    assert d.suites == ("ci-unit",) and d.reruns == ()


def test_the_bare_command_never_runs_a_job_again() -> None:
    d = decide(inputs(job("unit", "failed"), requested=(NEEDED,)))
    assert d.reruns == ()


def test_a_job_without_a_command_cannot_be_asked_for() -> None:
    d = decide(inputs(replace(job("unit", "failed"), command=""), requested=("ci-unit",)))
    assert d.reruns == ()


SHA_URL = f"/repos/{REPO}/commits/{HEAD}/check-runs"
POLICY_TEXT = """
version: 1
repo: o/r
jobs:
  lint:
    kind: cheap
    check: "CI / lint"
    workflow: ci-lint.yml
"""


def check(name: str, run: int, job_id: int, conclusion: str | None = "failure") -> dict[str, Any]:
    url = f"https://github.com/{REPO}/actions/runs/{run}/job/{job_id}"
    status = "completed" if conclusion else "in_progress"
    return {"id": job_id, "name": name, "status": status, "conclusion": conclusion,
            "app": {"slug": "github-actions"}, "details_url": url, "html_url": url}  # fmt: skip


def wf_id(name: str) -> int:
    return 1000 + sum(map(ord, name))


class Github:
    def __init__(self, checks: list[dict[str, Any]], runs: dict[int, tuple[str, str]], post: int = 201) -> None:
        self.checks, self.runs, self.post, self.posted = checks, runs, post, []
        self.statuses: list[dict[str, Any]] = []  # the lines of the app, newest first
        self.titles: dict[int, tuple[str, str]] = {}  # run -> (title, workflow path) for a run the controller started
        self.written: list[tuple[str, Any]] = []

    def request(self, method: str, path: str, *, params: Any = None, body: Any = None) -> Response:
        if method == "POST" and path.endswith(("/statuses/" + HEAD, "/check-runs")):
            self.written.append((path, body))
            return Response(201, {}, {})
        if method == "POST":
            self.posted.append(path)
            return Response(self.post, {"message": "nope"} if self.post >= 300 else {}, {})
        if path.endswith("/statuses"):
            return Response(200, self.statuses, {})
        if "/actions/runs/" in path and int(path.rsplit("/", 1)[1]) in self.titles:
            run = int(path.rsplit("/", 1)[1])
            title, wf = self.titles[run]
            return Response(200, {"display_title": title, "path": wf, "status": self.runs[run][1] or "completed"}, {})
        if path == SHA_URL:
            return Response(200, {"check_runs": self.checks}, {})
        if path.endswith("/actions/workflows"):
            return Response(200, {"workflows": [{"id": wf_id(n), "name": n} for n, _ in self.runs.values()]}, {})
        run = int(path.rsplit("/", 1)[1])
        if run not in self.runs:
            return Response(404, {"message": "Not Found"}, {})
        name, status = self.runs[run]
        return Response(
            200, {"name": f"{name} · #5", "workflow_id": wf_id(name), "run_number": run, "status": status}, {}
        )


def rerun(github: Github, ids: list[int], workflows: tuple[str, ...] = ("CI",), sha: str = HEAD) -> Any:
    return cheap_jobs.rerun_jobs(github, REPO, sha, ids, workflows)  # type: ignore[arg-type]


def test_a_failed_job_is_run_again_through_the_jobs_own_rerun_call() -> None:
    github = Github([check("unit", 10, 5), check("lint", 10, 6, "success")], {10: ("CI", "completed")})
    assert rerun(github, [5]) == [("unit", True, "run again")]
    assert github.posted == [f"/repos/{REPO}/actions/jobs/5/rerun"]


@pytest.mark.parametrize(
    ("asked", "checks", "runs", "workflows"),
    [
        (7, [check("unit", 10, 5)], {10: ("CI", "completed")}, ("CI",)),  # not a job of the commit
        (6, [check("lint", 10, 6, "success")], {10: ("CI", "completed")}, ("CI",)),  # it passed
        (5, [check("unit", 10, 5)], {10: ("CI", "in_progress")}, ("CI",)),  # its run still goes
        (5, [check("unit", 10, 5)], {10: ("Other", "completed")}, ("CI",)),  # a workflow nobody listed
        (5, [check("unit", 10, 5)], {10: ("CI", "completed")}, ()),  # nothing is listed
        (5, [check("unit", 10, 5, None)], {10: ("CI", "completed")}, ("CI",)),  # it runs
    ],
)
def test_a_job_that_is_not_a_failed_job_of_a_finished_listed_run_is_left_alone(
    asked: int, checks: list[dict[str, Any]], runs: dict[int, tuple[str, str]], workflows: tuple[str, ...]
) -> None:
    github = Github(checks, runs)
    [(_, started, _)] = rerun(github, [asked], workflows)
    assert started is False and github.posted == []


def test_a_refused_call_is_reported_and_the_next_job_is_still_tried() -> None:
    github = Github([check("unit", 10, 5), check("lint", 10, 6)], {10: ("CI", "completed")}, post=403)
    done = rerun(github, [5, 6])
    assert [(n, ok) for n, ok, _ in done] == [("unit", False), ("lint", False)]
    assert "403: nope" in done[0][2] and len(github.posted) == 2


def test_the_same_job_twice_is_run_again_once() -> None:
    github = Github([check("unit", 10, 5)], {10: ("CI", "completed")})
    rerun(github, [5, 5])
    assert len(github.posted) == 1


@pytest.mark.parametrize("ids", [[0], [-1], [True], ["5"], [1.5]])
def test_an_id_that_is_not_a_positive_number_is_refused(ids: list[Any]) -> None:
    with pytest.raises(ValueError, match="list of ids"):
        rerun(Github([], {}), ids)


def test_a_commit_that_is_not_40_hex_digits_is_refused() -> None:
    with pytest.raises(ValueError, match="invalid commit"):
        rerun(Github([], {}), [1], sha="../x")


def test_the_command_runs_the_jobs_again_and_exits_with_3_when_one_could_not(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    github = Github([check("unit", 10, 5)], {10: ("CI", "completed")})
    monkeypatch.setattr(cli, "build_client", lambda **_: github)
    monkeypatch.setattr(cli, "load_policy", lambda _p: parse_policy(POLICY_TEXT))
    argv = ["e2e-rerun", "--repo", REPO, "--sha", HEAD, "--cheap-workflows", '["CI"]', "--policy", "p.yml"]
    assert cli.main([*argv, "--jobs", "[5]"]) == 0
    assert "Ran again unit: run again" in capsys.readouterr().out
    assert cli.main([*argv, "--jobs", "[9]"]) == 3
    assert "left alone" in capsys.readouterr().err
    assert cli.main([*argv, "--jobs", "5"]) == 3
    assert "must be a JSON list" in capsys.readouterr().err


ROOT = Path(__file__).resolve().parents[2]
ACTION = ROOT / ".github" / "actions" / "e2e-rerun" / "action.yml"
DECIDE = ROOT / ".github" / "workflows" / "e2e-pr-decide.yml"


def test_the_action_declares_what_it_uses_and_expands_nothing_into_a_script() -> None:
    text = ACTION.read_text(encoding="utf-8")
    action = yaml.safe_load(text)
    assert set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", text)) == set(action["inputs"])
    for step in action["runs"]["steps"]:
        assert "${{" not in step.get("run", "") and "checkout" not in step.get("uses", "")
    assert '"--cheap-workflows=$CHEAP_WORKFLOWS"' in text and '"${#SHA}" -ne 40' in text


def test_the_decision_runs_jobs_again_with_a_token_made_for_that_step_only() -> None:
    steps = yaml.safe_load(DECIDE.read_text(encoding="utf-8"))["jobs"]["decide"]["steps"]
    token = next(s for s in steps if s.get("id") == "rerun-token")
    assert {k: v for k, v in token["with"].items() if k.startswith("permission-")} == {
        "permission-actions": "write",
        "permission-checks": "write",
        "permission-statuses": "${{ inputs.lines-kind == 'statuses' && 'write' || 'read' }}",
    }
    call = next(s for s in steps if str(s.get("uses", "")).endswith("/e2e-rerun"))
    asked = "steps.by-hand.outputs.reruns != '' && steps.by-hand.outputs.reruns != '[]'"
    assert token["if"] == asked and call["if"] == asked, "no token is made unless a job is asked for"
    assert call["continue-on-error"] is True
    assert call["with"]["github-token"] == "${{ steps.rerun-token.outputs.token }}"
    assert call["with"]["cheap-workflows"] == "${{ inputs.cheap-workflows }}"
    order = [s.get("id") or s.get("uses", "") for s in steps]
    assert order.index("rerun-token") < order.index(call["uses"]) < order.index("$/.github/actions/manual-comment")
    decide_step = next(s for s in steps if s.get("id") == "decide")
    assert decide_step["with"]["cheap-workflows"] == "${{ inputs.cheap-workflows }}"


def line(context: str, state: str, link: str, creator: str = "osac-ci[bot]") -> dict[str, Any]:
    return {"context": context, "state": state, "target_url": link, "description": "x", "creator": {"login": creator}}


def dispatched(
    failing: str = "failure", title: str | None = None, path: str = ".github/workflows/ci-lint.yml@refs/heads/main"
) -> Github:
    github = Github([], {})
    github.runs[20] = ("lint", "completed")
    github.titles[20] = (title or f"CI job · lint · #7 · {HEAD}", path)
    github.statuses = [line("CI / lint", failing, f"https://github.com/{REPO}/actions/runs/20/job/77")]
    return github


def run_policy(github: Github, ids: list[int]) -> Any:
    return cheap_stage.rerun(github, REPO, HEAD, ids, (), parse_policy(POLICY_TEXT), lines_kind="statuses")  # type: ignore[arg-type]


def test_a_failed_cheap_job_of_the_controller_is_run_again_its_line_says_so_and_the_sum_waits_for_it() -> None:
    github = dispatched()
    assert run_policy(github, [77]) == [("lint", True, "run again")]
    assert github.posted == [f"/repos/{REPO}/actions/jobs/77/rerun"]
    again = [b for p, b in github.written if p.endswith("/statuses/" + HEAD)]
    assert again == [
        {"state": "pending", "context": "CI / lint", "description": "Running again",
         "target_url": f"https://github.com/{REPO}/actions/runs/20"}
    ]  # fmt: skip
    sums = [b for p, b in github.written if p.endswith("/check-runs")]
    assert sums and sums[0]["name"] == "CI" and sums[0]["status"] == "in_progress"


@pytest.mark.parametrize(
    "github",
    [
        dispatched(path=".github/workflows/ci-other.yml@refs/heads/main"),  # not the workflow the policy names
        dispatched(title=f"CI job · lint · #7 · {'c' * 40}"),  # a run of another commit
        dispatched(title="E2E suite · lint · #7 · " + HEAD),  # not a cheap job's run
        dispatched(failing="success"),  # it passed
        dispatched(title=f"CI job · nope · #7 · {HEAD}"),  # a job the policy does not have
    ],
)
def test_a_dispatched_job_that_does_not_match_the_policy_or_the_commit_is_left_alone(github: Github) -> None:
    [(_, started, says)] = run_policy(github, [77])
    assert started is False and "left alone" in says and github.posted == [] and github.written == []


def test_a_line_that_is_not_the_apps_is_never_a_cheap_job() -> None:
    github = dispatched()
    github.statuses = [line("CI / lint", "failure", github.statuses[0]["target_url"], creator="github-actions[bot]")]
    [(_, started, _)] = run_policy(github, [77])
    assert started is False and github.posted == []


def test_the_panel_lists_the_jobs_the_controller_started_with_their_command() -> None:
    github = dispatched()
    [job] = cheap_jobs.read_dispatched_jobs(github, REPO, HEAD, [])  # type: ignore[arg-type]
    assert (job.name, job.state, job.command, job.job_id, job.run_done, job.line) == (
        "lint", "failed", "ci-lint", 77, True, "CI / lint",
    )  # fmt: skip
    assert job.can_run_again and job.workflow == "ci-lint.yml"


def test_the_check_that_adds_the_jobs_up_is_no_line_of_a_job_even_though_it_links_into_a_run() -> None:
    github = dispatched()
    sum_link = f"https://github.com/{REPO}/actions/runs/20"
    checks = [{"id": 9, "name": "CI", "status": "completed", "conclusion": "failure", "app": {"slug": "osac-ci"},
               "details_url": sum_link}]  # fmt: skip
    jobs = cheap_jobs.read_dispatched_jobs(github, REPO, HEAD, checks)  # type: ignore[arg-type]
    assert [(j.name, j.line) for j in jobs] == [("lint", "CI / lint")], "one job, from its own line"
    # a repository that names its sum differently says so, and the default name is then a line like any other
    other = cheap_jobs.read_dispatched_jobs(github, REPO, HEAD, checks, ("Cheap",))  # type: ignore[arg-type]
    assert sorted(j.line for j in other) == ["CI", "CI / lint"]


# the CI check of the controller in the decision --------------------------------------------------------------------


def app_ci(conclusion: str | None) -> Any:
    from osac_ci.model import CheckRun

    return CheckRun(
        "CI", "completed" if conclusion else "in_progress", conclusion, "2026-10-10T10:00:00Z", app="osac-ci"
    )


def with_ci(*runs: Any, ci: str = "CI") -> Inputs:
    return inputs(check_runs=tuple(runs), ci_check=ci, requested=())


def test_a_failed_ci_check_of_the_controller_holds_the_start_back() -> None:
    d = decide(with_ci(app_ci("failure")))
    assert d.action is Action.BLOCKED_CHEAP_FAILED and d.cheap_failed == ("CI: conclusion: failure",)


@pytest.mark.parametrize("runs", [(), (app_ci(None),)])
def test_a_ci_check_that_goes_or_is_not_written_yet_does_not_delay_the_start(runs: tuple[Any, ...]) -> None:
    d = decide(with_ci(*runs))
    assert d.action is Action.START and d.cheap_pending, "named, but not holding back"


def test_a_passed_ci_check_of_the_controller_starts_the_e2e_and_names_nothing_pending() -> None:
    d = decide(with_ci(app_ci("success")))
    assert d.action is Action.START and d.cheap_pending == () and d.cheap_failed == ()


def test_a_ci_check_of_another_app_is_not_the_controllers() -> None:
    from osac_ci.model import CheckRun

    other = CheckRun("CI", "completed", "failure", "2026-10-10T10:00:00Z", app="github-actions")
    assert decide(with_ci(other)).action is Action.START, "only the app's own CI check counts"


def test_without_the_controller_the_decision_reads_the_ruleset_as_before() -> None:
    assert decide(inputs(check_runs=(app_ci("failure"),), requested=())).action is Action.START
