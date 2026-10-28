"""The CI Autopilot check of a merge queue entry: in progress when the jobs are chosen, settled by the first failure or
by every job passing, closed as cancelled when a newer entry replaces it."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from osac_ci import cli, queue_gate
from osac_ci.github.api import Response
from osac_ci.policy import parse_policy

pytestmark = pytest.mark.unit
SHA = "c" * 40
RUN = 77


class Gh:
    """The jobs of the entry's run (name -> (status, conclusion)) and the checks of the commit."""

    def __init__(self, jobs: dict[str, tuple[str, str | None]]) -> None:
        self.jobs = jobs
        self.checks: list[dict[str, Any]] = []
        self.writes: list[tuple[str, Any]] = []

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path.endswith(f"/actions/runs/{RUN}/jobs"):
            listed = [{"id": i + 1, "name": n, "status": s, "conclusion": c, "started_at": f"2026-10-28T10:0{i}:00Z"}
                      for i, (n, (s, c)) in enumerate(self.jobs.items())]  # fmt: skip
            return Response(200, {"jobs": listed}, {})
        if method == "GET" and path.endswith("/check-runs"):
            return Response(200, {"check_runs": self.checks}, {})
        if method in ("POST", "PATCH") and "/check-runs" in path:
            self.writes.append((method, body))
            if method == "POST":
                self.checks.append({**body, "id": len(self.checks) + 1, "app": {"slug": "osac-ci"}})
            else:
                self.checks[-1].update({k: v for k, v in body.items()})
            return Response(200, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")

    def gate(self) -> dict[str, Any]:
        return self.checks[-1]


def write(gh: Gh, result: str, detail: str = "", jobs: list[str] | None = None) -> str:
    return queue_gate.write(gh, "o/r", SHA, RUN, jobs or ["lint", "alpha"], result, detail, "https://x/run")  # type: ignore[arg-type]


JOBS = {"lint": ("completed", "success"), "alpha": ("in_progress", None)}


def test_the_gate_of_an_entry_starts_in_progress_with_the_jobs_the_plan_chose() -> None:
    gh = Gh(JOBS)
    assert write(gh, "start") == "created"
    gate = gh.gate()
    assert gate["status"] == "in_progress" and "conclusion" not in gate
    assert gate["name"] == "CI Autopilot" and "lint, alpha" in gate["output"]["title"]
    assert (
        "| ✅ | lint | passed |" in gate["output"]["summary"]
        and "| ⏳ | alpha | running |" in gate["output"]["summary"]
    )
    assert gate["details_url"] == "https://x/run"


def test_every_job_passing_settles_it_as_success_and_the_events_tell_the_story() -> None:
    gh = Gh(JOBS)
    write(gh, "start")
    gh.jobs["alpha"] = ("completed", "success")
    write(gh, "success")
    assert gh.gate()["conclusion"] == "success" and gh.gate()["status"] == "completed"
    events = gh.gate()["output"]["text"].splitlines()[2:]
    assert [e.split(" · ", 1)[1] for e in events] == ["queue entry passed", "queue entry: testing lint, alpha"]


def test_the_first_failure_names_what_failed() -> None:
    gh = Gh({"lint": ("completed", "failure"), "alpha": ("in_progress", None)})
    write(gh, "start")
    write(gh, "failure", "lint")
    assert gh.gate()["conclusion"] == "failure" and gh.gate()["output"]["title"] == "Failed: lint"
    assert "| ❌ | lint | failed |" in gh.gate()["output"]["summary"]


def test_an_entry_replaced_by_a_newer_one_is_closed_as_cancelled() -> None:
    gh = Gh({"lint": ("completed", "cancelled"), "alpha": ("completed", "cancelled")})
    write(gh, "cancelled", "a newer entry replaced it")
    assert gh.gate()["conclusion"] == "cancelled" and "Cancelled: a newer entry" in gh.gate()["output"]["title"]


def test_a_settled_gate_that_goes_again_is_a_new_run_and_an_open_one_is_updated() -> None:
    gh = Gh(JOBS)
    write(gh, "start")
    write(gh, "start")  # the same state again: updated, not piled up
    assert [w[0] for w in gh.writes] == ["POST", "PATCH"]
    write(gh, "failure", "alpha")
    write(gh, "start")  # settled, then going again
    assert [w[0] for w in gh.writes][-1] == "POST"


@pytest.mark.parametrize(("result", "jobs"), [("nope", ["lint"]), ("start", ["not an id"]), ("start", ["A"])])
def test_nothing_is_written_for_a_bad_request(result: str, jobs: list[str]) -> None:
    gh = Gh(JOBS)
    with pytest.raises(ValueError):
        write(gh, result, jobs=jobs)
    assert gh.writes == []


def test_the_command_writes_the_gate_and_reports_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = Gh(JOBS)
    monkeypatch.setattr(cli, "build_client", lambda **_: gh)
    argv = ["autopilot-queue-gate", "--repo", "o/r", "--sha", SHA, "--run-id", str(RUN), "--jobs", '["lint","alpha"]']
    assert cli.main([*argv, "--result", "start"]) == 0
    assert "CI Autopilot (queue entry): created" in capsys.readouterr().out
    assert cli.main([*argv, "--result", "start", "--jobs", "lint"]) == 3


def test_the_cheap_jobs_a_change_needs_are_listed_for_the_plan(
    tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = tmp_path / "p.yml"
    policy.write_text(
        "version: 1\nrepo: o/r\npath_filters: {mode: enforce, filters: {code: ['**/*.go']}}\njobs:\n"
        "  lint: {kind: cheap, check: L, workflow: ci-lint.yml}\n"
        "  unit: {kind: cheap, check: U, workflow: ci-unit.yml, filters_any: [code]}\n"
        "  other: {kind: cheap, check: O}\n"
    )
    files = tmp_path / "files"
    files.write_text("docs/a.md\n")
    assert cli.main(["e2e-suites", "--policy", str(policy), "--files-from", str(files), "--cheap"]) == 0
    assert capsys.readouterr().out.strip() == '["lint"]'
    files.write_text("main.go\n")
    assert cli.main(["e2e-suites", "--policy", str(policy), "--files-from", str(files), "--cheap"]) == 0
    assert capsys.readouterr().out.strip() == '["lint", "unit"]'
    assert parse_policy(policy.read_text())  # the policy is valid


def _result() -> dict[str, Any]:
    import yaml
    from helpers import ROOT

    return yaml.safe_load((ROOT / ".github" / "workflows" / "autopilot-queue-result.yml").read_text(encoding="utf-8"))


def test_the_result_job_writes_the_gate_before_it_stops_anything_and_closes_it_whatever_happened() -> None:
    steps = _result()["jobs"]["result"]["steps"]
    uses = [str(s.get("uses", "")).rsplit("/", 1)[-1] for s in steps]
    order = [
        s.get("with", {}).get("phase") or s.get("with", {}).get("result") or u for s, u in zip(steps, uses, strict=True)
    ]
    gate_end = next(
        i for i, s in enumerate(steps) if s.get("uses", "").endswith("autopilot-queue-gate") and s.get("if")
    )
    stop = next(i for i, s in enumerate(steps) if s.get("with", {}).get("phase") == "stop")
    assert gate_end < stop, f"the gate is written first, then what runs is stopped: {order}"
    assert steps[gate_end]["if"] == "always()" and steps[gate_end]["with"]["result"].endswith("|| 'cancelled' }}")
    first = next(s for s in steps if s.get("uses", "").endswith("autopilot-queue-gate"))
    assert first["with"]["result"] == "start" and first["continue-on-error"] is True, (
        "opening the gate must not stop the watch"
    )


def test_the_app_token_of_the_result_job_may_only_write_checks_and_read_actions() -> None:
    for step in _result()["jobs"]["result"]["steps"]:
        if str(step.get("uses", "")).startswith("actions/create-github-app-token"):
            granted = {k: v for k, v in step["with"].items() if k.startswith("permission-")}
            assert granted == {"permission-checks": "write", "permission-actions": "read"}
        assert "${{" not in step.get("run", "")
