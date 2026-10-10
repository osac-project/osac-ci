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
    """A run started by a review reports the base repository as head repository and the fork's branch name."""
    fake = Pulls([pr(1), pr(2, SHA, owner="eliorerz", branch="osac-ci-override")])
    assert find_open_prs(fake, REPO, sha=SHA, owner="osac-project", branch="osac-ci-override") == [2]


def test_a_same_named_branch_in_the_base_repository_does_not_win_over_the_exact_commit() -> None:
    fake = Pulls(
        [
            pr(1, OTHER, owner="osac-project", branch="osac-ci-override"),
            pr(2, SHA, owner="eliorerz", branch="osac-ci-override"),
        ]
    )
    assert find_open_prs(fake, REPO, sha=SHA, owner="osac-project", branch="osac-ci-override") == [2]


def test_every_open_pull_request_with_that_head_is_returned() -> None:
    assert find_open_prs(Pulls([pr(3, SHA), pr(4), pr(5, SHA)]), REPO, sha=SHA) == [3, 5]


def test_the_listing_is_newest_first_and_stops_at_the_first_page_with_a_match() -> None:
    fake = Pulls([pr(n) for n in range(1, 250)] + [pr(900, SHA)])
    fake.prs.insert(3, pr(777, SHA))
    assert find_open_prs(fake, REPO, sha=SHA) == [777]
    assert len(fake.queries) == 1 and fake.queries[0]["sort"] == "updated" and fake.queries[0]["direction"] == "desc"


def test_the_search_is_bounded_to_three_pages() -> None:
    fake = Pulls([pr(n) for n in range(1, 450)] + [pr(900, SHA)])
    assert find_open_prs(fake, REPO, sha=SHA) == []
    assert len(fake.queries) == 3


def test_owner_and_branch_are_used_only_when_no_open_pull_request_has_the_head_commit() -> None:
    fake = Pulls([pr(8, OTHER, owner="alice", branch="topic")])
    assert find_open_prs(fake, REPO, sha=SHA, owner="alice", branch="topic") == [8]
    assert find_open_prs(Pulls([pr(8, OTHER, owner="alice", branch="topic")]), REPO, owner="alice", branch="topic") == [
        8
    ]


def test_nothing_is_guessed() -> None:
    assert find_open_prs(Pulls([pr(1)]), REPO, sha=SHA) == []
    assert find_open_prs(Pulls([pr(1)]), REPO, owner="nobody", branch="x") == []
    assert find_open_prs(Pulls([pr(1)]), REPO) == []
    fake = Pulls([pr(1)])
    assert find_open_prs(fake, REPO, owner="alice") == [] and fake.calls == []  # an owner alone names nothing


@pytest.mark.parametrize("sha", ["abc", "g" * 40, SHA + "0", "../x"])
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


def test_the_branch_is_passed_as_data_never_built_into_a_path() -> None:
    fake = Pulls([])
    find_open_prs(fake, REPO, owner="o", branch="a b/c;$(x)")
    assert fake.queries[-1]["head"] == "o:a b/c;$(x)" and fake.calls == [("GET", PULLS)]


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


def test_the_command_reports_errors_with_exit_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_cli(monkeypatch, Pulls([]), "--sha", "nope") == 3
    assert "invalid commit sha" in capsys.readouterr().err
