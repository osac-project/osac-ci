"""Run E2E suites by hand: the decision for a request, the E2E check around it, and the control comment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from osac_ci import e2e_check, manual_run
from osac_ci.compare import Requirement
from osac_ci.e2e_start import NEEDED, Action, Inputs, Suite, decide, to_json
from osac_ci.github.api import GitHubError, Response
from osac_ci.model import CheckRun

pytestmark = pytest.mark.unit
HEAD = "a" * 40
APP = 15368
REPO = "o/r"
SUITES = (
    Suite("vmaas", "E2E / VMaaS Full Install", (), ("code",)),
    Suite("caas", "E2E / CaaS Full Install", (), ("code",)),
    Suite("netris", "E2E / Netris", (), (), manual_only=True),
)
FILTERS = {"code": ["**/*.go"]}


def line(name: str, conclusion: str | None = "success", app: str = "osac-ci") -> CheckRun:
    return CheckRun(
        name, "completed" if conclusion else "in_progress", conclusion, "2026-10-10T10:00:00Z", app=app, app_id=1
    )


def inputs(**changes: object) -> Inputs:
    base = Inputs(
        number=7,
        head_sha=HEAD,
        author="alice",
        state="open",
        draft=False,
        reviews=(),
        check_runs=(),
        changed_files=("svc/main.go",),
        changed_files_known=True,
        cheap=(Requirement("lint", APP),),
        suites=SUITES,
        filters=FILTERS,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


# the decision for a request ----------------------------------------------------------------------------------------


def test_a_request_starts_the_suite_without_an_approval_and_on_a_draft() -> None:
    d = decide(inputs(requested=("vmaas",), requested_by="bob", draft=True))
    assert d.action is Action.START and d.manual and d.suites == ("vmaas",)
    assert d.suite_checks == (("vmaas", "E2E / VMaaS Full Install"),)
    assert d.notes == (("vmaas", "started by bob"),)


def test_a_request_can_start_a_suite_that_is_only_started_by_hand() -> None:
    d = decide(inputs(requested=("netris",), requested_by="bob"))
    assert d.starts and d.suites == ("netris",)


def test_the_sum_the_e2e_stands_for_is_the_suites_the_change_needs_not_the_hand_only_ones() -> None:
    d = decide(inputs(requested=("netris",), requested_by="bob"))
    assert [name for name, _ in d.selected_checks] == ["vmaas", "caas"]


def test_a_suite_that_is_running_is_not_started_again() -> None:
    running = line("E2E / VMaaS Full Install", None)
    d = decide(inputs(requested=("vmaas", "caas"), requested_by="bob", check_runs=(running,)))
    assert d.suites == ("caas",)
    assert ("vmaas", "is already running on this commit; it was not started again") in d.notes


def test_a_suite_that_passed_is_not_run_again() -> None:
    d = decide(
        inputs(requested=("vmaas",), requested_by="bob", check_runs=(line("E2E / VMaaS Full Install", "success"),))
    )
    assert d.action is Action.NOTHING_TO_START and not d.starts
    assert d.notes == (("vmaas", "already passed on this commit; it was not run again"),)


def test_a_suite_that_failed_can_be_asked_for_again() -> None:
    d = decide(
        inputs(requested=("vmaas",), requested_by="bob", check_runs=(line("E2E / VMaaS Full Install", "failure"),))
    )
    assert d.starts and d.suites == ("vmaas",)


def test_an_unknown_suite_starts_nothing_and_the_note_lists_the_suites() -> None:
    d = decide(inputs(requested=("nope",), requested_by="bob"))
    assert d.action is Action.NOTHING_TO_START
    assert d.notes == (("nope", "is not a suite of this repository (suites: vmaas, caas, netris)"),)


def test_a_refused_request_starts_nothing_and_says_why() -> None:
    d = decide(inputs(request_refused="@eve is not a member of the o organization, so nothing was started"))
    assert d.action is Action.NOTHING_TO_START and not d.starts and "eve" in d.reason


def test_a_closed_pull_request_starts_nothing_even_when_asked() -> None:
    assert decide(inputs(requested=("vmaas",), requested_by="bob", state="closed")).action is Action.SKIP_CLOSED


def test_the_automatic_decision_never_chooses_a_hand_only_suite() -> None:
    d = decide(inputs(reviews=()))
    assert "netris" not in d.suites and "netris" not in [n for n, _ in d.selected_checks]


# suites that passed by hand are not run again by the approval ------------------------------------------------------

from osac_ci.e2e_start import Review  # noqa: E402


def approved() -> tuple[Review, ...]:
    return (Review("bob", "APPROVED", "MEMBER", HEAD, permission="write"),)


def test_the_approval_runs_only_what_has_not_passed() -> None:
    d = decide(inputs(reviews=approved(), check_runs=(line("E2E / VMaaS Full Install", "success"),)))
    assert d.starts and d.suites == ("caas",)


def test_when_every_needed_suite_passed_by_hand_the_e2e_is_done() -> None:
    both = (line("E2E / VMaaS Full Install"), line("E2E / CaaS Full Install"))
    d = decide(inputs(reviews=approved(), check_runs=both))
    assert d.action is Action.ALREADY_PASSED and not d.starts


def test_a_rerun_of_a_finished_run_still_runs_everything() -> None:
    both = (line("E2E / VMaaS Full Install"), line("E2E / CaaS Full Install"), line("E2E"))
    d = decide(inputs(reviews=approved(), check_runs=both))
    assert d.starts and d.suites == ("vmaas", "caas")


# the E2E check around a run started by hand ------------------------------------------------------------------------


def entry(**changes: Any) -> dict[str, Any]:
    d = decide(inputs(requested=("vmaas",), requested_by="bob"))
    import json

    return {**json.loads(to_json(d)), **changes}


def test_a_run_started_by_hand_does_not_claim_the_e2e_check() -> None:
    out = e2e_check.plan(entry(), None, 500, lambda rid: False)
    assert out.start and out.writes == () and out.state == "running"


def test_already_passed_writes_the_sum_when_the_e2e_still_waits() -> None:
    d = decide(
        inputs(reviews=approved(), check_runs=(line("E2E / VMaaS Full Install"), line("E2E / CaaS Full Install")))
    )
    import json

    out = e2e_check.plan(json.loads(to_json(d)), None, 500, lambda rid: False)
    assert [w.state for w in out.writes] == ["success"] and "vmaas, caas" in out.writes[0].description


class Fake:
    """A GitHub with check runs and the comments of one pull request."""

    def __init__(
        self, runs: list[dict[str, Any]], comments: list[dict[str, Any]] | None = None, pr: dict[str, Any] | None = None
    ):
        self.runs = runs
        self.comments = comments or []
        self.pr = pr or {"state": "open", "user": {"login": "alice"}, "head": {"sha": HEAD}}
        self.members = {"alice", "bob"}
        self.posted: list[tuple[str, str, Any]] = []
        self.next_id = 9
        self.early: dict[str, Any] | None = None  # a control comment another run posts while this one creates its own
        self.delete_status = 204
        self.patch_status = 200
        self.reviews: list[dict[str, Any]] = []

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None, **_: Any) -> Response:
        self.posted.append((method, path, body))
        if method == "GET" and path.startswith("/orgs/o/members/"):
            return Response(204 if path.rsplit("/", 1)[1] in self.members else 404, {}, {})
        if method == "GET" and path.endswith("/check-runs") or "/check-runs?" in path:
            return Response(200, {"check_runs": self.runs}, {})
        if method == "GET" and "/issues/" in path and "/comments" in path:
            return Response(200, self.comments, {})
        if method == "GET" and path == f"/repos/{REPO}/pulls/7":
            return Response(200, self.pr, {})
        if method == "GET" and path == f"/repos/{REPO}/pulls/7/reviews":
            return Response(200, self.reviews, {})
        if method == "POST" and path.endswith("/check-runs"):
            self.runs.append({"name": body["name"], "status": body.get("status"), "conclusion": body.get("conclusion")})  # type: ignore[index]
            return Response(201, {}, {})
        if method == "POST" and path.endswith("/comments"):
            if self.early is not None:
                self.comments.append(self.early)
                self.early = None
            created = self.next_id
            self.next_id += 1
            self.comments.append({"id": created, "user": {"login": "osac-ci[bot]"}, "body": body["body"]})  # type: ignore[index]
            return Response(201, {"id": created}, {})
        if method == "PATCH" and "/issues/comments/" in path:
            wanted = int(path.rsplit("/", 1)[1])
            if self.patch_status == 200:
                next(c for c in self.comments if c["id"] == wanted)["body"] = body["body"]  # type: ignore[index]
            return Response(self.patch_status, {}, {})
        if method == "DELETE" and "/issues/comments/" in path:
            wanted = int(path.rsplit("/", 1)[1])
            if self.delete_status == 204:
                self.comments = [c for c in self.comments if c["id"] != wanted]
            return Response(self.delete_status, {}, {})
        raise AssertionError(f"unexpected request {method} {path}")


def passed(name: str, app: str = "osac-ci") -> dict[str, Any]:
    return {
        "name": name, "status": "completed", "conclusion": "success", "started_at": "2026-10-10T10:00:00Z",
        "app": {"slug": app},
    }  # fmt: skip


def test_the_sum_is_written_only_when_every_needed_suite_passed() -> None:
    lines = {"vmaas": "E2E / VMaaS Full Install", "caas": "E2E / CaaS Full Install"}
    partial = Fake([passed("E2E / VMaaS Full Install")])
    note = e2e_check.finish_by_hand(partial, REPO, HEAD, lines, "", "checks")  # type: ignore[arg-type]
    assert "left as it is" in note and not [p for p in partial.posted if p[0] == "POST"]
    full = Fake([passed("E2E / VMaaS Full Install"), passed("E2E / CaaS Full Install")])
    note = e2e_check.finish_by_hand(full, REPO, HEAD, lines, "", "checks")  # type: ignore[arg-type]
    assert "green" in note
    assert any(p[0] == "POST" and p[2]["name"] == "E2E" and p[2]["conclusion"] == "success" for p in full.posted)


def test_a_failed_suite_never_turns_the_sum_green() -> None:
    lines = {"vmaas": "E2E / VMaaS Full Install"}
    fake = Fake([{**passed("E2E / VMaaS Full Install"), "conclusion": "failure"}])
    assert "left as it is" in e2e_check.finish_by_hand(fake, REPO, HEAD, lines, "", "checks")  # type: ignore[arg-type]


def test_runs_by_hand_need_check_runs() -> None:
    with pytest.raises(ValueError, match="check runs"):
        e2e_check.finish_by_hand(Fake([]), REPO, HEAD, {"vmaas": "E2E / V"}, "", "statuses")  # type: ignore[arg-type]


# the control comment -----------------------------------------------------------------------------------------------


def views() -> tuple[manual_run.SuiteView, ...]:
    return (
        manual_run.SuiteView("vmaas", "E2E / VMaaS Full Install", "passed", "https://x/run/1"),
        manual_run.SuiteView("caas", "E2E / CaaS Full Install", "none"),
    )


def test_the_comment_lists_every_suite_with_an_unchecked_box() -> None:
    body = manual_run.render_comment(HEAD, views())
    assert body.startswith(manual_run.MARKER) and "<details>" in body and "> [!TIP]" in body
    assert "- [ ] <!-- suite:vmaas --> **VMaaS Full Install** · ✅ passed ([run](https://x/run/1))" in body
    assert manual_run.requested_suites(body) == ()


def test_the_e2e_section_names_both_ways_to_start_and_the_status_comes_first() -> None:
    body = manual_run.render_comment(HEAD, views())
    status, section = body.split("<details>")
    assert "## 🚦 CI status" in status and "## 🧪" not in status
    for part in ("**Who:**", "**How to start a suite:**", "**Good to know:**", "`/test`", "`/test <suite>`", HEAD[:7]):
        assert part in section
    assert "<summary><b>🧪 E2E tests</b> · ✅ 1 passed · ➖ 1 not run · run them now (2 available)</summary>" in section
    assert max(len(line) for line in body.splitlines() if line.startswith("> ")) < 200  # no wall of text


def test_checked_boxes_are_read_in_order() -> None:
    body = manual_run.render_comment(HEAD, views()).replace("- [ ] <!-- suite:caas", "- [x] <!-- suite:caas")
    body = body.replace("- [ ] <!-- suite:vmaas", "- [X] <!-- suite:vmaas")
    assert manual_run.requested_suites(body) == ("vmaas", "caas")


def test_a_box_written_by_hand_in_the_text_is_not_a_request() -> None:
    assert manual_run.requested_suites("- [x] caas\n- [x] <!-- suite:Caas -->") == ()


def test_the_last_answer_survives_a_refresh() -> None:
    body = manual_run.render_comment(HEAD, views(), "**Last request by @bob**\n- **vmaas** started by bob")
    assert manual_run.notes_of(body) == "**Last request by @bob**\n- **vmaas** started by bob"
    assert "> [!NOTE]" in body


def test_only_the_app_writes_the_control_comment() -> None:
    ok = {"user": {"login": "osac-ci[bot]"}, "body": manual_run.MARKER}
    assert manual_run.is_control_comment(ok)
    assert not manual_run.is_control_comment({"user": {"login": "eve"}, "body": manual_run.MARKER})
    assert not manual_run.is_control_comment({"user": {"login": "osac-ci[bot]"}, "body": "hello"})


def comment_api(body: str, comment_id: int = 9, issue: int = 7, author: str = "osac-ci[bot]") -> Fake:
    fake = Fake([])

    def request(method: str, path: str, body_: Any = None, **_: Any) -> Response:
        if method == "GET" and path == f"/repos/{REPO}/issues/comments/{comment_id}":
            return Response(
                200,
                {
                    "id": comment_id,
                    "user": {"login": author},
                    "body": body,
                    "issue_url": f"https://api/x/issues/{issue}",
                },
                {},
            )
        return Fake.request(fake, method, path, body_)

    fake.request = request  # type: ignore[method-assign]
    return fake


def checked(suite: str) -> str:
    return manual_run.render_comment(HEAD, views()).replace(f"- [ ] <!-- suite:{suite}", f"- [x] <!-- suite:{suite}")


def test_a_member_checking_a_box_asks_for_that_suite() -> None:
    got = manual_run.read_request(comment_api(checked("caas")), REPO, 7, 9, "bob")  # type: ignore[arg-type]
    assert got == (("caas",), "", HEAD)


def test_anyone_in_the_organization_may_ask_even_for_their_own_pull_request() -> None:
    fake = comment_api(checked("caas"))
    assert manual_run.read_request(fake, REPO, 7, 9, "alice") == (("caas",), "", HEAD)  # type: ignore[arg-type]


def test_someone_outside_the_organization_is_refused_and_nothing_starts() -> None:
    asked, refused, _ = manual_run.read_request(comment_api(checked("caas")), REPO, 7, 9, "eve")  # type: ignore[arg-type]
    assert asked == () and "not a member" in refused


@pytest.mark.parametrize(
    "api",
    [
        comment_api(checked("caas"), issue=8),  # the control comment of another pull request
        comment_api(checked("caas"), author="eve"),  # a comment of someone else with the marker
        comment_api("no marker here"),
    ],
)
def test_a_comment_that_is_not_this_pull_requests_control_comment_asks_for_nothing(api: Fake) -> None:
    assert manual_run.read_request(api, REPO, 7, 9, "bob") == ((), "", "")  # type: ignore[arg-type]


def test_an_edit_that_unchecks_a_box_asks_for_nothing() -> None:
    assert manual_run.read_request(comment_api(manual_run.render_comment(HEAD, views())), REPO, 7, 9, "bob") == (
        (),
        "",
        "",
    )  # type: ignore[arg-type]


# creating and refreshing the comment -------------------------------------------------------------------------------

from osac_ci.policy import parse_policy  # noqa: E402

POLICY_TEXT = """
version: 1
repo: o/r
path_filters:
  mode: enforce
  filters:
    code: ['**']
jobs:
  e2e-vmaas:
    kind: e2e
    check: "E2E / VMaaS Full Install"
    suite: vmaas
    filters_any: [code]
  e2e-netris:
    kind: e2e
    check: "E2E / Netris"
    suite: netris
    manual_only: true
"""
POLICY = parse_policy(POLICY_TEXT)


def test_the_comment_is_created_for_a_member_s_pull_request_and_lists_every_suite() -> None:
    fake = Fake([passed("E2E / VMaaS Full Install")])
    assert manual_run.refresh(fake, POLICY, REPO, 7) == "created"  # type: ignore[arg-type]
    body = fake.comments[0]["body"]
    assert "suite:vmaas" in body and "suite:netris" in body and "VMaaS Full Install** · ✅ passed" in body


def test_a_second_refresh_with_nothing_new_writes_nothing() -> None:
    fake = Fake([])
    manual_run.refresh(fake, POLICY, REPO, 7)  # type: ignore[arg-type]
    before = len([p for p in fake.posted if p[0] in ("POST", "PATCH")])
    assert manual_run.refresh(fake, POLICY, REPO, 7) == "unchanged"  # type: ignore[arg-type]
    assert len([p for p in fake.posted if p[0] in ("POST", "PATCH")]) == before


def test_a_changed_line_updates_the_comment_and_keeps_the_last_answer() -> None:
    fake = Fake([])
    manual_run.refresh(fake, POLICY, REPO, 7, notes="Last request by @bob:\n- **vmaas** started by bob")  # type: ignore[arg-type]
    fake.runs.append(passed("E2E / VMaaS Full Install"))
    assert manual_run.refresh(fake, POLICY, REPO, 7) == "updated"  # type: ignore[arg-type]
    body = fake.comments[0]["body"]
    assert "VMaaS Full Install** · ✅ passed" in body and "started by bob" in body


def test_an_answer_replaces_the_old_one() -> None:
    fake = Fake([])
    manual_run.refresh(fake, POLICY, REPO, 7, notes="old answer")  # type: ignore[arg-type]
    manual_run.refresh(fake, POLICY, REPO, 7, notes="new answer")  # type: ignore[arg-type]
    assert "new answer" in fake.comments[0]["body"] and "old answer" not in fake.comments[0]["body"]


def test_no_comment_is_made_for_a_pull_request_of_someone_outside_the_organization() -> None:
    fake = Fake([], pr={"state": "open", "user": {"login": "eve"}, "head": {"sha": HEAD}})
    assert manual_run.refresh(fake, POLICY, REPO, 7).startswith("skipped")  # type: ignore[arg-type]
    assert fake.comments == []


def test_no_comment_is_made_for_a_closed_pull_request() -> None:
    fake = Fake([], pr={"state": "closed", "user": {"login": "alice"}, "head": {"sha": HEAD}})
    assert manual_run.refresh(fake, POLICY, REPO, 7).startswith("skipped")  # type: ignore[arg-type]


def test_a_draft_pull_request_gets_the_comment_too() -> None:
    fake = Fake([], pr={"state": "open", "draft": True, "user": {"login": "alice"}, "head": {"sha": HEAD}})
    assert manual_run.refresh(fake, POLICY, REPO, 7) == "created"  # type: ignore[arg-type]


# the command line ---------------------------------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from osac_ci import cli  # noqa: E402


def test_the_comment_id_and_the_person_go_together(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    policy = tmp_path / "policy.yml"
    policy.write_text(POLICY_TEXT, encoding="utf-8")
    assert cli.main(["e2e-decide", "--policy", str(policy), "--repo", REPO, "--number", "7", "--manual-by", "bob"]) == 2
    assert "go together" in capsys.readouterr().err
    assert (
        cli.main(["e2e-decide", "--policy", str(policy), "--repo", REPO, "--number", "7", "--manual-comment-id", "9"])
        == 2
    )


def test_a_request_by_hand_needs_a_pull_request_number(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    policy = tmp_path / "policy.yml"
    policy.write_text(POLICY_TEXT, encoding="utf-8")
    argv = ["e2e-decide", "--policy", str(policy), "--repo", REPO, "--head-sha", HEAD]
    assert cli.main([*argv, "--manual-comment-id", "9", "--manual-by", "bob"]) == 2
    assert "--number" in capsys.readouterr().err


# the review findings -------------------------------------------------------------------------------------------------


def test_a_check_of_another_app_with_a_suite_s_name_does_not_make_it_passed_or_running() -> None:
    other = (line("E2E / VMaaS Full Install", "success", app="someone-else"),)
    d = decide(inputs(requested=("vmaas",), requested_by="bob", check_runs=other))
    assert d.starts and d.suites == ("vmaas",)
    d = decide(inputs(reviews=approved(), check_runs=(line("E2E / VMaaS Full Install", "success", app="x"),)))
    assert d.starts and d.suites == ("vmaas", "caas")  # not "already passed"


def test_a_check_of_another_app_does_not_make_the_sum_green() -> None:
    lines = {"vmaas": "E2E / VMaaS Full Install"}
    fake = Fake([passed("E2E / VMaaS Full Install", app="someone-else")])
    assert "left as it is" in e2e_check.finish_by_hand(fake, REPO, HEAD, lines, "", "checks")  # type: ignore[arg-type]


def test_the_comment_does_not_show_another_app_s_check_as_the_suite() -> None:
    runs = [passed("E2E / VMaaS Full Install", app="someone-else")]
    assert manual_run.views_for(SUITES, runs)[0].state == "none"
    assert manual_run.views_for(SUITES, [passed("E2E / VMaaS Full Install")])[0].state == "passed"


def existing(state: str, external_id: str = "") -> e2e_check.Existing:
    return e2e_check.Existing(state, "x", external_id)


@pytest.mark.parametrize(
    ("current", "ok"),
    [
        (None, True),
        (existing("pending", "waiting"), True),  # waiting
        (existing("failure"), True),  # a run that failed does not stop the suites passed by hand
        (existing("error"), True),
        (existing("success"), False),  # a pass stays
        (existing("pending", "run:500"), False),  # a run that is running stays
    ],
)
def test_the_sum_may_replace_a_wait_or_a_failure_but_not_a_pass_or_a_running_run(
    current: e2e_check.Existing | None, ok: bool
) -> None:
    assert e2e_check.replaceable(current) is ok
    entry = {"action": "already-passed", "selected_checks": {"vmaas": "E2E / VMaaS Full Install"}}
    assert bool(e2e_check.plan(entry, current, 1, lambda rid: True).writes) is ok


def test_a_failed_result_is_replaced_when_every_suite_has_passed_by_hand() -> None:
    lines = {"vmaas": "E2E / VMaaS Full Install"}
    fake = Fake(
        [
            passed("E2E / VMaaS Full Install"),
            {
                "name": "E2E",
                "status": "completed",
                "conclusion": "failure",
                "id": 1,
                "app": {"slug": "osac-ci"},
                "output": {"title": "Failed: vmaas"},
            },
        ]
    )
    assert "green" in e2e_check.finish_by_hand(fake, REPO, HEAD, lines, "", "checks")  # type: ignore[arg-type]


def test_a_comment_written_for_an_older_commit_cannot_start_anything() -> None:
    from osac_ci.e2e_start import refuse_stale_request

    old = "b" * 40
    asked, refused = refuse_stale_request(("vmaas",), "", old, HEAD)
    assert asked == () and "changed since the comment" in refused and "bbbbbbb is now aaaaaaa" in refused
    d = decide(inputs(requested=asked, request_refused=refused))
    assert d.action is Action.NOTHING_TO_START and not d.starts


def test_a_comment_for_the_current_commit_or_with_no_commit_is_not_refused() -> None:
    from osac_ci.e2e_start import refuse_stale_request

    assert refuse_stale_request(("vmaas",), "", HEAD, HEAD) == (("vmaas",), "")
    assert refuse_stale_request(("vmaas",), "", "", HEAD) == (("vmaas",), "")
    assert refuse_stale_request((), "", "b" * 40, HEAD) == ((), "")


def test_the_head_commit_is_in_the_comment_and_read_back() -> None:
    body = manual_run.render_comment(HEAD, views())
    assert f"<!-- head:{HEAD} -->" in body and manual_run.head_of(body) == HEAD
    assert manual_run.head_of("no marker") == ""


def test_two_comments_created_at_once_leave_the_oldest_filled_in() -> None:
    placeholder = {"id": 3, "user": {"login": "osac-ci[bot]"}, "body": f"{manual_run.MARKER}\nloading"}
    fake = Fake([passed("E2E / VMaaS Full Install")])
    fake.early = placeholder  # the early job posts its placeholder between our look and our POST
    assert manual_run.refresh(fake, POLICY, REPO, 7).startswith("created (kept the older")  # type: ignore[arg-type]
    assert [c["id"] for c in fake.comments] == [3]  # ours was deleted, the older one has the place
    assert "VMaaS Full Install** · ✅ passed" in fake.comments[0]["body"]  # and it was filled in


def test_a_comment_created_alone_is_kept() -> None:
    fake = Fake([])
    assert manual_run.refresh(fake, POLICY, REPO, 7) == "created"  # type: ignore[arg-type]
    assert len(fake.comments) == 1 and not [p for p in fake.posted if p[0] == "DELETE"]


def test_a_duplicate_that_cannot_be_deleted_fails_before_anything_is_updated() -> None:
    placeholder = {"id": 3, "user": {"login": "osac-ci[bot]"}, "body": f"{manual_run.MARKER}\nloading"}
    fake = Fake([passed("E2E / VMaaS Full Install")])
    fake.early = placeholder
    fake.delete_status = 403
    with pytest.raises(GitHubError, match="duplicate"):
        manual_run.refresh(fake, POLICY, REPO, 7)  # type: ignore[arg-type]
    assert fake.comments[0]["body"].endswith("loading")  # the older comment was not touched
    assert not [p for p in fake.posted if p[0] == "PATCH"]


def test_a_duplicate_that_is_already_gone_is_not_an_error() -> None:
    placeholder = {"id": 3, "user": {"login": "osac-ci[bot]"}, "body": f"{manual_run.MARKER}\nloading"}
    fake = Fake([passed("E2E / VMaaS Full Install")])
    fake.early = placeholder
    fake.delete_status = 404
    assert manual_run.refresh(fake, POLICY, REPO, 7).startswith("created (kept")  # type: ignore[arg-type]


# a /test comment ---------------------------------------------------------------------------------------------------


def test_a_bare_command_starts_the_suites_the_change_needs_not_the_hand_only_ones() -> None:
    d = decide(inputs(requested=(NEEDED,), requested_by="bob"))
    assert d.starts and d.manual and d.suites == ("vmaas", "caas")
    assert d.notes == (("vmaas", "started by bob"), ("caas", "started by bob"))


def test_a_bare_command_skips_what_passed_or_runs_and_so_retries_a_failure() -> None:
    runs = (line("E2E / VMaaS Full Install", "success"), line("E2E / CaaS Full Install", "failure"))
    d = decide(inputs(requested=(NEEDED,), requested_by="bob", check_runs=runs))
    assert d.suites == ("caas",) and ("vmaas", "already passed on this commit; it was not run again") in d.notes


def test_a_bare_command_on_a_docs_only_change_says_so_and_starts_nothing() -> None:
    d = decide(inputs(requested=(NEEDED,), requested_by="bob", changed_files=("README.md",)))
    assert d.action is Action.NOTHING_TO_START and "docs only" in d.reason


def test_a_command_works_on_a_draft_without_an_approval() -> None:
    assert decide(inputs(requested=(NEEDED,), requested_by="bob", draft=True)).starts


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("/test", (NEEDED,)),
        ("/test  ", (NEEDED,)),
        ("/test vmaas", ("vmaas",)),
        ("/test vmaas caas\nplease", ("vmaas", "caas")),
        ("/test\tvmaas\r\nthanks", ("vmaas",)),
        ("/test vmaas vmaas", ("vmaas",)),
        ("/test bmaas-netris_2", ("bmaas-netris_2",)),
    ],
)
def test_the_command_names_the_suites_or_asks_for_the_ones_the_change_needs(
    body: str, expected: tuple[str, ...]
) -> None:
    assert manual_run.parse_command(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        "",
        "hello\n/test vmaas",  # not on the first line
        "> /test vmaas",  # a quote
        "```\n/test vmaas\n```",  # a code fence
        " /test vmaas",  # indented
        "please /test vmaas",  # a sentence
        "/testing",
        "/test-all",
        "/test VMAAS",
        "/test vmaas, caas",
        "/test @needed",
        "/test vmaas && rm",
        "/test " + " ".join(["a"] * 11),  # more names than any repository has
        "/retest",
        "/test ?",
        "<!-- osac-ci:manual-run -->\n/test vmaas",
    ],
)
def test_anything_but_the_exact_command_is_not_a_request(body: str) -> None:
    assert manual_run.parse_command(body) is None


def command_api(body: str, *, author: str = "bob", kind: str = "User", issue: int = 7) -> Fake:
    fake = comment_api(body, author=author, issue=issue)
    inner = fake.request

    def request(method: str, path: str, body_: Any = None, **kw: Any) -> Response:
        response = inner(method, path, body_, **kw)
        if method == "GET" and path == f"/repos/{REPO}/issues/comments/9":
            response.data["user"]["type"] = kind
        return response

    fake.request = request  # type: ignore[method-assign]
    return fake


def test_a_member_s_command_asks_for_the_suites_it_names_and_is_not_tied_to_a_commit() -> None:
    got = manual_run.read_request(command_api("/test caas"), REPO, 7, 9, "bob")  # type: ignore[arg-type]
    assert got == (("caas",), "", "")


def test_a_bare_command_asks_for_the_suites_the_change_needs() -> None:
    assert manual_run.read_request(command_api("/test"), REPO, 7, 9, "bob") == ((NEEDED,), "", "")  # type: ignore[arg-type]


def test_the_owner_of_the_pull_request_may_use_the_command_when_a_member() -> None:
    api = command_api("/test vmaas", author="alice")
    assert manual_run.read_request(api, REPO, 7, 9, "alice") == (("vmaas",), "", "")  # type: ignore[arg-type]


def test_someone_outside_the_organization_is_refused_and_nothing_starts_by_command() -> None:
    api = command_api("/test vmaas", author="eve")
    asked, refused, _ = manual_run.read_request(api, REPO, 7, 9, "eve")  # type: ignore[arg-type]
    assert asked == () and "not a member" in refused


@pytest.mark.parametrize(
    "api",
    [
        command_api("/test vmaas", kind="Bot"),  # an app or a bot never asks, even a member-named one
        command_api("/test vmaas", author="carol"),  # the event's sender is not the comment's author
        command_api("/test vmaas", issue=8),  # a comment of another issue
        command_api("just a comment"),
    ],
)
def test_a_comment_that_is_not_a_person_s_own_command_asks_for_nothing(api: Fake) -> None:
    assert manual_run.read_request(api, REPO, 7, 9, "bob") == ((), "", "")  # type: ignore[arg-type]


def test_a_command_inside_the_control_comment_text_of_someone_else_is_not_read_as_boxes() -> None:
    # A person who copies the control comment (marker and checked box) is neither the app nor a command.
    api = command_api(checked("caas"), author="bob")
    assert manual_run.read_request(api, REPO, 7, 9, "bob") == ((), "", "")  # type: ignore[arg-type]


# the status block ---------------------------------------------------------------------------------------------------


def actions_check(
    name: str, conclusion: str | None = "success", started: str = "2026-10-10T10:00:00Z"
) -> dict[str, Any]:
    return {
        "name": name, "status": "completed" if conclusion else "in_progress", "conclusion": conclusion,
        "started_at": started, "app": {"slug": "github-actions"}, "details_url": f"https://x/{name.replace(' ', '-')}",
    }  # fmt: skip


def app_check(name: str, conclusion: str | None, title: str = "") -> dict[str, Any]:
    return {**passed(name), "conclusion": conclusion, "status": "completed" if conclusion else "in_progress",
            "output": {"title": title}, "details_url": "https://x/e2e"}  # fmt: skip


def review(login: str, state: str, commit: str = HEAD, at: str = "2026-10-10T10:00:00Z") -> dict[str, Any]:
    return {"user": {"login": login}, "state": state, "commit_id": commit, "submitted_at": at}


OLD = "b" * 40


@pytest.mark.parametrize(
    ("runs", "state"),
    [
        ([actions_check("CI")], "passed"),
        ([actions_check("CI", None)], "active"),
        ([actions_check("CI", "failure")], "failed"),
        ([actions_check("CI", "timed_out")], "failed"),
        ([actions_check("CI", "cancelled")], "cancelled"),
    ],
)
def test_a_check_state_is_read_from_its_newest_run(runs: list[dict[str, Any]], state: str) -> None:
    view = manual_run.check_view(runs, "CI", "github-actions")
    assert view is not None and view.state == state and view.url == "https://x/CI"


def test_the_newest_run_of_a_check_wins() -> None:
    runs = [
        actions_check("CI", "failure", "2026-10-10T10:00:00Z"),
        actions_check("CI", "success", "2026-10-10T11:00:00Z"),
    ]
    view = manual_run.check_view(runs, "CI", "github-actions")
    assert view is not None and view.state == "passed"


@pytest.mark.parametrize(
    "runs",
    [
        [],
        [actions_check("lint")],
        [actions_check("CI", "skipped")],
        [actions_check("CI", "neutral")],
        [{**actions_check("CI"), "app": {"slug": "someone-else"}}],  # another app's check of that name says nothing
        [{**actions_check("CI"), "app": None}],
    ],
)
def test_a_commit_with_no_usable_check_of_that_name_has_no_view(runs: list[dict[str, Any]]) -> None:
    assert manual_run.check_view(runs, "CI", "github-actions") is None


def test_the_e2e_check_keeps_what_it_says_even_when_it_neither_passed_nor_failed() -> None:
    view = manual_run.check_view(
        [app_check("E2E", "neutral", "Waiting: approval needed")], "E2E", "osac-ci", keep_other=True
    )
    assert view == manual_run.CheckView("other", "https://x/e2e", "Waiting: approval needed")
    waiting = manual_run.check_view([app_check("E2E", None, "Waiting: approval")], "E2E", "osac-ci", keep_other=True)
    assert waiting is not None and waiting.state == "active" and waiting.title == "Waiting: approval"


def test_only_the_e2e_check_shows_its_title_the_title_of_an_actions_job_is_just_its_name() -> None:
    run = {**actions_check("CI"), "output": {"title": "CI"}}
    view = manual_run.check_view([run], "CI", "github-actions")
    assert view == manual_run.CheckView("passed", "https://x/CI")


def test_a_failed_ci_names_the_checks_that_failed_but_not_the_e2e_lines_or_a_check_that_passed_since() -> None:
    runs = [
        actions_check("CI", "failure"),
        actions_check("Unit tests", "failure"),
        actions_check("Lint", "failure", "2026-10-10T09:00:00Z"),
        actions_check("Lint", "success", "2026-10-10T10:30:00Z"),  # failed first, passed on the newest run
        actions_check("Integration test", "timed_out"),
        actions_check("Docs", "skipped"),
        passed("E2E / VMaaS Full Install") | {"conclusion": "failure"},
        actions_check("E2E", "failure"),
        {**actions_check("Snyk", "failure"), "app": {"slug": "snyk"}},
        {**actions_check("Netris suite", "failure"), "app": {"slug": "osac-ci"}},  # a line of this app is no check
    ]
    view = manual_run.check_view(runs, "CI", "github-actions", with_failed=True)
    assert view is not None and view.failed == ("Integration test", "Snyk", "Unit tests")
    passing = manual_run.check_view(
        [actions_check("CI"), actions_check("Optional", "failure")], "CI", "github-actions", with_failed=True
    )
    assert passing is not None and passing.failed == ()


@pytest.mark.parametrize(
    ("reviews", "kind", "who"),
    [
        ([], "waiting", ()),
        ([review("a", "COMMENTED")], "waiting", ()),
        ([review("a", "APPROVED")], "approved", ("a",)),
        ([review("b", "APPROVED"), review("a", "APPROVED")], "approved", ("a", "b")),
        ([review("a", "APPROVED", OLD)], "older", ("a",)),
        ([review("a", "APPROVED", OLD), review("b", "APPROVED")], "approved", ("b",)),
        ([review("a", "APPROVED"), review("a", "DISMISSED", at="2026-10-10T11:00:00Z")], "waiting", ()),
        ([review("a", "APPROVED"), review("a", "COMMENTED", at="2026-10-10T11:00:00Z")], "approved", ("a",)),
        ([review("a", "CHANGES_REQUESTED"), review("a", "APPROVED", at="2026-10-10T11:00:00Z")], "approved", ("a",)),
        ([review("a", "APPROVED"), review("a", "CHANGES_REQUESTED", at="2026-10-10T11:00:00Z")], "changes", ("a",)),
        ([review("a", "APPROVED"), review("b", "CHANGES_REQUESTED")], "changes", ("b",)),
        ([review("a", "PENDING")], "waiting", ()),
        ([{"user": None, "state": "APPROVED", "commit_id": HEAD}], "waiting", ()),
    ],
)
def test_the_approval_is_read_from_the_newest_review_of_each_person_on_the_head_commit(
    reviews: list[dict[str, Any]], kind: str, who: tuple[str, ...]
) -> None:
    assert manual_run.approval_of(reviews, HEAD) == (kind, who)


def full_status(**changes: Any) -> manual_run.PrStatus:
    base = manual_run.PrStatus(
        "approved", ("alice",), manual_run.CheckView("passed", "https://x/CI"), manual_run.CheckView("passed", "https://x/J"),
        manual_run.CheckView("passed", "https://x/e2e", "Passed: all suites"), "squash",
    )  # fmt: skip
    return replace(base, **changes)


def block_of(body: str) -> str:
    return body.split(manual_run.STATUS_START)[1].split(manual_run.STATUS_END)[0]


def test_the_status_block_is_a_list_of_what_the_pull_request_waits_for() -> None:
    block = block_of(manual_run.render_comment(HEAD, views(), "", full_status()))
    assert block.splitlines()[1:] == [
        "> [!TIP]",
        "> ## 🚦 CI status",
        ">",
        "> - ✅ **Approval:** approved by `alice` on `aaaaaaa`",
        "> - ✅ **CI:** [passed](https://x/CI)",
        "> - ✅ **Jira reference:** [passed](https://x/J)",
        "> - ✅ **E2E:** [Passed: all suites](https://x/e2e)",
        "> - ✅ **Auto-merge:** on (squash)",
    ]


def test_each_state_of_the_pull_request_has_its_own_row() -> None:
    def rows(**changes: Any) -> str:
        return block_of(manual_run.render_comment(HEAD, views(), "", full_status(**changes)))

    assert "- ➖ **Auto-merge:** off" in rows(auto_merge="")
    assert "- ✏️ **Draft:** mark it ready for review" in rows(draft=True)
    assert "- ❌ **Merge conflict:** rebase on the target branch" in rows(conflict=True)
    assert "- ⏳ **Approval:** waiting for an approval" in rows(approval="waiting", approvers=())
    assert "- 🔄 **Approval:** approved by `a` on an older commit; needs a new approval on `aaaaaaa`" in rows(
        approval="older", approvers=("a",)
    )
    assert "- ❌ **Approval:** changes requested by `a`, `b`" in rows(approval="changes", approvers=("a", "b"))
    assert "- ❌ **CI:** [failed](https://x/CI)" in rows(ci=manual_run.CheckView("failed", "https://x/CI"))
    assert "- ⏳ **CI:** running" in rows(ci=manual_run.CheckView("active"))
    assert "- 🚫 **CI:** cancelled" in rows(ci=manual_run.CheckView("cancelled"))
    assert "Jira" not in rows(jira=None) and "**CI:**" not in rows(ci=None) and "**E2E:**" not in rows(e2e=None)
    assert "Draft" not in rows() and "Merge conflict" not in rows()


def test_a_failed_ci_names_the_failed_checks_and_says_how_many_more_there_are() -> None:
    names = tuple(f"check {i}" for i in range(8))
    ci = manual_run.CheckView("failed", "https://x/CI", failed=names)
    block = block_of(manual_run.render_comment(HEAD, views(), "", full_status(ci=ci)))
    assert "failed: `check 0`, `check 1`, `check 2`, `check 3`, `check 4` and 3 more" in block
    few = block_of(manual_run.render_comment(HEAD, views(), "", full_status(ci=replace(ci, failed=("a", "b")))))
    assert "failed: `a`, `b`" in few and "more" not in few


def test_the_suites_summary_counts_every_state_in_a_fixed_order() -> None:
    many = tuple(
        manual_run.SuiteView(f"s{i}", f"E2E / S{i}", st)
        for i, st in enumerate(["none", "passed", "active", "failed", "failed"])
    )
    assert manual_run.suites_summary(many) == "❌ 2 failed · ⏳ 1 running · ✅ 1 passed · ➖ 1 not run"
    assert manual_run.suites_summary(()) == ""


def test_a_comment_with_no_status_yet_says_it_is_loading_and_keeps_the_markers() -> None:
    body = manual_run.render_comment(HEAD, views())
    assert "Loading the status of the head commit `aaaaaaa`..." in body
    assert manual_run.requested_suites(body) == () and manual_run.head_of(body) == HEAD
    assert manual_run.STATUS_BLOCK.search(body)


def test_the_status_of_a_pull_request_comes_from_its_data_its_reviews_and_its_checks() -> None:
    pr = {"head": {"sha": HEAD}, "draft": True, "mergeable_state": "dirty", "auto_merge": {"merge_method": "squash"}}
    runs = [actions_check("CI"), actions_check("jira-reference", "failure"), app_check("E2E", "success", "Passed")]
    status = manual_run.pr_status(pr, [review("alice", "APPROVED")], runs)
    assert status.approval == "approved" and status.approvers == ("alice",)
    assert status.ci is not None and status.ci.state == "passed"
    assert status.jira is not None and status.jira.state == "failed"
    assert status.e2e == manual_run.CheckView("passed", "https://x/e2e", "Passed")
    assert status.auto_merge == "squash" and status.draft and status.conflict
    plain = manual_run.pr_status({"head": {"sha": HEAD}, "auto_merge": None, "mergeable_state": "clean"}, [], [])
    assert plain == manual_run.PrStatus("waiting") and not plain.auto_merge
    assert manual_run.pr_status({"head": {"sha": HEAD}, "auto_merge": {}}, [], []).auto_merge == "on"
    assert manual_run.pr_status({}, [], []).approval == "waiting"  # a pull request with no head says nothing


def test_a_refresh_writes_the_status_and_follows_the_pull_request() -> None:
    fake = Fake([actions_check("CI", None)])
    fake.pr = {**fake.pr, "auto_merge": None}
    manual_run.refresh(fake, POLICY, REPO, 7)  # type: ignore[arg-type]
    body = fake.comments[0]["body"]
    assert "**Approval:** waiting" in body and "**CI:** [running]" in body and "**Auto-merge:** off" in body
    fake.runs[0] = actions_check("CI", "success")
    fake.reviews = [review("alice", "APPROVED")]
    fake.pr = {**fake.pr, "auto_merge": {"merge_method": "squash"}}
    assert manual_run.refresh(fake, POLICY, REPO, 7) == "updated"  # type: ignore[arg-type]
    body = fake.comments[0]["body"]
    assert "approved by `alice`" in body and "**CI:** [passed]" in body and "**Auto-merge:** on (squash)" in body
    assert manual_run.refresh(fake, POLICY, REPO, 7) == "unchanged"  # type: ignore[arg-type]


# the status block on its own ---------------------------------------------------------------------------------------


def commented(fake: Fake, **changes: Any) -> Fake:
    """A Fake that already holds a control comment written for the head commit."""
    manual_run.refresh(fake, POLICY, REPO, 7)  # type: ignore[arg-type]
    return fake


def test_the_status_block_alone_is_rewritten_and_the_rest_of_the_comment_is_kept() -> None:
    fake = Fake([actions_check("CI")])
    fake.pr = {**fake.pr, "auto_merge": None}
    fake.reviews = [review("alice", "APPROVED")]
    commented(fake)
    first = fake.comments[0]["body"]
    kept = first.replace(manual_run.NOTES_START, manual_run.NOTES_START + "\n> [!NOTE]\n> **Last request by bob**")
    fake.comments[0]["body"] = kept
    fake.pr = {**fake.pr, "auto_merge": {"merge_method": "squash"}}
    assert manual_run.refresh_status(fake, REPO, 7) == "updated"  # type: ignore[arg-type]
    body = fake.comments[0]["body"]
    assert "**Auto-merge:** on (squash)" in body and "**Auto-merge:** off" not in body
    assert body.replace("➖ **Auto-merge:** off", "").split("<details>")[1] == kept.split("<details>")[1], (
        "only the block changed"
    )
    assert manual_run.refresh_status(fake, REPO, 7) == "unchanged"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("setup", "why"),
    [
        ("closed", "not open"),
        ("none", "no control comment"),
        ("old", "no status block"),
        ("other-head", "no status block"),
    ],
)
def test_the_status_block_is_left_alone_when_it_cannot_be_trusted(setup: str, why: str) -> None:
    fake = Fake([actions_check("CI")])
    if setup != "none":
        commented(fake)
    if setup == "closed":
        fake.pr = {**fake.pr, "state": "closed"}
    if setup == "old":  # a comment written before the status block existed
        fake.comments[0]["body"] = manual_run.STATUS_BLOCK.sub("", fake.comments[0]["body"])
    if setup == "other-head":
        fake.pr = {**fake.pr, "head": {"sha": "c" * 40}}
    before = [dict(c) for c in fake.comments]
    assert why in manual_run.refresh_status(fake, REPO, 7)  # type: ignore[arg-type]
    assert fake.comments == before
    assert not [p for p in fake.posted if p[0] == "PATCH"], "nothing was written"


def test_a_failed_write_of_the_status_block_is_an_error() -> None:
    fake = Fake([actions_check("CI")])
    fake.pr = {**fake.pr, "auto_merge": None}
    commented(fake)
    fake.pr = {**fake.pr, "auto_merge": {"merge_method": "squash"}}
    fake.patch_status = 500
    with pytest.raises(GitHubError):
        manual_run.refresh_status(fake, REPO, 7)  # type: ignore[arg-type]


def test_the_command_rewrites_the_status_block_and_reports_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake([actions_check("CI")])
    fake.pr = {**fake.pr, "auto_merge": None}
    commented(fake)
    fake.pr = {**fake.pr, "auto_merge": {"merge_method": "squash"}}
    monkeypatch.setattr(cli, "build_client", lambda **_: fake)
    assert cli.main(["manual-status", "--repo", REPO, "--number", "7"]) == 0
    assert "PR #7: status block updated" in capsys.readouterr().out
    assert "Auto-merge:** on (squash)" in fake.comments[0]["body"]


def test_a_failure_of_the_command_is_reported_and_exits_with_3(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = Fake([actions_check("CI")])
    commented(fake)
    fake.pr = {**fake.pr, "auto_merge": {"merge_method": "squash"}}
    fake.patch_status = 500
    monkeypatch.setattr(cli, "build_client", lambda **_: fake)
    assert cli.main(["manual-status", "--repo", REPO, "--number", "7"]) == 3
    assert "manual-status failed" in capsys.readouterr().err
