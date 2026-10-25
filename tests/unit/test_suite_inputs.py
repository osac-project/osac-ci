"""A suite that runs as a run of its own checks what it was started with against its pull request: anyone with write
access can start it with any inputs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
import yaml
from helpers import ROOT

from osac_ci import cli, e2e_dispatch
from osac_ci.github.api import Response

pytestmark = pytest.mark.unit
HEAD = "a" * 40
OTHER = "b" * 40


class Gh:
    """One pull request (number 7) of o/r with its head commit, the repository that holds it and its author."""

    def __init__(
        self, head_repo: str | None = "o/r", sha: str = HEAD, login: str = "alice", assoc: str = "MEMBER"
    ) -> None:
        self.pull = {
            "head": {"sha": sha, "repo": {"full_name": head_repo} if head_repo else None},
            "user": {"login": login},
            "author_association": assoc,
        }

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path == "/repos/o/r/pulls/7":
            return Response(200, self.pull, {})
        raise AssertionError(f"unexpected request {method} {path}")


def verify(gh: Gh, sha: str = HEAD, head_repo: str = "o/r", number: int = 7) -> tuple[str, str]:
    return e2e_dispatch.verify_suite_inputs(gh, "o/r", number, sha, head_repo)  # type: ignore[arg-type]


def test_a_commit_of_the_repository_itself_needs_no_author_or_association() -> None:
    assert verify(Gh()) == ("", "")


def test_a_fork_commit_gets_its_author_and_association_from_the_pull_request() -> None:
    assert verify(Gh(head_repo="alice/r", assoc="CONTRIBUTOR"), head_repo="alice/r") == ("alice", "CONTRIBUTOR")


def test_the_repository_is_compared_without_regard_to_case() -> None:
    assert verify(Gh(head_repo="Alice/R"), head_repo="alice/r")[0] == "alice"


def test_a_commit_that_is_no_longer_the_head_is_refused() -> None:
    with pytest.raises(ValueError, match="not the head"):
        verify(Gh(), sha=OTHER)


def test_a_repository_that_does_not_hold_the_head_is_refused() -> None:
    with pytest.raises(ValueError, match="not the one that holds"):
        verify(Gh(head_repo="alice/r"), head_repo="mallory/r")


def test_a_pull_request_whose_head_repository_was_deleted_is_refused() -> None:
    with pytest.raises(ValueError, match="not the one that holds"):
        verify(Gh(head_repo=None))


@pytest.mark.parametrize("login", ["", "bad login", "x;id"])
def test_an_author_that_is_not_a_login_is_refused(login: str) -> None:
    with pytest.raises(ValueError, match="could not be read"):
        verify(Gh(head_repo="alice/r", login=login), head_repo="alice/r")


def test_an_association_that_is_not_one_is_refused() -> None:
    with pytest.raises(ValueError, match="could not be read"):
        verify(Gh(head_repo="alice/r", assoc="member; id"), head_repo="alice/r")


@pytest.mark.parametrize(("sha", "number"), [("zz", 7), (HEAD, 0), ("A" * 40, 7)])
def test_inputs_that_are_not_what_they_should_be_are_refused_before_anything_is_read(sha: str, number: int) -> None:
    with pytest.raises(ValueError):
        verify(Gh(), sha=sha, number=number)


def test_the_command_prints_the_outputs_and_fails_on_a_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "build_client", lambda: Gh(head_repo="alice/r"))
    args = ["suite-inputs", "--repo", "o/r", "--number", "7", "--sha", HEAD]
    assert cli.main([*args, "--head-repo", "alice/r"]) == 0
    assert capsys.readouterr().out.splitlines() == ["fork-author=alice", "fork-association=MEMBER"]
    assert cli.main([*args, "--head-repo", "mallory/r"]) == 3
    assert "suite-inputs failed" in capsys.readouterr().err


def test_the_shared_workflow_hands_the_checked_values_on_and_asks_for_read_access_only() -> None:
    wf = yaml.safe_load((ROOT / ".github/workflows/suite-inputs.yml").read_text(encoding="utf-8"))
    job = wf["jobs"]["verify"]
    assert job["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert set(wf[True]["workflow_call"]["outputs"]) == {"fork-author", "fork-association"}
    (step,) = job["steps"]
    assert step["uses"] == "$/.github/actions/suite-inputs"
    assert step["with"]["repo"] == "${{ github.repository }}", "the pull request is looked up in this repository"
    assert step["with"]["github-token"] == "${{ github.token }}"
