"""The jobs of the cheap stage (the legs of CI) are read from the check runs of the head commit and listed in the status
block, with how to run a failed one again."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from osac_ci import cheap_jobs, cli, manual_run
from osac_ci.cheap_jobs import CheapJob
from osac_ci.github.api import GitHubError, Response

pytestmark = pytest.mark.unit
REPO = "o/r"


def check(
    name: str,
    run: int,
    job: int,
    status: str = "completed",
    conclusion: str | None = "success",
    *,
    app: str = "github-actions",
) -> dict[str, Any]:  # noqa: E501
    url = f"https://github.com/o/r/actions/runs/{run}/job/{job}"
    return {"id": job, "name": name, "status": status, "conclusion": conclusion, "app": {"slug": app},
            "details_url": url, "html_url": url}  # fmt: skip


class Runs:
    """The workflow runs GitHub knows: id -> (name, run number, status); an id that is absent answers 404."""

    def __init__(self, runs: Mapping[int, tuple[str, int, str]]) -> None:
        self.runs, self.asked = dict(runs), []

    def request(self, method: str, path: str, *, params: Any = None, body: Any = None) -> Response:
        assert method == "GET"
        run = int(path.rsplit("/", 1)[1])
        self.asked.append(run)
        if run not in self.runs:
            return Response(404, {"message": "Not Found"}, {})
        name, number, status = self.runs[run]
        return Response(200, {"name": name, "run_number": number, "status": status}, {})


def read(runs: Runs, checks: list[dict[str, Any]], workflows: tuple[str, ...] = ("CI",)) -> tuple[CheapJob, ...]:
    return cheap_jobs.read_cheap_jobs(runs, REPO, checks, workflows)  # type: ignore[arg-type]


def test_the_jobs_of_a_listed_workflow_are_read_with_their_state_link_and_ids() -> None:
    runs = Runs({10: ("CI", 1, "completed")})
    jobs = read(
        runs,
        [
            check("unit", 10, 101, conclusion="failure"),
            check("lint", 10, 102),
            check("build", 10, 103, "in_progress", None),
        ],
    )  # noqa: E501
    assert [(j.name, j.state, j.running, j.job_id, j.run_id, j.run_done) for j in jobs] == [
        ("unit", "failed", False, 101, 10, True),
        ("build", "active", True, 103, 10, True),
        ("lint", "passed", False, 102, 10, True),
    ], "failed first, then running, then passed"
    assert jobs[0].url == "https://github.com/o/r/actions/runs/10/job/101" and jobs[0].workflow == "CI"


@pytest.mark.parametrize(
    ("status", "conclusion", "state"),
    [
        ("completed", "success", "passed"),
        ("completed", "failure", "failed"),
        ("completed", "timed_out", "failed"),
        ("completed", "startup_failure", "failed"),
        ("completed", "action_required", "failed"),
        ("completed", "cancelled", "cancelled"),
        ("completed", "something-new", "failed"),
        ("queued", None, "active"),
        ("in_progress", None, "active"),
        ("completed", "skipped", None),
        ("completed", "neutral", None),
        ("completed", None, None),
    ],
)
def test_each_way_a_check_run_ends_is_one_state_and_a_skipped_job_says_nothing(
    status: str, conclusion: str | None, state: str | None
) -> None:
    jobs = read(Runs({10: ("CI", 1, "completed")}), [check("x", 10, 1, status, conclusion)])
    assert [j.state for j in jobs] == ([state] if state else [])


def test_a_queued_job_waits_and_a_running_one_runs() -> None:
    jobs = read(
        Runs({10: ("CI", 1, "in_progress")}),
        [check("a", 10, 1, "queued", None), check("b", 10, 2, "in_progress", None)],
    )  # noqa: E501
    assert {j.name: j.running for j in jobs} == {"a": False, "b": True}


def test_only_the_listed_workflows_and_only_github_actions_checks_count() -> None:
    runs = Runs({10: ("CI", 1, "completed"), 11: ("Other", 1, "completed")})
    checks = [check("unit", 10, 1), check("analyze", 11, 2), check("fake", 10, 3, app="someone-else")]
    assert [j.name for j in read(runs, checks)] == ["unit"]
    assert [j.name for j in read(runs, checks, ("CI", "Other"))] == ["unit", "analyze"], "by workflow, then name"


def test_a_workflow_that_is_not_listed_is_never_asked_about_when_nothing_is_listed() -> None:
    runs = Runs({10: ("CI", 1, "completed")})
    assert read(runs, [check("unit", 10, 1)], ()) == () and runs.asked == []


def test_each_run_is_looked_up_once_and_only_the_newest_run_of_a_workflow_counts() -> None:
    runs = Runs({10: ("CI", 1, "completed"), 20: ("CI", 2, "in_progress")})
    jobs = read(
        runs,
        [check("unit", 10, 1, conclusion="failure"), check("lint", 10, 2), check("unit", 20, 3, "in_progress", None)],
    )  # noqa: E501
    assert [(j.name, j.run_id, j.state) for j in jobs] == [("unit", 20, "active")]
    assert sorted(runs.asked) == [10, 20]


def test_a_run_that_cannot_be_read_is_left_out_and_the_others_still_show() -> None:
    runs = Runs({10: ("CI", 1, "completed")})  # run 99 answers 404
    jobs = read(runs, [check("unit", 10, 1), check("ghost", 99, 2)])
    assert [j.name for j in jobs] == ["unit"]

    class Broken:
        def request(self, *a: Any, **k: Any) -> Response:
            raise GitHubError(502, "down")

    assert cheap_jobs.read_cheap_jobs(Broken(), REPO, [check("unit", 10, 1)], ("CI",)) == ()  # type: ignore[arg-type]


def test_a_check_with_no_run_in_its_link_or_no_id_is_ignored() -> None:
    odd = {
        "id": 5,
        "name": "x",
        "status": "completed",
        "conclusion": "success",
        "app": {"slug": "github-actions"},
        "details_url": "https://example.com/x",
    }  # noqa: E501
    noid = {**check("y", 10, 1), "id": "not a number"}
    assert read(Runs({10: ("CI", 1, "completed")}), [odd, noid]) == ()


@pytest.mark.parametrize(
    ("state", "done", "job_id", "again"),
    [("failed", True, 1, True), ("cancelled", True, 1, True), ("failed", False, 1, False), ("passed", True, 1, False),
     ("active", True, 1, False), ("failed", True, 0, False)],
)  # fmt: skip
def test_only_a_failed_or_cancelled_job_of_a_finished_run_can_be_run_again(
    state: str, done: bool, job_id: int, again: bool
) -> None:
    assert CheapJob("x", state, job_id=job_id, run_done=done).can_run_again is again


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ()),
        ("   ", ()),
        ("[]", ()),
        ('["CI"]', ("CI",)),
        ('["CI", "Autopilot demo", "CI"]', ("CI", "Autopilot demo")),
    ],
)
def test_the_workflows_come_from_a_json_list_of_names(text: str, expected: tuple[str, ...]) -> None:
    assert cheap_jobs.parse_workflows(text) == expected


@pytest.mark.parametrize(
    "text", ["CI", "{}", '"CI"', "[1]", '[""]', '["  "]', '["a\\nb"]', f'["{"x" * 101}"]', "[", '[["CI"]]']
)
def test_a_list_that_is_not_names_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        cheap_jobs.parse_workflows(text)


# the status block --------------------------------------------------------------------------------------------------

HEAD = "a" * 40


def rows(*jobs: CheapJob, closed: str = "") -> str:
    status = manual_run.PrStatus("waiting", cheap=tuple(jobs), closed=closed)
    return "\n".join(manual_run.status_block(HEAD, status))


def test_the_jobs_are_listed_in_a_collapsed_section_inside_the_status_markers() -> None:
    text = rows(
        CheapJob("unit", "failed", "https://x/u", 1, 10, True),
        CheapJob("build", "active", "https://x/b", 2, 10, False, True),
        CheapJob("lint", "passed", "https://x/l", 3, 10, True),
    )
    assert text.startswith(manual_run.STATUS_START) and text.endswith(manual_run.STATUS_END)
    assert "<details><summary><b>🧰 Checks</b> · 1 failed · 1 running · 1 passed (3 jobs)</summary>" in text
    assert text.index("[!TIP]") < text.index("🧰 Checks") < text.index(manual_run.STATUS_END)
    assert "**unit** · [failed](https://x/u) · comment `/test unit` to run it again" in text
    assert "**build** · [running](https://x/b)" in text and "`/test build`" not in text, (
        "a running job cannot be re-run"
    )
    assert "**lint** · [passed](https://x/l)" in text and "`/test lint`" not in text


def test_a_failed_job_of_a_run_that_goes_does_not_offer_a_re_run_and_a_queued_one_says_waiting() -> None:
    text = rows(CheapJob("unit", "failed", "", 1, 10, False), CheapJob("q", "active", "", 2, 10, False, False))
    assert "**unit** · failed" in text and "to run it again" not in text
    assert "**q** · waiting" in text


def test_no_jobs_no_section_and_a_finished_pull_request_has_none() -> None:
    assert "🧰" not in rows()
    assert "🧰" not in rows(CheapJob("unit", "failed", "", 1, 10, True), closed="merged")


def test_a_long_list_is_cut_and_says_how_many_more() -> None:
    jobs = [CheapJob(f"j{i:02d}", "passed", "", i + 1, 10, True) for i in range(manual_run.CHEAP_SHOWN + 5)]
    text = rows(*jobs)
    assert f"({len(jobs)} jobs)" in text and "- and 5 more" in text and f"**j{manual_run.CHEAP_SHOWN:02d}**" not in text


def test_both_commands_give_the_listed_workflows_to_the_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def fake_status(*_a: Any, **kw: Any) -> str:
        seen["status"] = kw["cheap_workflows"]
        return "unchanged"

    def fake_refresh(*_a: Any, **kw: Any) -> str:
        seen["comment"] = kw["cheap_workflows"]
        return "unchanged"

    monkeypatch.setattr(manual_run, "refresh_status", fake_status)
    monkeypatch.setattr(manual_run, "refresh", fake_refresh)
    monkeypatch.setattr(cli, "build_client", lambda **_: object())
    monkeypatch.setattr(cli, "build_edit_client", lambda **_: None)
    monkeypatch.setattr(cli, "build_org_client", lambda **_: None)
    monkeypatch.setattr(cli, "load_policy", lambda _p: object())
    policy = tmp_path / "policy.yml"
    policy.write_text("", encoding="utf-8")
    listed = '["CI", "Lint"]'
    assert cli.main(["manual-status", "--repo", REPO, "--number", "7", "--cheap-workflows", listed]) == 0
    assert seen["status"] == ("CI", "Lint")
    argv = ["manual-comment", "--policy", str(policy), "--repo", REPO, "--number", "7"]
    assert cli.main([*argv, "--cheap-workflows", listed]) == 0
    assert seen["comment"] == ("CI", "Lint")
    assert cli.main(["manual-status", "--repo", REPO, "--number", "7"]) == 0
    assert seen["status"] == ()  # the default: nothing listed, nothing changes


def test_a_bad_list_of_workflows_fails_the_command_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "build_client", lambda **_: object())
    monkeypatch.setattr(cli, "build_edit_client", lambda **_: None)
    assert cli.main(["manual-status", "--repo", REPO, "--number", "7", "--cheap-workflows", "CI"]) == 3
    assert "JSON list of names" in capsys.readouterr().err


ROOT = Path(__file__).resolve().parents[2]


def test_the_actions_and_the_workflows_carry_the_list_down_to_the_commands() -> None:
    for name in ("manual-comment", "manual-status"):
        path = ROOT / ".github" / "actions" / name / "action.yml"
        action = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert action["inputs"]["cheap-workflows"]["default"] == "[]"
        text = path.read_text(encoding="utf-8")
        assert "CHEAP_WORKFLOWS: ${{ inputs.cheap-workflows }}" in text  # through the environment, never the script
        assert '"--cheap-workflows=$CHEAP_WORKFLOWS"' in text
    for file in ("e2e-pr-decide.yml", "e2e-pr-finish.yml", "e2e-pr-comment.yml"):
        path = ROOT / ".github" / "workflows" / file
        wf = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert wf[True]["workflow_call"]["inputs"]["cheap-workflows"]["default"] == "[]"
        steps = [s for job in wf["jobs"].values() for s in job.get("steps", [])]
        calls = [s for s in steps if str(s.get("uses", "")).endswith(("/manual-comment", "/manual-status"))]
        assert calls
        for call in calls:
            assert call["with"]["cheap-workflows"] == "${{ inputs.cheap-workflows }}", (file, call["uses"])
