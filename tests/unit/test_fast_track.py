"""A fast track: a person of the fast-track team skips every test of one pull request. The E2E check of the commit says
who and why, the CI workflow is stopped and run again, and the merge queue entry of that pull request is not tested."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from osac_ci import cli, e2e_check, fast_mark, fast_track
from osac_ci.e2e_check import Write
from osac_ci.github.api import Response
from osac_ci.policy import PolicyError, parse_policy

pytestmark = pytest.mark.unit
HEAD = "a" * 40
OTHER = "b" * 40
REASON = "Production is down, the fix was reviewed in chat"
POLICY = parse_policy("""
version: 1
repo: o/r
fast_track: {team: ci-duty}
jobs:
  vmaas: {kind: e2e, check: "E2E / VMaaS", suite: vmaas}
""")


class Gh:
    """One pull request (#7), the members of the team, check runs, and the workflow runs of the commit."""

    def __init__(self, author: str = "dev", files: tuple[str, ...] = ("src/a.go",), state: str = "open") -> None:
        self.author, self.files, self.state, self.head = author, files, state, HEAD
        self.team = {"duty-person"}
        self.checks: list[dict[str, Any]] = []
        self.runs: list[dict[str, Any]] = []  # suites: workflow_dispatch runs
        self.ci_runs: list[dict[str, Any]] = []  # the CI workflow runs of the commit
        self.cancelled: list[int] = []
        self.rerun: list[int] = []
        self.cancel_status = 202
        self.rerun_status = 201
        self.ends_when_cancelled = True

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path == "/repos/o/r/pulls/7":
            return Response(200, {"state": self.state, "head": {"sha": self.head}, "user": {"login": self.author}}, {})
        if method == "GET" and path == "/repos/o/r/pulls/7/files":
            return Response(200, [{"filename": f} for f in self.files], {})
        if method == "GET" and path.startswith("/orgs/o/teams/ci-duty/memberships/"):
            return (
                Response(200, {"state": "active"}, {}) if path.rsplit("/", 1)[1] in self.team else Response(404, {}, {})
            )
        if method == "GET" and path.endswith("/check-runs"):
            sha = path.split("/commits/")[1].split("/")[0]
            name = (params or {}).get("check_name")
            return Response(
                200, {"check_runs": [c for c in self.checks if c["head_sha"] == sha and name in (None, c["name"])]}, {}
            )
        if method == "POST" and path == "/repos/o/r/check-runs":
            self.checks.append({**body, "id": len(self.checks) + 1, "app": {"slug": "osac-ci"}})
            return Response(201, {}, {})
        if method == "GET" and path == "/repos/o/r/actions/runs":
            status = (params or {}).get("status")
            return Response(200, {"workflow_runs": [r for r in self.runs if r["status"] == status]}, {})
        if method == "GET" and path == "/repos/o/r/actions/workflows/ci.yml/runs":
            return Response(200, {"workflow_runs": self.ci_runs}, {})
        if method == "GET" and path.startswith("/repos/o/r/actions/runs/"):
            rid = int(path.rsplit("/", 1)[1])
            return Response(200, next(r for r in [*self.runs, *self.ci_runs] if r["id"] == rid), {})
        if method == "POST" and path.endswith("/cancel"):
            rid = int(path.split("/")[-2])
            self.cancelled.append(rid)
            if self.cancel_status == 202 and self.ends_when_cancelled:
                for r in [*self.runs, *self.ci_runs]:
                    if r["id"] == rid:
                        r["status"] = "completed"
            return Response(self.cancel_status, {}, {})
        if method == "POST" and path.endswith("/rerun"):
            self.rerun.append(int(path.split("/")[-2]))
            return Response(self.rerun_status, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")

    def line(self, name: str, sha: str = HEAD, app: str = "osac-ci") -> None:
        self.checks.append(
            {"id": len(self.checks) + 1, "name": name, "status": "completed", "conclusion": "failure",
             "app": {"slug": app}, "head_sha": sha, "output": {"title": "Failed", "text": ""}}
        )  # fmt: skip


def go(gh: Gh, actor: str = "duty-person", reason: str = REASON) -> str:
    return fast_track.apply(gh, "o/r", 7, actor, reason, POLICY, "https://x/run", lambda _s: None)  # type: ignore[arg-type]


def e2e(gh: Gh, sha: str = HEAD) -> dict[str, Any]:
    return [c for c in gh.checks if c["name"] == "E2E" and c["head_sha"] == sha][-1]


# who may ------------------------------------------------------------------------------------------------------------


def refused(**changes: Any) -> str | None:
    base: dict[str, Any] = {
        "actor": "duty-person", "in_team": True, "author": "dev", "files": ["src/a.go"], "reason": REASON,
        "state": "open", "team": "ci-duty", "allow_author": False, "protected": [".github/**", "CODEOWNERS"],
    }  # fmt: skip
    return fast_track.refusal(**{**base, **changes})


def test_a_member_of_the_team_may_fast_track_an_ordinary_change() -> None:
    assert refused() is None


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"state": "closed"}, "not open"),
        ({"reason": "no"}, "say why"),
        ({"in_team": False}, "not in the ci-duty team"),
        ({"author": "duty-person"}, "own pull request"),
        ({"files": ["src/a.go", ".github/workflows/ci.yml"]}, ".github/workflows/ci.yml"),
        ({"files": ["CODEOWNERS"]}, "CODEOWNERS"),
    ],
)
def test_the_refusals(changes: dict[str, Any], words: str) -> None:
    assert words in (refused(**changes) or "")


def test_the_author_may_when_the_policy_allows_it_and_a_long_list_of_files_is_cut() -> None:
    assert refused(author="duty-person", allow_author=True) is None
    assert "and 3 more" in (refused(files=[f".github/f{i}" for i in range(6)]) or "")


# the E2E check ------------------------------------------------------------------------------------------------------


def test_a_fast_track_passes_the_e2e_check_and_says_who_and_why() -> None:
    gh = Gh()
    said = go(gh)
    assert said == f"Fast track: skipped by duty-person: {REASON} (commit aaaaaaa)"
    check = e2e(gh)
    assert (check["status"], check["conclusion"]) == ("completed", "success")
    assert check["output"]["title"] == f"Fast track: skipped by duty-person: {REASON}"
    assert "> **Fast track** by @duty-person" in check["output"]["summary"]
    assert fast_mark.decode(check["output"]["text"], HEAD) == fast_mark.FastTrack(HEAD, "duty-person", REASON)
    assert e2e_check.fast_track_on(gh, "o/r", HEAD) is not None  # type: ignore[arg-type]
    assert e2e_check.fast_track_on(gh, "o/r", OTHER) is None  # type: ignore[arg-type]


def test_nothing_written_afterwards_replaces_the_fast_track() -> None:
    gh = Gh()
    go(gh)
    for state in ("pending", "success", "failure", "error"):
        e2e_check.write_status(gh, "o/r", HEAD, Write(state, "Cancelled"), "https://x/run", "checks")  # type: ignore[arg-type]
    assert len([c for c in gh.checks if c["name"] == "E2E"]) == 1, "the suites it stopped end as Cancelled: not written"
    e2e_check.write_status(gh, "o/r", OTHER, Write("success", "Passed: x"), "", "checks")  # type: ignore[arg-type]
    assert len([c for c in gh.checks if c["name"] == "E2E"]) == 2, "another commit is written as always"


def test_a_check_of_another_app_is_no_fast_track() -> None:
    gh = Gh()
    go(gh)
    e2e(gh)["app"] = {"slug": "github-actions"}
    assert e2e_check.fast_track_on(gh, "o/r", HEAD) is None  # type: ignore[arg-type]


def test_a_new_commit_has_no_fast_track() -> None:
    gh = Gh()
    go(gh)
    gh.head = OTHER
    assert fast_track.current(gh, "o/r", 7) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("make", "words"),
    [
        (lambda: (Gh(), "somebody", REASON), "not in the ci-duty team"),
        (lambda: (Gh(author="duty-person"), "duty-person", REASON), "own pull request"),
        (lambda: (Gh(files=(".github/workflows/x.yml",)), "duty-person", REASON), "never fast-tracked"),
        (lambda: (Gh(state="closed"), "duty-person", REASON), "not open"),
        (lambda: (Gh(), "duty-person", "ok"), "say why"),
        (lambda: (Gh(), "not a login!", REASON), "not a login"),
    ],
)
def test_a_refused_fast_track_writes_and_stops_nothing(make: Any, words: str) -> None:
    gh, actor, reason = make()
    gh.runs.append({"id": 11, "status": "in_progress", "display_title": f"E2E suite · vmaas · #7 · {HEAD}"})
    with pytest.raises(ValueError, match=words):
        go(gh, actor, reason)
    assert gh.checks == [] and gh.cancelled == [] and gh.rerun == []


def test_a_policy_without_the_key_lets_nobody_fast_track() -> None:
    plain = parse_policy("version: 1\nrepo: o/r\njobs: {a: {kind: cheap, check: x}}\n")
    with pytest.raises(ValueError, match="nobody may"):
        fast_track.apply(Gh(), "o/r", 7, "duty-person", REASON, plain)  # type: ignore[arg-type]


def test_the_policy_key_is_checked_when_it_loads() -> None:
    jobs = "jobs: {a: {kind: cheap, check: x}}\n"
    for bad in (
        "{team: 'a b'}",
        "{team: t, other: 1}",
        "{team: t, ci_workflow: 'ci'}",
        "{team: t, ci_workflow: '../x.yml'}",
    ):
        with pytest.raises(PolicyError):
            parse_policy(f"version: 1\nrepo: o/r\nfast_track: {bad}\n" + jobs)
    rule = parse_policy("version: 1\nrepo: o/r\nfast_track: {team: t}\n" + jobs).fast_track
    assert rule is not None and rule.protected_paths[0] == ".github/**" and rule.ci_workflow == "ci.yml"


# what still runs ----------------------------------------------------------------------------------------------------


def test_the_suites_that_run_are_cancelled_and_the_check_says_so() -> None:
    gh = Gh()
    gh.runs += [
        {"id": 11, "status": "in_progress", "display_title": f"E2E suite · vmaas · #7 · {HEAD}"},
        {"id": 12, "status": "queued", "display_title": f"E2E suite · caas · #7 · {HEAD}"},
        {"id": 21, "status": "in_progress", "display_title": f"E2E suite · vmaas · #7 · {OTHER}"},  # an older commit
        {
            "id": 22,
            "status": "in_progress",
            "display_title": f"E2E suite · vmaas · #8 · {HEAD}",
        },  # another pull request
        {"id": 23, "status": "in_progress", "display_title": "Some other workflow"},
    ]
    said = go(gh)
    assert sorted(gh.cancelled) == [11, 12] and "stopped caas, vmaas" in said
    assert "Stopped caas, vmaas." in e2e(gh)["output"]["summary"]


def test_the_ci_workflow_of_the_commit_is_run_again_after_the_check_is_written() -> None:
    gh = Gh()
    gh.ci_runs.append({"id": 31, "status": "completed", "conclusion": "failure"})
    said = go(gh)
    assert gh.rerun == [31] and gh.cancelled == [] and "ran ci.yml again" in said


def test_a_ci_run_that_still_goes_is_cancelled_first_and_the_newest_run_is_the_one_run_again() -> None:
    gh = Gh()
    gh.ci_runs += [{"id": 31, "status": "completed"}, {"id": 32, "status": "in_progress"}]
    assert "ran ci.yml again" in go(gh)
    assert gh.cancelled == [32] and gh.rerun == [32]


def test_a_ci_run_that_does_not_end_is_named_and_the_fast_track_stands() -> None:
    gh = Gh()
    gh.ci_runs.append({"id": 32, "status": "in_progress"})
    gh.ends_when_cancelled = False
    said = fast_track.apply(
        gh,
        "o/r",
        7,
        "duty-person",
        REASON,
        POLICY,
        "",
        lambda _s: None,
    )  # type: ignore[arg-type]
    assert "could not run ci.yml again" in said and gh.rerun == []
    assert e2e(gh)["conclusion"] == "success"


def test_a_workflow_with_no_run_for_the_commit_is_left_alone() -> None:
    gh = Gh()
    assert "ci.yml" not in go(gh) and gh.rerun == []


def test_a_run_that_cannot_be_cancelled_or_run_again_is_named_and_does_not_undo_the_fast_track() -> None:
    gh = Gh()
    gh.runs.append({"id": 11, "status": "in_progress", "display_title": f"E2E suite · vmaas · #7 · {HEAD}"})
    gh.ci_runs.append({"id": 31, "status": "completed"})
    gh.cancel_status, gh.rerun_status = 403, 403
    said = go(gh)
    assert "could not stop vmaas" in said and "could not run ci.yml again" in said
    assert e2e(gh)["conclusion"] == "success"


# the decision, the merge queue and the commands ---------------------------------------------------------------------


def queue(gh: Gh, ref: str = f"gh-readonly-queue/main/pr-7-{'c' * 40}") -> fast_mark.FastTrack | None:
    return fast_track.for_queue(gh, "o/r", ref)  # type: ignore[arg-type]


def test_a_queue_entry_of_a_fast_tracked_pull_request_is_fast_tracked() -> None:
    gh = Gh()
    assert queue(gh) is None, "no check at all"
    go(gh)
    assert queue(gh) == fast_mark.FastTrack(HEAD, "duty-person", REASON)
    assert queue(gh, f"refs/heads/gh-readonly-queue/main/pr-7-{'c' * 40}") is not None
    assert fast_track.outputs(queue(gh)) == {"fast": True, "login": "duty-person", "reason": REASON}
    assert fast_track.outputs(None)["fast"] is False


def test_a_queue_entry_is_tested_when_the_fast_track_does_not_hold() -> None:
    gh = Gh()
    go(gh)
    assert queue(gh, "main") is None and queue(gh, "gh-readonly-queue/main/pr-x-" + "c" * 40) is None
    gh.head = OTHER
    assert queue(gh) is None, "the pull request moved on: the fast track was for the older commit"
    gh.head, gh.state = HEAD, "closed"
    assert queue(gh) is None


def test_the_e2e_check_of_a_fast_tracked_entry_passes_and_says_why() -> None:
    gh = Gh()
    fast_track.entry_check(gh, "o/r", "c" * 40, f"duty-person: {REASON}", "https://x/run")  # type: ignore[arg-type]
    check = e2e(gh, "c" * 40)
    assert (check["status"], check["conclusion"]) == ("completed", "success")
    assert check["output"]["title"] == f"Fast track: skipped by duty-person: {REASON}"


def test_the_commands(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = Gh()
    monkeypatch.setattr(cli, "build_client", lambda **_: gh)
    monkeypatch.setattr(cli, "load_policy", lambda _p: POLICY)
    args = ["fast-track", "--policy", "p", "--repo", "o/r", "--number", "7", "--reason", REASON]
    assert cli.main([*args, "--actor", "somebody"]) == 3
    assert "not in the ci-duty team" in capsys.readouterr().err and gh.checks == []
    assert cli.main([*args, "--actor", "duty-person"]) == 0
    assert "Fast track: Fast track: skipped by duty-person" in capsys.readouterr().out
    assert cli.main(["fast-track-check", "--repo", "o/r", "--number", "7"]) == 0
    assert '"fast": true' in capsys.readouterr().out
    assert cli.main(["fast-track-check", "--repo", "o/r", "--head-ref", f"gh-readonly-queue/main/pr-7-{'c' * 40}"]) == 0
    assert '"login": "duty-person"' in capsys.readouterr().out
    assert cli.main(["fast-track-entry", "--repo", "o/r", "--sha", "c" * 40, "--detail", "duty-person: x y z w"]) == 0
    assert "Fast track: skipped by duty-person: x y z w" in capsys.readouterr().out
