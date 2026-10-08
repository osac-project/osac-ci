"""`osac-ci explain-pr` end to end against the fake GitHub (no network)."""

from __future__ import annotations

import pytest
from fakes import REPO, FakeGitHub, standard_fake
from helpers import ROOT

from osac_ci import cli
from osac_ci.github.api import GitHubError

pytestmark = pytest.mark.contract
POLICY = str(ROOT / "policy" / "toy.yml")


def run(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, *extra: str) -> int:
    monkeypatch.setattr(cli, "build_client", lambda: fake)
    return cli.main(["explain-pr", "--policy", POLICY, "--repo", REPO, "--number", "7", "--org", "example", *extra])


def test_explain_pr_prints_a_verdict(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(monkeypatch, standard_fake()) == 0
    out = capsys.readouterr().out
    assert out.startswith("### OSAC CI:") and "**Next:**" in out


def test_explain_pr_reports_github_errors_with_exit_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = standard_fake()
    fake.add("GET", f"/repos/{REPO}/pulls/7", {"message": "Not Found"}, status=404)
    assert run(monkeypatch, fake) == 3
    assert "cannot read the PR" in capsys.readouterr().err


def test_explain_pr_without_membership_lookup_makes_no_membership_request(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = standard_fake()
    assert run(monkeypatch, fake, "--no-membership-lookup") == 0
    assert not [c for c in fake.calls if "/orgs/" in c[1]]


def test_a_membership_failure_is_not_silently_treated_as_no(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = standard_fake()
    fake.add("GET", "/orgs/example/members/alice", {"message": "rate limited"}, status=403)
    assert run(monkeypatch, fake) == 3
    assert "403" in capsys.readouterr().err


def test_missing_token_stops_with_a_clear_message(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in cli.TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as err:
        cli.build_client()
    assert "GH_TOKEN" in str(err.value)


def test_token_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "abc")
    assert cli.build_client() is not None


def test_github_error_type_is_exported() -> None:
    assert issubclass(GitHubError, RuntimeError)
