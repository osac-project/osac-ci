"""Reading a lock fact back from the OSAC CI check run of a commit."""

from __future__ import annotations

from typing import Any

import pytest
from fakes import REPO, SHA, FakeGitHub

from osac_ci import cli
from osac_ci.lockfacts import UNKNOWN, encode, lookup
from osac_ci.model import LockFact

pytestmark = pytest.mark.contract
CHECKS = f"/repos/{REPO}/commits/{SHA}/check-runs"
FACTS = (LockFact("e2e-vmaas", "e2e-vmaas-gate", "locked", "cost"), LockFact("unit", "Run unit tests", "open"))


def fake_with(*runs: dict[str, Any]) -> FakeGitHub:
    fake = FakeGitHub()
    fake.add("GET", CHECKS, {"total_count": len(runs), "check_runs": list(runs)})
    return fake


def run(text: str, id_: int = 1) -> dict[str, Any]:
    return {"id": id_, "name": "OSAC CI", "output": {"text": text}}


def test_a_locked_and_an_open_job_are_read_back() -> None:
    fake = fake_with(run(encode(FACTS, SHA)))
    assert lookup(fake, REPO, SHA, "e2e-vmaas", "OSAC CI") == "locked"
    assert lookup(fake, REPO, SHA, "unit", "OSAC CI") == "open"


def test_the_request_asks_for_the_latest_run_of_that_name() -> None:
    fake = fake_with(run(encode(FACTS, SHA)))
    lookup(fake, REPO, SHA, "unit", "OSAC CI")
    assert fake.calls == [("GET", CHECKS)]


def test_the_newest_run_wins() -> None:
    old = run(encode((LockFact("unit", "u", "locked", "cost"),), SHA), id_=1)
    new = run(encode((LockFact("unit", "u", "open"),), SHA), id_=2)
    assert lookup(fake_with(new, old), REPO, SHA, "unit", "OSAC CI") == "open"


@pytest.mark.parametrize(
    "runs",
    [
        [],
        [{"id": 1, "name": "OSAC CI", "output": {}}],
        [{"id": 1, "name": "OSAC CI", "output": {"text": None}}],
        [{"id": 1, "name": "OSAC CI"}],
        [run("no block")],
        [run(encode(FACTS, "d" * 40))],  # a block for another commit
    ],
)
def test_no_usable_fact_is_unknown(runs: list[dict[str, Any]]) -> None:
    assert lookup(fake_with(*runs), REPO, SHA, "unit", "OSAC CI") == UNKNOWN


def test_a_job_without_locks_is_unknown() -> None:
    assert lookup(fake_with(run(encode(FACTS, SHA))), REPO, SHA, "lint", "OSAC CI") == UNKNOWN


def test_an_api_error_is_unknown_not_an_exception() -> None:
    fake = FakeGitHub()
    fake.add("GET", CHECKS, {"message": "boom"}, status=502)
    assert lookup(fake, REPO, SHA, "unit", "OSAC CI") == UNKNOWN


@pytest.mark.parametrize("sha", ["", "abc", "g" * 40, SHA + "0"])
def test_a_bad_sha_is_an_error_not_a_query(sha: str) -> None:
    fake = FakeGitHub()
    with pytest.raises(ValueError):
        lookup(fake, REPO, sha, "unit", "OSAC CI")
    assert fake.calls == []


def command(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, *args: str) -> int:
    monkeypatch.setattr(cli, "build_client", lambda **_: fake)
    return cli.main(["lock-status", "--repo", REPO, *args])


def test_the_command_prints_the_state(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert command(monkeypatch, fake_with(run(encode(FACTS, SHA))), "--sha", SHA, "--job", "e2e-vmaas") == 0
    assert capsys.readouterr().out == "locked\n"


def test_the_command_prints_unknown_and_still_succeeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert command(monkeypatch, fake_with(), "--sha", SHA, "--job", "x") == 0
    assert capsys.readouterr().out == "unknown\n"


def test_the_command_refuses_a_bad_sha(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert command(monkeypatch, FakeGitHub(), "--sha", "nope", "--job", "x") == 3
    assert "invalid commit sha" in capsys.readouterr().err


def test_the_command_needs_no_policy_file(monkeypatch: pytest.MonkeyPatch) -> None:
    assert command(monkeypatch, fake_with(), "--sha", SHA, "--job", "x") == 0


def test_the_command_goes_on_without_any_token_for_a_public_repository(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in cli.TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)
    seen: dict[str, Any] = {}

    class Anonymous(FakeGitHub):
        pass

    def fake_http_client(token: str, *, anonymous: bool = False) -> FakeGitHub:
        seen.update(token=token, anonymous=anonymous)
        return fake_with(run(encode(FACTS, SHA)))

    monkeypatch.setattr(cli, "HttpClient", fake_http_client)
    assert cli.main(["lock-status", "--repo", REPO, "--sha", SHA, "--job", "unit"]) == 0
    assert capsys.readouterr().out == "open\n" and seen == {"token": "", "anonymous": True}


def test_with_a_token_the_command_uses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "tok")
    seen: dict[str, Any] = {}

    def fake_http_client(token: str, *, anonymous: bool = False) -> FakeGitHub:
        seen.update(token=token, anonymous=anonymous)
        return fake_with()

    monkeypatch.setattr(cli, "HttpClient", fake_http_client)
    assert cli.main(["lock-status", "--repo", REPO, "--sha", SHA, "--job", "unit"]) == 0
    assert seen == {"token": "tok", "anonymous": False}


def test_the_other_commands_still_need_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in cli.TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="set one of"):
        cli.build_client()
