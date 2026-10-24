"""Starting suites as runs of their own: what is asked of GitHub, what is written on the pull request, and what is
refused before anything is."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from osac_ci import cli, e2e_dispatch
from osac_ci.github.api import Response

pytestmark = pytest.mark.unit
HEAD = "a" * 40
VM = "E2E / VMaaS Full Install"
CA = "E2E / CaaS Full Install"


class Gh:
    """A repository: its default branch, the workflows that can be dispatched (id of the run each starts, or the
    status GitHub answers with), and what is written."""

    def __init__(self, runs: Mapping[str, int | Response] | None = None, branch: str = "main") -> None:
        self.runs = dict(runs or {"e2e-vmaas-full-install.yml": 111, "e2e-caas-full-install.yml": 222})
        self.branch = branch
        self.dispatched: list[tuple[str, Any]] = []
        self.written: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path == "/repos/o/r":
            return Response(200, {"default_branch": self.branch}, {})
        if method == "POST" and "/dispatches" in path:
            file = path.split("/workflows/")[1].split("/")[0]
            self.dispatched.append((file, body))
            got = self.runs[file]
            if isinstance(got, Response):
                return got
            return Response(200, {"workflow_run_id": got, "run_url": "x", "html_url": "y"}, {})
        if method == "POST":
            self.written.append((method, path, body))
            return Response(201, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")


ITEMS = [
    {"suite": "vmaas", "workflow": "e2e-vmaas-full-install.yml", "check": VM, "tier": ""},
    {"suite": "caas", "workflow": "e2e-caas-full-install.yml", "check": CA, "tier": "full"},
]


def start(gh: Gh, items: list[dict[str, str]] = ITEMS, **given: Any) -> tuple[list[e2e_dispatch.Started], list[str]]:
    args: dict[str, Any] = {"sha": HEAD, "number": 7, "head_repo": "o/r", "lines_kind": "statuses"}
    args.update(given)
    return e2e_dispatch.dispatch_suites(gh, "o/r", items, **args)  # type: ignore[arg-type]


def test_each_suite_is_started_on_the_default_branch_with_its_inputs_and_the_run_id_asked_for() -> None:
    gh = Gh()
    started, failed = start(gh)
    assert failed == [] and [(s.suite, s.run) for s in started] == [("vmaas", 111), ("caas", 222)]
    file, body = gh.dispatched[0]
    assert file == "e2e-vmaas-full-install.yml"
    assert body == {
        "ref": "main",
        "inputs": {
            "repo": "o/r", "sha": HEAD, "pr-number": "7", "fork-pr-author": "", "fork-pr-author-association": "",
        },
        "return_run_details": True,
    }  # fmt: skip


def test_the_tier_is_passed_only_to_a_suite_that_was_asked_for_one() -> None:
    gh = Gh()
    start(gh)
    assert "tier" not in gh.dispatched[0][1]["inputs"] and gh.dispatched[1][1]["inputs"]["tier"] == "full"


def test_a_fork_pull_request_passes_its_author_and_association() -> None:
    gh = Gh()
    start(gh, head_repo="mallory/r", fork_author="mallory", fork_association="NONE")
    given = gh.dispatched[0][1]["inputs"]
    assert (given["repo"], given["fork-pr-author"], given["fork-pr-author-association"]) == (
        "mallory/r",
        "mallory",
        "NONE",
    )


def test_the_line_of_a_started_suite_waits_for_a_runner_and_links_to_its_own_run() -> None:
    gh = Gh()
    start(gh)
    first, second = gh.written
    assert first[1] == f"/repos/o/r/statuses/{HEAD}"
    assert first[2]["context"] == VM and first[2]["state"] == "pending" and first[2]["target_url"].endswith("/runs/111")
    assert first[2]["description"] == "Waiting for a runner"
    assert second[2]["description"] == "Waiting for a runner · full", "the version it was asked for"


@pytest.mark.parametrize(
    "answer", [Response(422, {"message": "Unexpected inputs provided"}, {}), Response(204, {}, {})]
)
def test_a_suite_that_could_not_be_started_gets_a_failed_line_and_the_others_still_start(answer: Response) -> None:
    gh = Gh({"e2e-vmaas-full-install.yml": answer, "e2e-caas-full-install.yml": 222})
    started, failed = start(gh)
    assert failed == ["vmaas"] and [s.suite for s in started] == ["caas"]
    line = gh.written[0][2]
    assert line["context"] == VM and line["state"] == "failure" and "could not be started" in line["description"]


@pytest.mark.parametrize(
    "given",
    [
        {"sha": "xyz"},
        {"sha": "A" * 40},
        {"number": 0},
        {"head_repo": "a/b/c"},
        {"fork_author": "a b"},
        {"fork_author": "x;id"},
        {"fork_association": "none"},
        {"fork_association": "A" * 40},
    ],
)
def test_a_value_that_is_not_what_it_should_be_is_refused_before_anything_is_asked(given: dict) -> None:
    gh = Gh()
    with pytest.raises(ValueError):
        start(gh, **given)
    assert gh.dispatched == [] and gh.written == []


@pytest.mark.parametrize(
    "change",
    [
        {"suite": "VMAAS"},
        {"suite": ""},
        {"workflow": "../x.yml"},
        {"workflow": "x.yml?ref=evil"},
        {"workflow": "x"},
        {"tier": "Full"},
        {"tier": "x" * 30},
        {"check": "E2E"},
    ],
)
def test_a_suite_that_is_not_what_it_should_be_is_refused_before_anything_is_asked(change: dict) -> None:
    gh = Gh({"x.yml": 1, "e2e-vmaas-full-install.yml": 1})
    with pytest.raises(ValueError):
        start(gh, [{**ITEMS[0], **change}])
    assert gh.dispatched == [] and gh.written == []


def test_a_default_branch_that_is_not_a_branch_name_is_refused() -> None:
    gh = Gh(branch="main; rm -rf")
    with pytest.raises(ValueError, match="default branch"):
        start(gh)
    assert gh.dispatched == []


def test_the_dispatch_uses_the_main_client_and_the_lines_the_one_given_for_them() -> None:
    main, lines = Gh(), Gh()
    start(main, lines_client=lines)
    assert len(main.dispatched) == 2 and main.written == []
    assert len(lines.written) == 2 and lines.dispatched == []


def test_a_run_is_named_in_a_way_that_reads_back_and_nothing_else_reads_as_a_suite_run() -> None:
    name = e2e_dispatch.run_name("vmaas", 7, HEAD)
    assert name == f"E2E suite · vmaas · #7 · {HEAD}"
    assert e2e_dispatch.read_run_name(name) == ("vmaas", 7, HEAD)
    for other in (
        "",
        "E2E #7 · branch",
        f"E2E suite · vmaas · #7 · {HEAD} extra",
        f"E2E suite · VMAAS · #7 · {HEAD}",
        f"E2E suite · vmaas · #x · {HEAD}",
        f"E2E suite · vmaas · #7 · {'A' * 40}",
        f"E2E suite · vmaas · #7 · {'a' * 39}",
        f"E2E suite · vmaas\n · #7 · {HEAD}",
    ):
        assert e2e_dispatch.read_run_name(other) is None, other


def test_the_command_writes_the_runs_started_and_fails_when_a_suite_could_not_be_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = Gh({"e2e-vmaas-full-install.yml": 111, "e2e-caas-full-install.yml": Response(404, {}, {})})
    monkeypatch.setattr(cli, "build_client", lambda *a, **k: gh)
    monkeypatch.setattr(cli, "build_lines_client", lambda: None)
    out = tmp_path / "runs.json"
    argv = ["e2e-dispatch", "--repo", "o/r", "--sha", HEAD, "--number", "7", "--head-repo", "o/r"]
    code = cli.main([*argv, "--dispatch", json.dumps(ITEMS), "--json-file", str(out)])
    assert code == 3
    assert json.loads(out.read_text()) == {"runs": {"vmaas": {"run": 111, "check": VM}}, "failed": ["caas"]}
    assert "Started vmaas as run 111" in capsys.readouterr().out


def test_the_command_refuses_what_is_not_a_json_list_of_objects(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["e2e-dispatch", "--repo", "o/r", "--sha", HEAD, "--number", "7", "--head-repo", "o/r"]
    assert cli.main([*argv, "--dispatch", "{}"]) == 3 and "JSON list" in capsys.readouterr().err


# the action and the decision workflow ----------------------------------------------------------------------------

import re  # noqa: E402

import yaml  # noqa: E402
from helpers import ROOT  # noqa: E402

ACTION = ROOT / ".github" / "actions" / "e2e-dispatch" / "action.yml"
DECIDE = ROOT / ".github" / "workflows" / "e2e-pr-decide.yml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_every_input_of_the_action_used_is_declared_and_every_input_declared_is_used() -> None:
    used = set(re.findall(r"\$\{\{\s*inputs\.([a-z-]+)\s*\}\}", ACTION.read_text(encoding="utf-8")))
    assert used == set(load(ACTION)["inputs"])


def test_nothing_is_expanded_into_the_script_and_nothing_is_checked_out() -> None:
    for step in load(ACTION)["runs"]["steps"]:
        assert "${{" not in step.get("run", "")
        assert "checkout" not in step.get("uses", "")


def test_the_action_refuses_a_number_or_commit_that_is_not_what_it_should_be_and_unsets_an_empty_lines_token() -> None:
    script = next(s["run"] for s in load(ACTION)["runs"]["steps"] if s.get("id") == "dispatch")
    assert '"" | *[!0-9]*)' in script and '"" | *[!0-9a-f]*)' in script and '"${#SHA}" -ne 40' in script
    assert 'if [ -z "$OSAC_CI_LINES_TOKEN" ]; then unset OSAC_CI_LINES_TOKEN; fi' in script
    assert 'osac-ci e2e-dispatch --repo "$REPO"' in script and "--json-file" in script


def test_the_decision_starts_them_after_the_code_is_found_and_with_the_workflows_own_token_for_the_dispatch() -> None:
    steps = load(DECIDE)["jobs"]["decide"]["steps"]
    ids = [s.get("id") for s in steps]
    assert ids.index("code") < ids.index("dispatch")
    step = steps[ids.index("dispatch")]
    assert step["uses"] == "$/.github/actions/e2e-dispatch"
    assert step["if"] == "steps.report.outputs.start == 'true' && steps.report.outputs.dispatched != '[]'"
    given = step["with"]
    assert given["github-token"] == "${{ github.token }}", "a workflow_dispatch is what the workflow's token may start"
    assert given["lines-token"] == "${{ steps.app-token.outputs.token }}"
    assert given["head-repo"] == "${{ steps.code.outputs.repo }}"
    assert given["dispatched"] == "${{ steps.report.outputs.dispatched }}"


def test_the_decision_hands_the_caller_the_suites_of_its_own_run_and_what_was_dispatched() -> None:
    wf = load(DECIDE)
    outputs = wf[True]["workflow_call"]["outputs"]
    for name in ("suites", "suite-checks", "dispatched", "dispatched-runs", "all-suite-checks"):
        assert name in outputs
    job = wf["jobs"]["decide"]["outputs"]
    assert job["suites"] == "${{ steps.report.outputs.suites }}", "the inline suites: the caller's jobs"
    assert job["dispatched-runs"] == "${{ steps.dispatch.outputs.runs || '{}' }}"
    assert job["all-suite-checks"] == "${{ steps.report.outputs.all-suite-checks }}"
