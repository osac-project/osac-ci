"""The review sink: the account that stands in for a team's review request is taken off once an approval counts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from osac_ci import cli, review_sink
from osac_ci.github.api import GitHubError, Response

pytestmark = pytest.mark.unit
HEAD = "a" * 40
OLD = "b" * 40
SINK = "osac-ci-bot"


class Gh:
    def __init__(self, *, state: str = "open", requested: tuple[str, ...] = (SINK,), head_repo: str = "o/r") -> None:
        self.state, self.requested, self.head_repo = state, requested, head_repo
        self.reviews: list[dict[str, Any]] = []
        self.permissions = {"maintainer": "write", "dev": "write", "stranger": "read"}
        self.removed: list[Any] = []
        self.remove_status = 200

    def review(self, user: str, state: str = "APPROVED", commit: str = HEAD, kind: str = "User") -> None:
        self.reviews.append({"user": {"login": user, "type": kind}, "state": state, "commit_id": commit,
                             "author_association": "MEMBER", "submitted_at": "2026-11-04T10:00:00Z"})  # fmt: skip

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        if method == "GET" and path == "/repos/o/r/pulls/7":
            head = {"sha": HEAD, "repo": {"full_name": self.head_repo}}
            pull = {"state": self.state, "user": {"login": "dev"}, "head": head,
                    "requested_reviewers": [{"login": r} for r in self.requested]}  # fmt: skip
            return Response(200, pull, {})
        if method == "GET" and path == "/repos/o/r/pulls/7/reviews":
            return Response(200, self.reviews, {})
        if method == "GET" and path.startswith("/repos/o/r/collaborators/") and path.endswith("/permission"):
            user = path.split("/")[-2]
            return Response(200, {"permission": self.permissions.get(user, "none"), "user": {"login": user}}, {})
        if method == "DELETE" and path == "/repos/o/r/pulls/7/requested_reviewers":
            self.removed.append(body)
            return Response(self.remove_status, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")


def clear(gh: Gh, sink: str = SINK) -> str:
    return review_sink.clear(gh, "o/r", 7, sink)  # type: ignore[arg-type]


def test_an_approval_of_the_current_commit_takes_the_sink_off() -> None:
    gh = Gh()
    gh.review("maintainer")
    said = clear(gh)
    assert gh.removed == [{"reviewers": [SINK]}]
    assert said == f"removed the request of {SINK} (maintainer approved aaaaaaa)"


@pytest.mark.parametrize(
    "setup",
    [
        lambda g: None,
        lambda g: g.review("maintainer", commit=OLD),  # an approval of an older commit
        lambda g: g.review("maintainer", state="CHANGES_REQUESTED"),
        lambda g: g.review("stranger"),  # no write access: the approval does not count
        lambda g: g.review("dev"),  # the author does not count
        lambda g: (g.review("maintainer"), g.review("maintainer", state="DISMISSED")),
        lambda g: g.review(SINK, kind="Bot"),  # the sink itself: GitHub clears its request, nothing is left to remove
    ],
)
def test_the_sink_stays_when_nobody_else_has_an_approval_that_counts(setup: Any) -> None:
    gh = Gh()
    setup(gh)
    assert "stays" in clear(gh) and gh.removed == []


def test_an_approval_of_coderabbit_counts_like_the_one_that_turns_auto_merge_on() -> None:
    gh = Gh()
    gh.review("coderabbitai[bot]", kind="Bot")
    assert "removed the request" in clear(gh) and gh.removed == [{"reviewers": [SINK]}]


def test_the_sink_approving_next_to_another_approval_still_leaves_nothing_to_do_but_the_other_one_removes_it() -> None:
    gh = Gh()
    gh.review(SINK, kind="Bot")
    gh.review("maintainer")
    assert clear(gh).startswith(f"removed the request of {SINK} (maintainer approved")


def test_nothing_is_done_when_the_sink_is_not_asked_or_the_pull_request_is_over() -> None:
    gh = Gh(requested=("somebody",))
    gh.review("maintainer")
    assert "is not asked to review" in clear(gh) and gh.removed == []
    gh = Gh(state="closed")
    gh.review("maintainer")
    assert "not open" in clear(gh) and gh.removed == []


def test_only_the_sink_goes_and_a_fork_needs_a_person_with_write_access() -> None:
    gh = Gh(requested=(SINK, "somebody"), head_repo="fork/r")
    gh.review("maintainer")
    clear(gh)
    assert gh.removed == [{"reviewers": [SINK]}]  # not the other request
    gh = Gh(head_repo="fork/r")
    gh.review("coderabbitai[bot]", kind="Bot")
    assert "stays" in clear(gh) and gh.removed == []  # on a fork a bot's approval does not count


def test_a_refusal_of_github_is_an_error_and_the_sink_must_be_a_login() -> None:
    gh = Gh()
    gh.review("maintainer")
    gh.remove_status = 403
    with pytest.raises(GitHubError):
        clear(gh)
    with pytest.raises(ValueError, match="login"):
        clear(Gh(), "not a login")


def test_the_command(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gh = Gh()
    gh.review("maintainer")
    monkeypatch.setattr(cli, "build_client", lambda **_: gh)
    assert cli.main(["review-sink-clear", "--repo", "o/r", "--number", "7", "--sink", SINK]) == 0
    assert "Review sink: removed the request of osac-ci-bot" in capsys.readouterr().out
    gh.remove_status = 500
    gh.removed.clear()
    assert cli.main(["review-sink-clear", "--repo", "o/r", "--number", "7", "--sink", SINK]) == 3


def test_the_decide_workflow_takes_the_sink_off_in_the_job_that_turns_auto_merge_on_and_only_when_asked() -> None:
    import yaml
    from helpers import ROOT

    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "e2e-pr-decide.yml").read_text(encoding="utf-8"))
    given = workflow[True]["workflow_call"]["inputs"]["review-sink"]
    assert given["default"] == "", "off unless the caller names an account"
    job = workflow["jobs"]["auto-merge"]
    assert job["if"] == "needs.decide.outputs.approved == 'true'"
    step = next(s for s in job["steps"] if s.get("uses", "").endswith("/review-sink-clear"))
    assert step["if"] == "inputs.review-sink != ''" and step["continue-on-error"] is True
    assert step["with"]["github-token"] == "${{ steps.app-token.outputs.token }}"
    assert step["with"]["sink"] == "${{ inputs.review-sink }}"


def test_logins_and_repository_names_are_compared_without_regard_to_case() -> None:
    gh = Gh(requested=("Osac-CI-Bot",), head_repo="O/R")  # GitHub spells the login and the repository its own way
    gh.review("maintainer")
    said = clear(gh, "osac-ci-bot")
    assert gh.removed == [{"reviewers": ["Osac-CI-Bot"]}], "the request is removed under the spelling GitHub uses"
    assert "removed the request of Osac-CI-Bot" in said
    gh = Gh(requested=("Osac-CI-Bot",))
    gh.review("OSAC-ci-bot", kind="Bot")  # the sink approving, spelled another way: nothing left to remove
    assert "stays" in clear(gh, "osac-ci-bot") and gh.removed == []
    gh = Gh(head_repo="O/R")
    gh.review("coderabbitai[bot]", kind="Bot")  # the same repository, so not a fork: a bot's approval counts
    assert "removed the request" in clear(gh)
