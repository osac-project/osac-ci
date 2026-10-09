"""The stale-only sweep: one GraphQL listing, then only the PRs that need a verdict are read and posted."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fakes import REPO, SHA, FakeGitHub, standard_fake

from osac_ci import cli
from osac_ci.github.api import GitHubError, Response
from osac_ci.github.standing import fetch_standings
from osac_ci.policy import Policy, parse_policy
from osac_ci.publish import describe, sweep
from osac_ci.stale import StaleRules

pytestmark = pytest.mark.contract
BASE = f"/repos/{REPO}"
NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
READY: Policy = parse_policy(
    "version: 1\nrepo: example/app\nmerge: {required_labels: [lgtm, approved], blocking_labels: []}\n"
    "jobs: {a: {check: check-0}, b: {check: check-1}, c: {check: check-2}}\n"
)


def node(number: int, updated: str, runs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    suites = [{"checkRuns": {"nodes": runs}}] if runs is not None else []
    return {
        "number": number,
        "updatedAt": updated,
        "headRefOid": SHA,
        "commits": {"nodes": [{"commit": {"checkSuites": {"nodes": suites}}}]},
    }


def run_node(status: str = "COMPLETED", conclusion: str | None = "SUCCESS", at: str = "2026-10-09T11:00:00Z") -> dict:
    return {"status": status, "conclusion": conclusion, "startedAt": at, "completedAt": at if conclusion else None}


class Listing(FakeGitHub):
    """Answers the open-PR listing (in pages of ``page_size``) and leaves every other GraphQL query to the base fake."""

    def __init__(self, nodes: list[dict[str, Any]], page_size: int = 100) -> None:
        super().__init__()
        self.nodes, self.page_size, self.queries = nodes, page_size, 0

    def request(self, method: str, path: str, *, params: Any = None, body: Any = None) -> Response:
        if path == "/graphql" and body and "pullRequests" in body["query"]:
            self.queries += 1
            self.calls.append((method, path))
            self.bodies.append((method, path, body))
            start = int(body["variables"]["after"] or "0")
            chunk = self.nodes[start : start + self.page_size]
            more = start + self.page_size < len(self.nodes)
            page = {"nodes": chunk, "pageInfo": {"hasNextPage": more, "endCursor": str(start + self.page_size)}}
            return Response(200, {"data": {"repository": {"pullRequests": page}}})
        return super().request(method, path, params=params, body=body)


def listing_with_pr_routes(nodes: list[dict[str, Any]], **kw: Any) -> Listing:
    fake = Listing(nodes, **kw)
    template = standard_fake()
    for route, answer in template.routes.items():
        fake.routes[route] = answer
    for n in {x["number"] for x in nodes}:
        for suffix in ("", "/reviews", "/files"):
            fake.routes[("GET", f"{BASE}/pulls/{n}{suffix}")] = template.routes[("GET", f"{BASE}/pulls/7{suffix}")]
        fake.routes[("GET", f"{BASE}/issues/{n}/events")] = (200, [])
    fake.add("POST", f"{BASE}/check-runs", {"id": 1}, status=201)
    return fake


def posts(fake: FakeGitHub) -> int:
    return len([1 for m, p, _ in fake.bodies if m == "POST" and p.endswith("/check-runs")])


def test_the_listing_is_paged_and_keeps_the_newest_run_across_suites() -> None:
    older, newer = run_node("COMPLETED", "ACTION_REQUIRED", "2026-10-09T10:00:00Z"), run_node(at="2026-10-09T11:00:00Z")
    nodes = [node(n, "2026-10-09T09:00:00Z", [older, newer]) for n in range(1, 6)]
    fake = Listing(nodes, page_size=2)
    standings = fetch_standings(fake, REPO, "OSAC CI")
    assert [s.number for s in standings] == [1, 2, 3, 4, 5] and fake.queries == 3
    assert standings[0].last is not None and standings[0].last.conclusion == "success"


def test_a_pr_without_a_check_run_has_no_verdict() -> None:
    (s,) = fetch_standings(Listing([node(1, "2026-10-09T09:00:00Z")]), REPO, "OSAC CI")
    assert s.last is None


def test_a_failed_listing_is_an_error_not_an_empty_sweep() -> None:
    class Broken(Listing):
        def request(self, method: str, path: str, *, params: Any = None, body: Any = None) -> Response:
            return Response(502, {"message": "bad gateway"})

    with pytest.raises(GitHubError):
        fetch_standings(Broken([]), REPO, "OSAC CI")


def test_only_the_prs_that_need_a_verdict_are_read_and_posted() -> None:
    fresh = node(1, "2026-10-09T09:00:00Z", [run_node(at="2026-10-09T10:00:00Z")])
    changed = node(2, "2026-10-09T11:30:00Z", [run_node(at="2026-10-09T10:00:00Z")])
    none_yet = node(3, "2026-10-09T09:00:00Z")
    fake = listing_with_pr_routes([fresh, changed, none_yet])
    result = sweep(fake, READY, REPO, org="example", stale=StaleRules(), now=NOW)
    assert sorted(o.pr for o in result) == [2, 3] and result.open_prs == 3
    assert not [c for c in fake.calls if c[1] == f"{BASE}/pulls/1"]  # the fresh PR was never read
    assert result.reasons == {3: "no verdict", 2: "changed since the verdict"}
    text = describe(result)
    assert "[no verdict]" in text and "stale-only: 2 of 3 open PRs needed a new verdict" in text


def test_a_verdict_found_unchanged_is_still_posted_so_the_pr_is_not_picked_every_time() -> None:
    """Without this the PR would stay "changed since the verdict" for good and every sweep would read it again."""
    from test_publish import with_existing

    changed = node(2, "2026-10-09T11:30:00Z", [run_node(at="2026-10-09T10:00:00Z")])
    first = listing_with_pr_routes([changed])
    sweep(first, READY, REPO, org="example", stale=StaleRules(), now=NOW)
    (body,) = [b for m, p, b in first.bodies if m == "POST" and p.endswith("/check-runs")]

    again = with_existing(listing_with_pr_routes([changed]), {"id": 5, "external_id": body["external_id"]})
    result = sweep(again, READY, REPO, org="example", stale=StaleRules(), now=NOW)
    assert [o.action for o in result] == ["created"] and posts(again) == 1

    plain = with_existing(listing_with_pr_routes([changed]), {"id": 5, "external_id": body["external_id"]})
    from osac_ci.publish import publish_pr

    assert publish_pr(plain, READY, REPO, 2, org="example").action == "unchanged"  # the normal rule is untouched


def test_nothing_is_read_when_everything_is_current() -> None:
    fresh = node(1, "2026-10-09T09:00:00Z", [run_node(at="2026-10-09T10:00:00Z")])
    fake = listing_with_pr_routes([fresh])
    result = sweep(fake, READY, REPO, org="example", stale=StaleRules(), now=NOW)
    assert list(result) == [] and posts(fake) == 0 and fake.queries == 1
    assert "0 PRs, 0 failed" in describe(result) and "0 of 1 open PRs" in describe(result)


def test_the_limit_keeps_the_most_urgent() -> None:
    nodes = [node(1, "2026-10-09T11:30:00Z", [run_node(at="2026-10-09T10:00:00Z")]), node(2, "2026-10-09T09:00:00Z")]
    fake = listing_with_pr_routes(nodes)
    result = sweep(fake, READY, REPO, org="example", stale=StaleRules(), now=NOW, limit=1)
    assert [o.pr for o in result] == [2]  # no verdict outranks a changed one


def test_it_cannot_be_combined_with_a_rotation() -> None:
    with pytest.raises(ValueError, match="do not combine"):
        sweep(listing_with_pr_routes([]), READY, REPO, stale=StaleRules(), recent=5)


def run_cli(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, *args: str) -> int:
    monkeypatch.setattr(cli, "build_client", lambda: fake)
    return cli.main(["publish", "--policy", "policy/toy.yml", "--repo", REPO, "--org", "example", *args])


@pytest.mark.parametrize(
    "args",
    [
        ("--number", "7", "--stale-only"),
        ("--all", "--stale-only", "--recent", "5"),
        ("--all", "--stale-only", "--rotate", "5"),
    ],
)
def test_cli_refuses_the_wrong_combinations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], args: tuple[str, ...]
) -> None:
    fake = listing_with_pr_routes([])
    assert run_cli(monkeypatch, fake, *args) == 2
    assert "--stale-only" in capsys.readouterr().err and fake.calls == []


def test_cli_runs_a_stale_only_sweep(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = listing_with_pr_routes([node(3, "2026-10-09T09:00:00Z")])
    assert run_cli(monkeypatch, fake, "--all", "--stale-only", "--dry-run") == 0
    assert "[no verdict]" in capsys.readouterr().out


# ---- which app's verdicts count, and what the query asks for -----------------------------------------------------


def variables_sent(fake: Listing) -> dict[str, Any]:
    (body,) = [b for m, p, b in fake.bodies if p == "/graphql"][:1]
    return body["variables"]


def test_the_default_trusts_verdicts_posted_with_the_actions_token() -> None:
    fake = Listing([node(1, "2026-10-09T09:00:00Z")])
    fetch_standings(fake, REPO, "OSAC CI")
    assert variables_sent(fake)["app"] == 15368 and variables_sent(fake)["check"] == "OSAC CI"


def test_any_app_can_be_accepted_for_a_check_posted_with_another_apps_token() -> None:
    fake = Listing([node(1, "2026-10-09T09:00:00Z")])
    fetch_standings(fake, REPO, "OSAC CI", app_id=None)
    assert variables_sent(fake)["app"] is None


def test_the_query_asks_for_the_newest_runs_not_the_first_ones() -> None:
    from osac_ci.github.standing import _QUERY

    assert "checkSuites(last:" in _QUERY and "checkRuns(last:" in _QUERY
    assert "$app: Int)" in _QUERY and "$app: Int!" not in _QUERY  # the filter is optional


def test_a_run_that_has_not_started_does_not_break_the_listing() -> None:
    queued = {"status": "QUEUED", "conclusion": None, "startedAt": None, "completedAt": None}
    (s,) = fetch_standings(Listing([node(1, "2026-10-09T09:00:00Z", [queued])]), REPO, "OSAC CI")
    assert s.last is not None and s.last.started_at is None and s.last.status == "queued"


def test_a_sweep_survives_a_queued_verdict_and_reads_that_pr() -> None:
    queued = {"status": "QUEUED", "conclusion": None, "startedAt": None, "completedAt": None}
    fake = listing_with_pr_routes([node(2, "2026-10-09T09:00:00Z", [queued])])
    result = sweep(fake, READY, REPO, org="example", stale=StaleRules(), now=NOW)
    assert [o.pr for o in result] == [2] and result.reasons == {2: "verdict without a time"}


def test_the_newest_of_several_runs_wins_even_when_it_has_not_started() -> None:
    old = run_node(at="2026-10-09T09:00:00Z")
    pending = {"status": "IN_PROGRESS", "conclusion": None, "startedAt": "2026-10-09T10:00:00Z", "completedAt": None}
    (s,) = fetch_standings(Listing([node(1, "2026-10-09T08:00:00Z", [old, pending])]), REPO, "OSAC CI")
    assert s.last is not None and s.last.status == "in_progress"


def test_cli_passes_the_app_and_the_ages_to_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_sweep(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        from osac_ci.publish import Sweep

        return Sweep([], 0)

    monkeypatch.setattr(cli, "sweep", fake_sweep)
    monkeypatch.setattr(cli, "build_client", lambda: FakeGitHub())
    argv = ["publish", "--policy", "policy/toy.yml", "--repo", REPO, "--all", "--stale-only"]
    assert cli.main([*argv, "--stale-after", "60", "--max-age", "0", "--verdict-app-id", "0"]) == 0
    assert seen["stale"] == StaleRules(60, 0) and seen["verdict_app_id"] is None
    assert cli.main(argv) == 0
    assert seen["stale"] == StaleRules(1800, 21600) and seen["verdict_app_id"] == 15368


@pytest.mark.parametrize("flag", ["--stale-after", "--max-age", "--verdict-app-id"])
def test_cli_rejects_negative_ages_and_ids(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["publish", "--policy", "policy/toy.yml", "--repo", REPO, "--all", "--stale-only", flag, "-1"])
    assert "must not be negative" in capsys.readouterr().err


def test_the_sweep_hands_the_app_to_the_listing() -> None:
    fake = listing_with_pr_routes([node(1, "2026-10-09T09:00:00Z", [run_node(at="2026-10-09T10:00:00Z")])])
    sweep(fake, READY, REPO, org="example", stale=StaleRules(), now=NOW, verdict_app_id=None)
    assert variables_sent(fake)["app"] is None
    other = listing_with_pr_routes([node(1, "2026-10-09T09:00:00Z", [run_node(at="2026-10-09T10:00:00Z")])])
    sweep(other, READY, REPO, org="example", stale=StaleRules(), now=NOW)
    assert variables_sent(other)["app"] == 15368
