"""The Jira reference of a pull request as a line of the controller (from Prow's label, or from the title)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from osac_ci import autopilot_summary, cli, jira_reference
from osac_ci.github.api import Response
from osac_ci.policy import PolicyError, parse_policy

pytestmark = pytest.mark.unit
HEAD = "a" * 40
JOBS = "jobs: {a: {kind: cheap, check: x}}\n"
LABEL = parse_policy('version: 1\nrepo: o/r\njira_reference: {check: "CI Autopilot / Jira reference"}\n' + JOBS)
TITLE = parse_policy("version: 1\nrepo: o/r\njira_reference: {check: J, mode: title}\n" + JOBS)
NONE = parse_policy("version: 1\nrepo: o/r\n" + JOBS)


class Gh:
    def __init__(self, title: str = "NO-ISSUE: x", labels: tuple[str, ...] = (), state: str = "open") -> None:
        self.title, self.labels, self.state = title, labels, state
        self.statuses: list[dict[str, Any]] = []
        self.posted: list[dict[str, Any]] = []

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path == "/repos/o/r/pulls/7":
            pull = {"state": self.state, "title": self.title, "head": {"sha": HEAD}}
            return Response(200, {**pull, "labels": [{"name": n} for n in self.labels]}, {})
        if method == "GET" and path.endswith("/statuses"):
            return Response(200, self.statuses, {})
        if method == "POST" and path.endswith("/statuses/" + HEAD):
            self.posted.append(body)
            self.statuses.insert(0, {**body, "creator": {"login": "osac-ci[bot]"}})
            return Response(201, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")


def run(gh: Gh, policy: Any = LABEL) -> str:
    return jira_reference.update(gh, "o/r", 7, policy)  # type: ignore[arg-type]


def test_the_line_is_green_when_prows_label_is_on_the_pull_request_and_red_when_it_is_not() -> None:
    assert run(Gh(labels=("jira/valid-reference", "other"))) == "CI Autopilot / Jira reference: success"
    gh = Gh(labels=("other",))
    assert run(gh) == "CI Autopilot / Jira reference: failure"
    assert "No valid Jira reference" in gh.posted[0]["description"] and gh.posted[0]["context"].startswith(
        "CI Autopilot"
    )


@pytest.mark.parametrize(
    ("title", "ok"),
    [
        ("NO-ISSUE: x", True),
        ("OSAC-1234: x", True),
        ("OSAC-1234 x", False),
        ("no-issue: x", False),
        ("fix a thing", False),
    ],
)
def test_without_prow_the_title_decides(title: str, ok: bool) -> None:
    assert run(Gh(title=title, labels=()), TITLE).endswith("success" if ok else "failure")


def test_the_line_is_written_again_only_when_its_answer_changed() -> None:
    gh = Gh(labels=())
    run(gh)
    assert run(gh).endswith("unchanged (failure)") and len(gh.posted) == 1
    gh.labels = ("jira/valid-reference",)
    assert run(gh).endswith("success") and len(gh.posted) == 2


def test_nothing_is_written_for_a_closed_pull_request_or_a_policy_without_the_key() -> None:
    assert "not open" in run(Gh(state="closed")) and "nothing to do" in run(Gh(), NONE)


def test_a_title_pattern_that_is_not_a_regular_expression_is_refused() -> None:
    with pytest.raises(PolicyError, match="regular expression"):
        parse_policy("version: 1\nrepo: o/r\njira_reference: {mode: title, title_pattern: '('}\n" + JOBS)


def test_the_command_writes_the_line(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = Gh(labels=("jira/valid-reference",))
    monkeypatch.setattr(cli, "build_client", lambda **_: gh)
    monkeypatch.setattr(cli, "load_policy", lambda _p: LABEL)
    assert cli.main(["jira-reference", "--policy", "p", "--repo", "o/r", "--number", "7"]) == 0
    assert "Jira reference: CI Autopilot / Jira reference: success" in capsys.readouterr().out


# in the CI Autopilot gate ------------------------------------------------------------------------------------------


class Checks:
    """Statuses of the lines only; no check runs."""

    def __init__(self, statuses: list[dict[str, Any]]) -> None:
        self.statuses = statuses
        self.written: list[Any] = []

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path == "/repos/o/r/pulls/7":
            return Response(200, {"state": "open", "head": {"sha": HEAD}}, {})
        if method == "GET" and path.endswith("/check-runs"):
            return Response(200, {"check_runs": []}, {})
        if method == "GET" and path.endswith("/statuses"):
            return Response(200, self.statuses, {})
        if method in ("POST", "PATCH"):
            self.written.append(body)
            return Response(201, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")


def status(state: str) -> dict[str, Any]:
    return {"context": "CI Autopilot / Jira reference", "state": state, "description": "x",
            "target_url": "", "creator": {"login": "osac-ci[bot]"}, "created_at": "2026-10-28T10:00:00Z"}  # fmt: skip


@pytest.mark.parametrize(
    ("statuses", "status_", "conclusion"),
    [
        ([status("failure")], "completed", "failure"),
        ([], "in_progress", None),
        ([status("success")], "completed", "success"),
    ],
)
def test_the_jira_line_is_part_of_the_gate(
    statuses: list[dict[str, Any]], status_: str, conclusion: str | None
) -> None:
    gh = Checks(statuses)
    only_jira = parse_policy('version: 1\nrepo: o/r\njira_reference: {check: "CI Autopilot / Jira reference"}\n' + JOBS)
    autopilot_summary.update(gh, "o/r", 7, only_jira, "x", "", datetime(2026, 10, 28, tzinfo=UTC))  # type: ignore[arg-type]
    body = gh.written[0]
    assert body["status"] == status_ and body.get("conclusion") == conclusion
