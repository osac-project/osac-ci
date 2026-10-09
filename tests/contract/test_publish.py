"""Publishing the verdict as a check run, against the fake GitHub (no network)."""

from __future__ import annotations

from typing import Any

import pytest
from fakes import REPO, SHA, FakeGitHub, check_runs, standard_fake

from osac_ci import cli
from osac_ci.github.api import GitHubError
from osac_ci.policy import Policy, parse_policy
from osac_ci.publish import describe, publish_pr, sweep

pytestmark = pytest.mark.contract
BASE = f"/repos/{REPO}"
CHECKS = f"{BASE}/commits/{SHA}/check-runs"

# The fake PR carries lgtm and approved and three green checks named check-0 .. check-2.
READY = parse_policy(
    "version: 1\nrepo: example/app\nmerge: {required_labels: [lgtm, approved], blocking_labels: []}\n"
    "jobs: {a: {check: check-0}, b: {check: check-1}, c: {check: check-2}}\n"
)
WAITING = parse_policy(
    "version: 1\nrepo: example/app\nmerge: {required_labels: [lgtm, approved], blocking_labels: []}\n"
    "jobs: {a: {check: check-0}, z: {check: never-reported}}\n"
)


def fake_with_publish_routes() -> FakeGitHub:
    fake = standard_fake()
    fake.add("POST", f"{BASE}/check-runs", {"id": 99}, status=201)
    return fake


def with_existing(fake: FakeGitHub, *runs: dict[str, Any]) -> FakeGitHub:
    """Serve the three green checks plus already-posted OSAC CI runs (one route answers both lookups)."""
    data = check_runs(3)
    data["check_runs"] = [
        *data["check_runs"],
        *({"name": "OSAC CI", "status": "completed", "conclusion": "success", **r} for r in runs),
    ]
    fake.add("GET", CHECKS, data)
    return fake


def posted(fake: FakeGitHub) -> list[dict[str, Any]]:
    return [b for m, p, b in fake.bodies if m == "POST" and p.endswith("/check-runs")]


def publish(fake: FakeGitHub, policy: Policy = READY, **kw: Any):  # type: ignore[no-untyped-def]
    return publish_pr(fake, policy, REPO, 7, org="example", **kw)


def test_a_ready_pr_gets_a_success_check_on_its_head_commit() -> None:
    fake = fake_with_publish_routes()
    out = publish(fake)
    (body,) = posted(fake)
    assert (out.action, out.head_sha) == ("created", SHA)
    assert (body["name"], body["head_sha"], body["status"], body["conclusion"]) == (
        "OSAC CI",
        SHA,
        "completed",
        "success",
    )


def test_a_waiting_pr_is_in_progress_with_no_conclusion() -> None:
    fake = fake_with_publish_routes()
    publish(fake, WAITING)
    (body,) = posted(fake)
    assert body["status"] == "in_progress" and "conclusion" not in body


def test_an_unchanged_verdict_is_not_posted_again() -> None:
    first = fake_with_publish_routes()
    publish(first)
    (body,) = posted(first)
    again = with_existing(fake_with_publish_routes(), {"id": 5, "external_id": body["external_id"]})
    out = publish(again)
    assert out.action == "unchanged" and posted(again) == []


def test_a_changed_verdict_is_posted_even_if_an_older_run_exists() -> None:
    fake = with_existing(fake_with_publish_routes(), {"id": 5, "external_id": "osac-ci:old"})
    assert publish(fake).action == "created"


def test_only_the_newest_existing_run_is_compared() -> None:
    first = fake_with_publish_routes()
    publish(first)
    current = posted(first)[0]["external_id"]
    fake = with_existing(
        fake_with_publish_routes(),
        {"id": 9, "external_id": "osac-ci:newer-but-different"},
        {"id": 3, "external_id": current},
    )
    assert publish(fake).action == "created"


def test_dry_run_posts_nothing_and_asks_nothing_about_existing_runs() -> None:
    fake = fake_with_publish_routes()
    out = publish(fake, dry_run=True)
    assert out.action == "dry-run" and posted(fake) == []


def test_a_pr_that_cannot_be_read_fully_gets_a_visible_failure_not_a_pass() -> None:
    fake = fake_with_publish_routes()
    fake.add("GET", f"{BASE}/pulls/7/reviews", {"message": "boom"}, status=500)
    out = publish(fake)
    (body,) = posted(fake)
    assert out.state.value == "planner-error" and body["conclusion"] == "failure"
    assert "could not read PR #7" in body["output"]["summary"]


def test_a_rejected_post_is_an_error_not_silently_ignored() -> None:
    fake = standard_fake()
    fake.add("POST", f"{BASE}/check-runs", {"message": "Resource not accessible"}, status=403)
    with pytest.raises(GitHubError, match="403"):
        publish(fake)


def test_a_missing_pr_is_an_error() -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/pulls/7", {"message": "Not Found"}, status=404)
    with pytest.raises(GitHubError):
        publish(fake)


def test_the_sweep_publishes_for_every_open_pr_and_survives_one_failing() -> None:
    fake = fake_with_publish_routes()
    fake.add(
        "GET",
        f"{BASE}/pulls",
        [{"number": 7, "draft": False, "head": {"sha": SHA}}, {"number": 8, "head": {"sha": "e" * 40}}],
    )
    outcomes = sweep(fake, READY, REPO, org="example")
    assert {(o.pr, o.action) for o in outcomes} == {(7, "created"), (8, "failed")}
    text = describe(outcomes)
    assert "PR #7: created success (ready-to-enqueue)" in text and "2 PRs, 1 failed" in text


def test_the_sweep_includes_drafts_so_they_show_what_they_wait_for() -> None:
    fake = fake_with_publish_routes()
    fake.add("GET", f"{BASE}/pulls", [{"number": 7, "draft": True, "head": {"sha": SHA}}])
    assert [o.pr for o in sweep(fake, READY, REPO, org="example", dry_run=True)] == [7]


def test_the_sweep_respects_a_limit() -> None:
    fake = fake_with_publish_routes()
    fake.add("GET", f"{BASE}/pulls", [{"number": 7, "head": {"sha": SHA}}, {"number": 8, "head": {"sha": SHA}}])
    assert [o.pr for o in sweep(fake, READY, REPO, org="example", dry_run=True, limit=1)] == [7]


# ---- the command line -----------------------------------------------------------------------------------------

POLICY_ARGS = ["--policy", "policy/toy.yml", "--repo", REPO, "--org", "example"]


def run_cli(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, *args: str) -> int:
    monkeypatch.setattr(cli, "build_client", lambda: fake)
    return cli.main(["publish", *POLICY_ARGS, *args])


def test_cli_publishes_one_pr(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = fake_with_publish_routes()
    assert run_cli(monkeypatch, fake, "--number", "7", "--note", "Informational") == 0
    assert "PR #7: created" in capsys.readouterr().out
    assert posted(fake)[0]["output"]["summary"].startswith("Informational")


def test_cli_dry_run_posts_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fake = fake_with_publish_routes()
    assert run_cli(monkeypatch, fake, "--number", "7", "--dry-run") == 0
    assert posted(fake) == [] and "dry-run" in capsys.readouterr().out


def test_cli_sweep_exits_1_when_any_pr_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = fake_with_publish_routes()
    fake.add("GET", f"{BASE}/pulls", [{"number": 7, "head": {"sha": SHA}}, {"number": 8, "head": {"sha": SHA}}])
    assert run_cli(monkeypatch, fake, "--all") == 1
    assert "1 failed" in capsys.readouterr().out


def test_cli_reports_an_api_error_with_exit_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = standard_fake()
    fake.add("GET", f"{BASE}/pulls/7", {"message": "Not Found"}, status=404)
    assert run_cli(monkeypatch, fake, "--number", "7") == 3
    assert "publish failed" in capsys.readouterr().err


def test_cli_needs_a_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "build_client", lambda: standard_fake())
    with pytest.raises(SystemExit):
        cli.main(["publish", *POLICY_ARGS])


def test_a_missing_org_token_that_is_required_becomes_a_visible_failure_not_a_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(cli.ORG_TOKEN_ENV, raising=False)
    fake = fake_with_publish_routes()
    code = run_cli(monkeypatch, fake, "--number", "7", "--lookup-membership", "--require-org-token")
    (body,) = posted(fake)
    assert code == 0 and "planner-error" in capsys.readouterr().out
    assert body["conclusion"] == "failure" and "OSAC_CI_ORG_TOKEN is not available" in body["output"]["summary"]
    assert not [c for c in fake.calls if c[1].startswith("/orgs/")]  # the main token never asked about the org


def test_without_the_require_flag_the_main_client_is_used_for_org_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(cli.ORG_TOKEN_ENV, raising=False)
    assert cli.build_org_client() is None
    assert isinstance(cli.build_org_client(required=True), cli._NoOrgToken)  # type: ignore[attr-defined]
    monkeypatch.setenv(cli.ORG_TOKEN_ENV, "x")
    assert isinstance(cli.build_org_client(required=True), cli.HttpClient)


def test_a_failing_check_listing_does_not_hide_the_fail_closed_verdict() -> None:
    # Reading the PR already failed (so the verdict is a planner-error), and so does the lookup of the existing run:
    # there is nothing to compare with, so the failure is posted anyway instead of the job dying before it.
    fake = fake_with_publish_routes()
    fake.add("GET", CHECKS, {"message": "boom"}, status=500)
    out = publish(fake)
    (body,) = posted(fake)
    assert out.action == "created" and out.state.value == "planner-error" and body["conclusion"] == "failure"


# ---- the sweep budget ----------------------------------------------------------------------------------------------


def open_prs(fake: FakeGitHub, numbers: list[int]) -> None:
    """Open PRs 1..n listed newest-updated first, each readable like PR 7 with its own number."""
    fake.add("GET", f"{BASE}/pulls", [{"number": n, "head": {"sha": SHA}} for n in numbers])
    for n in numbers:
        pr = standard_fake().routes[("GET", f"{BASE}/pulls/7")][1]
        fake.add("GET", f"{BASE}/pulls/{n}", pr)
        for suffix in ("reviews", "files"):
            fake.add(
                "GET", f"{BASE}/pulls/{n}/{suffix}", standard_fake().routes[("GET", f"{BASE}/pulls/7/{suffix}")][1]
            )
        fake.add("GET", f"{BASE}/issues/{n}/events", [])
        fake.add("POST", "/graphql", {"data": {"node": {"mergeQueueEntry": None}}})


def test_select_prs_without_a_budget_takes_everything() -> None:
    from osac_ci.publish import select_prs

    prs = [{"number": n} for n in (9, 3, 7)]
    assert select_prs(prs, recent=None, rotate=None, tick=5) == prs


def test_select_prs_takes_the_most_recent_and_a_rotating_slice_of_the_rest() -> None:
    from osac_ci.publish import select_prs

    prs = [{"number": n} for n in (50, 40, 30, 20, 10, 5, 4, 3, 2, 1)]  # newest updated first
    picks = [[p["number"] for p in select_prs(prs, recent=2, rotate=3, tick=t)] for t in range(3)]
    # recent and rotated alternate, so a sweep cut short by the quota still serves both
    assert picks[0] == [50, 1, 40, 2, 3]  # the two newest, and the first slice of the others (by number)
    assert picks[1] == [50, 4, 40, 5, 10]
    assert picks[2] == [50, 20, 40, 30]  # the last slice is shorter
    assert [p["number"] for p in select_prs(prs, recent=2, rotate=3, tick=3)] == picks[0]  # and it wraps


def test_every_pr_is_covered_within_a_bounded_number_of_sweeps() -> None:
    from osac_ci.publish import select_prs

    prs = [{"number": n} for n in range(100, 0, -1)]
    seen: set[int] = set()
    sweeps = -(-(len(prs) - 10) // 15)  # ceil(others / rotate)
    for tick in range(sweeps):
        seen |= {p["number"] for p in select_prs(prs, recent=10, rotate=15, tick=tick)}
    assert seen == {p["number"] for p in prs}
    assert all(len(select_prs(prs, recent=10, rotate=15, tick=t)) <= 25 for t in range(20))  # never over the budget


@pytest.mark.parametrize(("recent", "rotate"), [(0, 0), (None, 0), (0, None), (5, 0)])
def test_select_prs_edge_budgets_never_crash_or_duplicate(recent: int | None, rotate: int | None) -> None:
    from osac_ci.publish import select_prs

    prs = [{"number": n} for n in (3, 2, 1)]
    got = [p["number"] for p in select_prs(prs, recent=recent, rotate=rotate, tick=7)]
    assert len(got) == len(set(got)) and set(got) <= {1, 2, 3}


def test_a_budgeted_sweep_publishes_only_the_selected_prs_and_says_it_is_partial() -> None:
    fake = fake_with_publish_routes()
    open_prs(fake, [7, 6, 5, 4, 3, 2, 1])
    result = sweep(fake, READY, REPO, org="example", dry_run=True, recent=2, rotate=2, tick=0)
    assert [o.pr for o in result] == [7, 1, 6, 2] and result.open_prs == 7
    assert "(this sweep covers 4 of 7 open PRs)" in describe(result)


def test_a_sweep_without_a_budget_says_nothing_about_being_partial() -> None:
    fake = fake_with_publish_routes()
    open_prs(fake, [7, 6])
    assert "this sweep covers" not in describe(sweep(fake, READY, REPO, org="example", dry_run=True))


def test_the_reserve_skips_the_prs_not_started_instead_of_failing_them() -> None:
    fake = fake_with_publish_routes()
    open_prs(fake, [7, 6, 5, 4, 3])
    fake.remaining, fake.remaining_drop_per_call = 150, 30  # a request costs 30: it runs below the reserve of 100
    result = sweep(fake, READY, REPO, org="example", dry_run=True, reserve=100, workers=1)
    actions = [o.action for o in result]
    assert actions[0] == "dry-run" and "skipped" in actions and "failed" not in actions
    assert actions == sorted(actions, key=lambda a: a == "skipped")  # once the reserve is hit, the rest are skipped
    text = describe(result)
    assert "skipped for the request reserve" in text and "requests left" in text


def test_without_a_reserve_the_quota_is_not_consulted() -> None:
    fake = fake_with_publish_routes()
    open_prs(fake, [7, 6])
    fake.remaining, fake.remaining_drop_per_call = 1, 1
    assert {o.action for o in sweep(fake, READY, REPO, org="example", dry_run=True, workers=1)} == {"dry-run"}


def test_an_unknown_quota_never_skips() -> None:
    fake = fake_with_publish_routes()
    open_prs(fake, [7, 6])
    assert fake.remaining is None
    assert {o.action for o in sweep(fake, READY, REPO, org="example", dry_run=True, reserve=10_000)} == {"dry-run"}


def test_cli_passes_the_budget_and_reports_the_requests_left(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = fake_with_publish_routes()
    open_prs(fake, [7, 6, 5, 4])
    fake.remaining = 4000
    monkeypatch.setattr(cli, "build_client", lambda: fake)
    monkeypatch.setattr(cli.time, "time", lambda: 1200.0)  # tick 2 with the default 600 s interval
    code = cli.main(
        ["publish", *POLICY_ARGS, "--all", "--dry-run", "--recent", "1", "--rotate", "1", "--reserve", "50"]
    )
    out = capsys.readouterr().out
    assert (
        code == 0 and "this sweep covers 2 of 4 open PRs" in out and "requests left in this token's window: 4000" in out
    )
    assert [line.split(":")[0] for line in out.splitlines() if line.startswith("PR #")] == [
        "PR #7",
        "PR #6",
    ]  # tick 2 of 3 slices of [4, 5, 6]


@pytest.mark.parametrize(
    ("recent", "rotate", "expected"),
    [
        (-1, 0, []),  # a negative count is zero: it must not slice from the end and keep all but the last
        (-5, -2, []),
        (0, -2, []),
        (None, -1, []),
        (-1, None, []),
        (2, -3, [4, 3]),  # the recent ones are kept, a negative rotation adds nothing and cannot divide by zero
    ],
)
def test_select_prs_treats_negative_counts_as_zero(recent: int | None, rotate: int | None, expected: list[int]) -> None:
    from osac_ci.publish import select_prs

    prs = [{"number": n} for n in (4, 3, 2, 1)]
    assert [p["number"] for p in select_prs(prs, recent=recent, rotate=rotate, tick=3)] == expected


@pytest.mark.parametrize("flag", ["--recent", "--rotate", "--reserve", "--interval", "--limit"])
def test_cli_rejects_a_negative_sweep_value_at_the_boundary(flag: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        cli.main(["publish", *POLICY_ARGS, "--all", flag, "-1"])
    assert stop.value.code == 2 and "must not be negative" in capsys.readouterr().err


def test_cli_still_takes_zero_for_every_sweep_value() -> None:
    parsed = cli._parser().parse_args(
        [
            "publish",
            *POLICY_ARGS,
            "--all",
            "--recent",
            "0",
            "--rotate",
            "0",
            "--reserve",
            "0",
            "--interval",
            "0",
            "--limit",
            "0",
        ]
    )
    assert (parsed.recent, parsed.rotate, parsed.reserve, parsed.interval, parsed.limit) == (0, 0, 0, 0, 0)


def test_a_limit_applies_after_the_selection_so_it_never_hides_a_pr_from_the_rotation() -> None:
    fake = fake_with_publish_routes()
    open_prs(fake, list(range(12, 0, -1)))  # twelve open PRs, newest first
    # 3 rotated slices of 3 from the 9 others, tick 2 is the one holding PRs 7, 8, 9: all beyond the first ten listed
    result = sweep(fake, READY, REPO, org="example", dry_run=True, recent=1, rotate=3, tick=2, limit=4)
    assert [o.pr for o in result] == [12, 7, 8, 9] and result.open_prs == 12
    assert "(this sweep covers 4 of 12 open PRs)" in describe(result)


def test_a_limit_below_the_budget_keeps_both_kinds_of_pr() -> None:
    fake = fake_with_publish_routes()
    open_prs(fake, list(range(10, 0, -1)))
    result = sweep(fake, READY, REPO, org="example", dry_run=True, recent=3, rotate=3, tick=0, limit=2)
    assert [o.pr for o in result] == [10, 1]  # one recent and one rotated, not two recent


def test_under_quota_pressure_both_kinds_of_pr_are_still_started() -> None:
    fake = fake_with_publish_routes()
    open_prs(fake, [9, 8, 7, 6, 5, 4, 3, 2, 1])
    # A PR costs 7 requests here. At 10 each, 330 left lets four PRs start above the reserve of 100, then the rest skip.
    fake.remaining, fake.remaining_drop_per_call = 330, 10
    result = sweep(fake, READY, REPO, org="example", dry_run=True, recent=3, rotate=3, tick=0, reserve=100, workers=1)
    started = [o.pr for o in result if o.action != "skipped"]
    assert started == [9, 1, 8, 2]  # recent and rotated alternate: back to back it would be [9, 8, 7] and no rotated PR
    assert any(o.action == "skipped" for o in result)


def test_cli_prints_the_quota_only_for_a_budgeted_sweep(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def run_cli(*extra: str) -> str:
        fake = fake_with_publish_routes()
        open_prs(fake, [7, 6])
        fake.remaining = 4000
        monkeypatch.setattr(cli, "build_client", lambda: fake)
        assert cli.main(["publish", *POLICY_ARGS, "--dry-run", *extra]) == 0
        return capsys.readouterr().out

    assert "requests left" not in run_cli("--number", "7")  # default output is unchanged
    assert "requests left" not in run_cli("--all")
    assert "requests left" not in run_cli("--all", "--limit", "1")
    for flags in (["--recent", "1"], ["--rotate", "1"], ["--reserve", "10"]):
        assert "requests left in this token's window: 4000" in run_cli("--all", *flags)


def test_a_small_limit_shrinks_the_rotated_slice_so_every_pr_is_still_reached() -> None:
    from osac_ci.publish import select_prs

    prs = [{"number": n} for n in range(12, 0, -1)]  # recent 3 -> 12, 11, 10; the 9 others are 1..9
    reached: set[int] = set()
    for tick in range(9):
        picked = [p["number"] for p in select_prs(prs, recent=3, rotate=3, tick=tick, limit=2)]
        assert len(picked) == 2 and picked[0] == 12  # one recent, one rotated
        reached.add(picked[1])
    assert reached == set(range(1, 10))  # truncating afterwards would only ever reach 1, 4 and 7


@pytest.mark.parametrize(
    ("recent", "rotate", "limit", "sizes"),
    [
        (3, 3, 2, (1, 1)),
        (3, 3, 3, (2, 1)),  # an odd limit gives the extra one to the recent group
        (3, 3, 6, (3, 3)),
        (3, 3, 100, (3, 3)),  # a limit above the budget changes nothing
        (5, 1, 4, (3, 1)),  # the rotated group cannot use its half, so the recent group gets the rest
        (1, 5, 4, (1, 3)),
        (3, 0, 2, (2, 0)),
        (0, 3, 2, (0, 2)),
        (3, 3, 0, (0, 0)),
    ],
)
def test_the_limit_is_shared_between_the_two_groups(
    recent: int, rotate: int, limit: int, sizes: tuple[int, int]
) -> None:
    from osac_ci.publish import select_prs

    prs = [{"number": n} for n in range(30, 0, -1)]
    picked = [p["number"] for p in select_prs(prs, recent=recent, rotate=rotate, tick=0, limit=limit)]
    recent_numbers = {p["number"] for p in prs[:recent]}
    assert (sum(n in recent_numbers for n in picked), sum(n not in recent_numbers for n in picked)) == sizes
    assert len(picked) <= limit


def test_a_limit_without_a_budget_still_caps_the_sweep() -> None:
    from osac_ci.publish import select_prs

    prs = [{"number": n} for n in (5, 4, 3, 2, 1)]
    assert select_prs(prs, recent=None, rotate=None, tick=0, limit=2) == prs[:2]
    assert select_prs(prs, recent=None, rotate=None, tick=0, limit=0) == []
    assert select_prs(prs, recent=None, rotate=None, tick=0) == prs
