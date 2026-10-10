"""Finding the open pull request(s) a finished workflow run belongs to, by head commit or by head owner and branch."""

from __future__ import annotations

from typing import Any

import pytest
from fakes import REPO, FakeGitHub

from osac_ci import cli
from osac_ci.github.api import GitHubError, Response
from osac_ci.publish import find_open_prs

pytestmark = pytest.mark.contract
SHA = "a" * 40
OTHER = "b" * 40
PULLS = f"/repos/{REPO}/pulls"


class Pulls(FakeGitHub):
    """Serves the open-PR listing page by page and an owner:branch filter, recording the queries asked."""

    def __init__(self, prs: list[dict[str, Any]]) -> None:
        super().__init__()
        self.prs = prs
        self.queries: list[dict[str, str]] = []

    def request(self, method: str, path: str, *, params: Any = None, body: Any = None) -> Response:
        if path != PULLS:
            return super().request(method, path, params=params, body=body)
        self.calls.append((method, path))
        self.queries.append(dict(params or {}))
        params = params or {}
        if "head" in params:
            owner, _, branch = params["head"].partition(":")
            return Response(200, [p for p in self.prs if p.get("_owner") == owner and p.get("_branch") == branch])
        page, per = int(params.get("page", "1")), int(params.get("per_page", "30"))
        return Response(200, self.prs[(page - 1) * per : page * per])


def pr(number: int, sha: str = OTHER, owner: str = "someone", branch: str = "feature") -> dict[str, Any]:
    return {"number": number, "head": {"sha": sha}, "_owner": owner, "_branch": branch}


def test_the_head_commit_finds_the_pull_request_whatever_the_run_says_about_its_repository() -> None:
    fake = Pulls([pr(1), pr(2, SHA, owner="eliorerz", branch="osac-ci-override")])
    assert find_open_prs(fake, REPO, sha=SHA) == [2]


def test_every_open_pull_request_with_that_head_is_returned() -> None:
    assert find_open_prs(Pulls([pr(3, SHA), pr(4), pr(5, SHA)]), REPO, sha=SHA) == [3, 5]


def test_matches_on_later_pages_are_returned_too() -> None:
    """Two open pull requests can share a head commit, and they need not be on the same page of the listing."""
    fake = Pulls([pr(n) for n in range(1, 300)])
    fake.prs.insert(3, pr(777, SHA))
    fake.prs.insert(250, pr(888, SHA))
    assert find_open_prs(fake, REPO, sha=SHA) == [777, 888]
    assert [q["page"] for q in fake.queries] == ["1", "2", "3"]
    assert fake.queries[0]["sort"] == "updated" and fake.queries[0]["direction"] == "desc"


def test_a_short_listing_ends_the_search() -> None:
    fake = Pulls([pr(1, SHA), pr(2)])
    assert find_open_prs(fake, REPO, sha=SHA) == [1] and len(fake.queries) == 1


def test_the_search_is_bounded_to_three_pages() -> None:
    fake = Pulls([pr(n) for n in range(1, 450)] + [pr(900, SHA)])
    assert find_open_prs(fake, REPO, sha=SHA) == []
    assert len(fake.queries) == 3


def test_a_same_named_branch_in_the_base_repository_is_never_taken_for_the_run() -> None:
    """The case from review: a review-started run names the base repository and the fork's branch name. A pull request
    that merely has that branch in the base repository is not the run's pull request."""
    fake = Pulls([pr(1, OTHER, owner="osac-project", branch="osac-ci-override")])
    assert find_open_prs(fake, REPO, sha=SHA) == []
    assert not [q for q in fake.queries if "head" in q]  # the head owner:branch filter is never asked


def test_nothing_is_guessed() -> None:
    assert find_open_prs(Pulls([pr(1)]), REPO, sha=SHA) == []
    assert find_open_prs(Pulls([]), REPO, sha=SHA) == []


@pytest.mark.parametrize("sha", ["", "abc", "g" * 40, SHA + "0", "../x"])
def test_an_invalid_sha_is_an_error_not_a_query(sha: str) -> None:
    fake = Pulls([])
    with pytest.raises(ValueError):
        find_open_prs(fake, REPO, sha=sha)
    assert fake.calls == []


def test_an_api_error_is_an_error_not_an_empty_answer() -> None:
    class Broken(Pulls):
        def request(self, method: str, path: str, *, params: Any = None, body: Any = None) -> Response:
            return Response(502, {"message": "bad gateway"})

    with pytest.raises(GitHubError):
        find_open_prs(Broken([]), REPO, sha=SHA)


def run_cli(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, *args: str) -> int:
    monkeypatch.setattr(cli, "build_client", lambda: fake)
    return cli.main(["find-pr", "--repo", REPO, *args])


def test_the_command_prints_one_number_per_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, Pulls([pr(3, SHA), pr(5, SHA)]), "--sha", SHA) == 0
    assert capsys.readouterr().out == "3\n5\n"


def test_the_command_prints_nothing_when_nothing_matches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, Pulls([pr(1)]), "--sha", SHA) == 0
    assert capsys.readouterr().out.strip() == ""


def test_the_command_needs_a_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, Pulls([]))


def test_the_command_reports_errors_with_exit_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, Pulls([]), "--sha", "nope") == 3
    assert "invalid commit sha" in capsys.readouterr().err
