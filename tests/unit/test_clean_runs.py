"""Deleting the skipped runs of the service workflows: only what did nothing, only when old enough."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from osac_ci import clean_runs
from osac_ci.github.api import GitHubError, Response

pytestmark = pytest.mark.unit
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def run(
    rid: int, conclusion: str | None = "skipped", status: str = "completed", at: str = "2026-10-24T11:00:00Z"
) -> dict:
    return {"id": rid, "status": status, "conclusion": conclusion, "updated_at": at}


class Fake:
    """The workflow runs of a repository, by workflow file; DELETE answers ``delete_status`` (per run id if given)."""

    def __init__(
        self, runs: Mapping[str, list[dict[str, Any]]], delete_status: Mapping[int, int] | None = None
    ) -> None:
        self.runs = dict(runs)
        self.delete_status = dict(delete_status or {})
        self.asked: list[tuple[str, str]] = []

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        self.asked.append((method, path))
        if method == "GET":
            name = path.split("/workflows/")[1].split("/")[0]
            if name not in self.runs:
                return Response(404, {"message": "Not Found"}, {})
            page = int((params or {}).get("page", "1"))
            per = int((params or {}).get("per_page", "30"))
            items = self.runs[name][(page - 1) * per : page * per]
            return Response(200, {"workflow_runs": items}, {})
        rid = int(path.rsplit("/", 1)[1])
        return Response(self.delete_status.get(rid, 204), {}, {})


def deleted(fake: Fake) -> list[int]:
    return [int(p.rsplit("/", 1)[1]) for m, p in fake.asked if m == "DELETE"]


def sweep(fake: Fake, *names: str, **kw: Any) -> list[clean_runs.Cleaned]:
    return clean_runs.clean(fake, "o/r", list(names) or ["e2e-pr.yml"], now=NOW, **kw)  # type: ignore[arg-type]


def test_a_skipped_run_that_is_old_enough_is_deleted() -> None:
    fake = Fake({"e2e-pr.yml": [run(1)]})
    (result,) = sweep(fake)
    assert deleted(fake) == [1] and (result.found, result.deleted, result.failed) == (1, 1, ())


@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled", "timed_out", "neutral", None])
def test_a_run_that_did_something_or_failed_is_never_deleted(conclusion: str | None) -> None:
    fake = Fake({"e2e-pr.yml": [run(1, conclusion)]})
    sweep(fake)
    assert deleted(fake) == []


@pytest.mark.parametrize("status", ["in_progress", "queued", "waiting", "requested"])
def test_a_run_that_is_still_going_is_never_deleted(status: str) -> None:
    fake = Fake({"e2e-pr.yml": [run(1, "skipped", status)]})
    sweep(fake)
    assert deleted(fake) == []


def test_a_run_younger_than_the_minimum_age_stays_for_now() -> None:
    fake = Fake({"e2e-pr.yml": [run(1, at="2026-10-24T11:57:00Z"), run(2, at="2026-10-24T11:55:00Z")]})
    sweep(fake, min_age_minutes=5)
    assert deleted(fake) == [2], "five minutes old counts, three does not"


@pytest.mark.parametrize("bad", [{"id": 1, "status": "completed", "conclusion": "skipped"}, run(1, at="yesterday")])
def test_a_run_that_cannot_be_dated_is_left_alone(bad: dict) -> None:
    fake = Fake({"e2e-pr.yml": [bad]})
    sweep(fake)
    assert deleted(fake) == []


def test_a_dry_run_deletes_nothing_and_says_what_it_would() -> None:
    fake = Fake({"e2e-pr.yml": [run(1), run(2)]})
    results = sweep(fake, dry_run=True)
    assert deleted(fake) == [] and results[0].found == 2 and results[0].deleted == 0
    assert "2 skipped run(s) would be deleted" in clean_runs.render(results, dry_run=True)


def test_a_refused_delete_is_reported_and_the_others_still_go() -> None:
    fake = Fake({"e2e-pr.yml": [run(1), run(2), run(3)]}, delete_status={2: 403})
    (result,) = sweep(fake)
    assert deleted(fake) == [1, 2, 3] and result.deleted == 2 and result.failed == (2,)
    assert "1 refused" in clean_runs.render([result], dry_run=False)


def test_a_run_that_is_already_gone_counts_as_deleted() -> None:
    fake = Fake({"e2e-pr.yml": [run(1)]}, delete_status={1: 404})
    assert sweep(fake)[0].deleted == 1


def test_a_workflow_the_repository_does_not_have_is_not_an_error() -> None:
    fake = Fake({"e2e-pr.yml": [run(1)]})
    results = sweep(fake, "e2e-comment.yml", "e2e-pr.yml")
    assert results[0].missing and results[1].deleted == 1


def test_a_listing_that_fails_for_another_reason_is_an_error() -> None:
    class Down(Fake):
        def request(self, method: str, path: str, **kw: Any) -> Response:
            return Response(502, {"message": "bad gateway"}, {})

    with pytest.raises(GitHubError):
        sweep(Down({}))


@pytest.mark.parametrize("name", ["../x.yml", ".github/workflows/e2e-pr.yml", "e2e-pr", "a b.yml", "", "x.yml?y=1"])
def test_a_workflow_name_that_is_not_a_file_name_is_refused_before_anything_is_asked(name: str) -> None:
    fake = Fake({})
    with pytest.raises(ValueError):
        clean_runs.clean(fake, "o/r", [name], now=NOW)  # type: ignore[arg-type]
    assert fake.asked == []


def test_a_repository_that_is_not_owner_slash_name_and_a_zero_age_are_refused() -> None:
    with pytest.raises(ValueError):
        clean_runs.clean(Fake({}), "o/r/x", ["e2e-pr.yml"], now=NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        clean_runs.clean(Fake({}), "o/r", ["e2e-pr.yml"], now=NOW, min_age_minutes=0)  # type: ignore[arg-type]


def test_at_most_500_runs_of_one_workflow_are_taken_in_one_sweep() -> None:
    fake = Fake({"e2e-pr.yml": [run(i) for i in range(1, 601)]})
    sweep(fake)
    assert len(deleted(fake)) == 500, "the next sweep takes the rest"
