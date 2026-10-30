"""A fast track: a person of the fast-track team skips every test of one pull request, the gate of the commit says who
and why, and the merge queue entry of that pull request is not tested either."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from osac_ci import autopilot_summary, cli, fast_mark, fast_track, queue_gate
from osac_ci.github.api import Response
from osac_ci.policy import PolicyError, parse_policy

pytestmark = pytest.mark.unit
HEAD = "a" * 40
OTHER = "b" * 40
NOW = datetime(2026, 10, 30, 12, 0, 5, tzinfo=UTC)
REASON = "Production is down, the fix was reviewed in chat"
POLICY = parse_policy("""
version: 1
repo: o/r
fast_track: {team: ci-duty}
jobs:
  lint: {kind: cheap, check: "CI Autopilot / CI / Lint", workflow: ci-lint.yml}
""")


class Gh:
    """One pull request (#7), the members of the team, and the check runs of the commits."""

    def __init__(self, author: str = "dev", files: tuple[str, ...] = ("src/a.go",), state: str = "open") -> None:
        self.author, self.files, self.state, self.head = author, files, state, HEAD
        self.team = {"duty-person"}
        self.checks: list[dict[str, Any]] = []

    def line(self, name: str, conclusion: str, sha: str = HEAD) -> None:
        self.checks.append({"id": len(self.checks) + 1, "name": name, "status": "completed", "conclusion": conclusion,
                            "app": {"slug": "osac-ci"}, "head_sha": sha, "output": {"title": "x", "text": ""},
                            "started_at": "2026-10-30T11:00:00Z", "details_url": ""})  # fmt: skip

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path == "/repos/o/r/pulls/7":
            pull = {"state": self.state, "head": {"sha": self.head}, "user": {"login": self.author}}
            return Response(200, pull, {})
        if method == "GET" and path == "/repos/o/r/pulls/7/files":
            return Response(200, [{"filename": f} for f in self.files], {})
        if method == "GET" and path.startswith("/orgs/o/teams/ci-duty/memberships/"):
            login = path.rsplit("/", 1)[1]
            return Response(200, {"state": "active"}, {}) if login in self.team else Response(404, {}, {})
        if method == "GET" and path.endswith("/check-runs"):
            sha = path.split("/commits/")[1].split("/")[0]
            name = (params or {}).get("check_name")
            return Response(
                200, {"check_runs": [c for c in self.checks if c["head_sha"] == sha and name in (None, c["name"])]}, {}
            )
        if method == "GET" and path.endswith("/statuses"):
            return Response(200, [], {})
        if method in ("POST", "PATCH") and "/check-runs" in path:
            if method == "POST":
                self.checks.append({**body, "id": len(self.checks) + 1, "app": {"slug": "osac-ci"}})
            else:
                target = next(c for c in self.checks if path.endswith(f"/{c['id']}"))
                target.update({k: v for k, v in body.items() if k != "output"})
                target["output"] = body["output"]
            return Response(200 if method == "PATCH" else 201, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")

    def gate(self, sha: str = HEAD) -> dict[str, Any]:
        return [c for c in self.checks if c["name"] == autopilot_summary.NAME and c["head_sha"] == sha][-1]


def go(gh: Gh, actor: str = "duty-person", reason: str = REASON) -> str:
    return fast_track.apply(gh, "o/r", 7, actor, reason, POLICY, "https://x/run")  # type: ignore[arg-type]


# the mark -----------------------------------------------------------------------------------------------------------


def test_the_mark_round_trips_and_belongs_to_one_commit() -> None:
    fast = fast_mark.FastTrack(HEAD, "duty-person", REASON)
    text = "intro\n" + fast_mark.encode(fast) + "\n\n#### Events"
    assert fast_mark.decode(text, HEAD) == fast
    assert fast_mark.decode(text, OTHER) is None, "the mark of an older commit is of no use"
    assert fast_mark.decode("no mark here", HEAD) is None


@pytest.mark.parametrize(
    "text", ["<!-- fast-track:!!!! -->", "<!-- fast-track:e30 -->", "<!-- fast-track:W10 -->", "<!-- fast-track: -->"]
)
def test_a_damaged_mark_is_no_mark(text: str) -> None:
    assert fast_mark.decode(text, HEAD) is None


def test_a_mark_with_a_bad_login_or_a_short_or_dirty_reason_is_no_mark() -> None:
    for fast in (
        fast_mark.FastTrack(HEAD, "bad login", REASON),
        fast_mark.FastTrack(HEAD, "a", "no"),
        fast_mark.FastTrack(HEAD, "a", "<b>markup</b> in the reason"),
    ):
        assert fast_mark.decode(fast_mark.encode(fast), HEAD) is None


def test_the_reason_loses_what_makes_markup_and_is_cut() -> None:
    assert fast_mark.clean_reason("  a <img src=x> `b`\n [c](d)  ") == "a img src=x b c (d)"
    assert len(fast_mark.clean_reason("x" * 500)) == fast_mark.REASON_MAX


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
    many = [f".github/f{i}" for i in range(6)]
    assert "and 3 more" in (refused(files=many) or "")


# the gate -----------------------------------------------------------------------------------------------------------


def test_a_fast_track_passes_the_gate_and_says_who_and_why() -> None:
    gh = Gh()
    said = go(gh)
    assert said == f"Fast track: skipped by duty-person: {REASON} (commit aaaaaaa)"
    gate = gh.gate()
    assert (gate["status"], gate["conclusion"]) == ("completed", "success")
    assert gate["output"]["title"] == f"Fast track: skipped by duty-person: {REASON}"
    assert "> **Fast track** by @duty-person" in gate["output"]["summary"]
    assert fast_mark.decode(gate["output"]["text"], HEAD) == fast_mark.FastTrack(HEAD, "duty-person", REASON)
    assert "Fast track by duty-person" in gate["output"]["text"], "the event is in the log of the check"


def test_the_gate_stays_passed_when_a_line_fails_later_and_keeps_the_mark() -> None:
    gh = Gh()
    go(gh)
    gh.line("CI Autopilot / CI / Lint", "failure")
    autopilot_summary.update(gh, "o/r", 7, POLICY, "lint failed", "", NOW)  # type: ignore[arg-type]
    gate = gh.gate()
    assert gate["conclusion"] == "success" and "Fast track" in gate["output"]["title"]
    assert fast_mark.decode(gate["output"]["text"], HEAD) is not None
    assert len([c for c in gh.checks if c["name"] == autopilot_summary.NAME]) == 1


def test_a_new_commit_has_no_fast_track() -> None:
    gh = Gh()
    go(gh)
    gh.head = OTHER
    assert fast_track.current(gh, "o/r", 7) is None  # type: ignore[arg-type]
    autopilot_summary.update(gh, "o/r", 7, POLICY, "pushed", "", NOW)  # type: ignore[arg-type]
    assert gh.gate(OTHER).get("conclusion") is None and "Fast track" not in gh.gate(OTHER)["output"]["title"]


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
def test_a_refused_fast_track_writes_nothing(make: Any, words: str) -> None:
    gh, actor, reason = make()
    with pytest.raises(ValueError, match=words):
        go(gh, actor, reason)
    assert gh.checks == []


def test_a_policy_without_the_key_lets_nobody_fast_track() -> None:
    plain = parse_policy("version: 1\nrepo: o/r\njobs: {lint: {kind: cheap, check: x}}\n")
    with pytest.raises(ValueError, match="nobody may"):
        fast_track.apply(Gh(), "o/r", 7, "duty-person", REASON, plain)  # type: ignore[arg-type]


def test_the_policy_key_is_checked_when_it_loads() -> None:
    jobs = "jobs: {lint: {kind: cheap, check: x}}\n"
    with pytest.raises(PolicyError):
        parse_policy("version: 1\nrepo: o/r\nfast_track: {team: 'a b'}\n" + jobs)
    with pytest.raises(PolicyError):
        parse_policy("version: 1\nrepo: o/r\nfast_track: {team: t, other: 1}\n" + jobs)
    assert (
        parse_policy("version: 1\nrepo: o/r\nfast_track: {team: t}\n" + jobs).fast_track.protected_paths[0]
        == ".github/**"
    )  # type: ignore[union-attr]


# the merge queue ----------------------------------------------------------------------------------------------------


def queue(gh: Gh, ref: str = f"gh-readonly-queue/main/pr-7-{'c' * 40}") -> fast_mark.FastTrack | None:
    return fast_track.for_queue(gh, "o/r", ref)  # type: ignore[arg-type]


def test_a_queue_entry_of_a_fast_tracked_pull_request_is_fast_tracked() -> None:
    gh = Gh()
    go(gh)
    assert queue(gh) == fast_mark.FastTrack(HEAD, "duty-person", REASON)
    assert queue(gh, f"refs/heads/gh-readonly-queue/main/pr-7-{'c' * 40}") is not None
    assert fast_track.queue_outputs(queue(gh)) == {"fast": True, "login": "duty-person", "reason": REASON}
    assert fast_track.queue_outputs(None)["fast"] is False


def test_a_queue_entry_is_tested_when_there_is_no_fast_track_for_its_pull_request() -> None:
    gh = Gh()
    assert queue(gh) is None, "no gate at all"
    go(gh)
    assert queue(gh, "main") is None and queue(gh, "gh-readonly-queue/main/pr-x-" + "c" * 40) is None
    gh.head = OTHER
    assert queue(gh) is None, "the pull request moved on: the fast track was for the older commit"
    gh.head, gh.state = HEAD, "closed"
    assert queue(gh) is None


def test_a_gate_that_another_app_wrote_is_no_proof() -> None:
    gh = Gh()
    go(gh)
    gh.gate()["app"] = {"slug": "github-actions"}
    assert queue(gh) is None


def test_the_gate_of_the_queue_entry_says_fast_track_and_passes() -> None:
    gh = Gh()
    said = queue_gate.write(gh, "o/r", "c" * 40, 1, [], "fast-track", f"duty-person: {REASON}", "https://x/run")  # type: ignore[arg-type]
    assert said == "created"
    gate = [c for c in gh.checks if c["head_sha"] == "c" * 40 or c.get("head_sha") == "c" * 40][-1]
    assert (gate["status"], gate["conclusion"]) == ("completed", "success")
    assert gate["output"]["title"] == f"Fast track: skipped by duty-person: {REASON}"


# the decision and the commands ---------------------------------------------------------------------------------------


def test_the_command_fast_tracks_and_a_refusal_is_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = Gh()
    monkeypatch.setattr(cli, "build_client", lambda **_: gh)
    monkeypatch.setattr(cli, "load_policy", lambda _p: POLICY)
    args = ["fast-track", "--policy", "p", "--repo", "o/r", "--number", "7", "--reason", REASON]
    assert cli.main([*args, "--actor", "somebody"]) == 3
    assert "not in the ci-duty team" in capsys.readouterr().err and gh.checks == []
    assert cli.main([*args, "--actor", "duty-person"]) == 0
    assert "Fast track: Fast track: skipped by duty-person" in capsys.readouterr().out
    monkeypatch.setattr(cli, "load_policy", lambda _p: POLICY)
    assert cli.main(["fast-track-queue", "--repo", "o/r", "--head-ref", f"gh-readonly-queue/main/pr-7-{'c' * 40}"]) == 0
    assert '"fast": true' in capsys.readouterr().out
