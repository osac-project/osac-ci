"""The lock facts: what the planner decided per lockable job, written into the check run and read back."""

from __future__ import annotations

import json

import pytest
from helpers import HEAD, snap

from osac_ci.lockfacts import encode, parse
from osac_ci.model import CheckRun, LockFact, Mode
from osac_ci.planner import plan
from osac_ci.policy import parse_policy
from osac_ci.publish import payload

pytestmark = pytest.mark.unit

BASE = "version: 1\nrepo: o/r\nmerge: {required_labels: [], blocking_labels: []}\n"
LOCKS = "locks: {review: {open_when: [lgtm-label]}, member: {open_when: [org-member]}}\n"
JOBS = (
    "jobs: {a: {check: a, locks: [review]}, b: {check: b, locks: [review], paths: ['ui/**']}, "
    "c: {check: c}, d: {check: d, locks: [member, review]}}\n"
)
POLICY = parse_policy(BASE + LOCKS + JOBS)


def facts_for(mode: Mode = Mode.PR, **kw):  # type: ignore[no-untyped-def]
    return plan(snap(POLICY, changed_files=kw.pop("files", ("go/a.go",)), check_runs=(), **kw), POLICY, mode).lock_facts


# ---- what the planner says ----------------------------------------------------------------------------------------


def test_every_job_with_locks_gets_a_fact_in_policy_order_and_jobs_without_locks_do_not() -> None:
    facts = facts_for(labels=frozenset())
    assert [f.job_id for f in facts] == ["a", "b", "d"]


def test_a_closed_lock_is_locked_and_names_the_lock() -> None:
    by_job = {f.job_id: f for f in facts_for(labels=frozenset())}
    assert (by_job["a"].state, by_job["a"].lock) == ("locked", "review")
    assert by_job["a"].check == "a"


def test_an_open_lock_is_open() -> None:
    by_job = {f.job_id: f.state for f in facts_for(labels=frozenset({"lgtm"}))}
    assert by_job == {"a": "open", "b": "not-applicable", "d": "open"}


def test_a_job_that_does_not_apply_is_not_applicable_whatever_its_lock() -> None:
    by_job = {f.job_id: f.state for f in facts_for(labels=frozenset(), files=("go/a.go",))}
    assert by_job["b"] == "not-applicable"
    by_job = {f.job_id: f.state for f in facts_for(labels=frozenset(), files=("ui/a.ts",))}
    assert by_job["b"] == "locked"


def test_the_first_closed_lock_of_a_job_is_the_one_named() -> None:
    fork = {f.job_id: f for f in facts_for(labels=frozenset(), is_fork=True, author="x")}
    assert fork["d"].lock == "member" and fork["d"].code == "membership"


def test_a_job_that_ran_is_open_even_if_a_lock_is_closed_now() -> None:
    s = snap(
        POLICY, labels=frozenset(), changed_files=("go/a.go",), check_runs=(CheckRun("a", "completed", "success"),)
    )
    assert {f.job_id: f.state for f in plan(s, POLICY).lock_facts}["a"] == "open"


def test_a_merge_queue_commit_has_no_facts() -> None:
    assert facts_for(Mode.QUEUE, labels=frozenset()) == ()


def test_a_policy_without_locks_has_no_facts() -> None:
    plain = parse_policy(BASE + "jobs: {a: {check: a}}\n")
    assert plan(snap(plain), plain).lock_facts == ()


# ---- the block ----------------------------------------------------------------------------------------------------

FACTS = (LockFact("a", "a", "locked", "review"), LockFact("b", "b", "open"), LockFact("c", "c", "not-applicable"))


def test_nothing_is_written_when_there_are_no_facts() -> None:
    assert encode((), HEAD) == ""


def test_the_block_round_trips() -> None:
    text = encode(FACTS, HEAD)
    assert parse(text, HEAD) == {
        "a": {"state": "locked", "lock": "review"},
        "b": {"state": "open", "lock": ""},
        "c": {"state": "not-applicable", "lock": ""},
    }
    assert "| a | a | locked | review |" in text  # people get a table too


def test_the_block_is_one_line_of_compact_json_inside_a_comment() -> None:
    (line,) = [ln for ln in encode(FACTS, HEAD).splitlines() if ln.startswith("<!--")]
    assert line.endswith("-->") and "\n" not in line
    assert json.loads(line.removeprefix("<!-- osac-ci-locks:v1 ").removesuffix(" -->"))["head"] == HEAD


def test_a_name_with_two_hyphens_cannot_end_the_comment_early() -> None:
    odd = (LockFact("a--b", "a", "locked", "lock--x"),)
    text = encode(odd, HEAD)
    (line,) = [ln for ln in text.splitlines() if ln.startswith("<!--")]
    assert line.count("--") == 2  # only the opening and closing markers
    assert parse(text, HEAD) == {"a--b": {"state": "locked", "lock": "lock--x"}}


def test_a_block_for_another_commit_is_no_fact() -> None:
    assert parse(encode(FACTS, HEAD), "d" * 40) is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no block here",
        '<!-- osac-ci-locks:v2 {"head":"' + HEAD + '","jobs":{}} -->',
        "<!-- osac-ci-locks:v1 not json -->",
        "<!-- osac-ci-locks:v1 [] -->",
        '<!-- osac-ci-locks:v1 {"head":"' + HEAD + '"} -->',
        '<!-- osac-ci-locks:v1 {"head":"' + HEAD + '","jobs":[]} -->',
        '<!-- osac-ci-locks:v1 {"head":"' + HEAD + '","jobs":{"a":{"state":"maybe"}}} -->',
        '<!-- osac-ci-locks:v1 {"head":"' + HEAD + '","jobs":{"a":{"state":"open","lock":3}}} -->',
        '<!-- osac-ci-locks:v1 {"head":"' + HEAD + '","jobs":{"a":"open"}} -->',
        '<!-- osac-ci-locks:v1 {"head":"'
        + HEAD
        + '","jobs":{"a":{"state":["open"]}}} -->',  # unhashable: must not raise
        '<!-- osac-ci-locks:v1 {"head":"' + HEAD + '","jobs":{"a":{"state":{"x":1}}}} -->',
        '<!-- osac-ci-locks:v1 {"head":"' + HEAD + '","jobs":{"a":{"state":null}}} -->',
        '<!-- osac-ci-locks:v1 {"head":"' + HEAD + '","jobs":{"a":{"state":1}}} -->',
        '<!-- osac-ci-locks:v1 {"head":"' + HEAD + '","jobs":{"a":{"state":true}}} -->',
        '<!-- osac-ci-locks:v1 {"head":"' + HEAD + '","jobs":{"a":{"state":"open"}} -->',
    ],
)
def test_anything_unexpected_is_no_fact_never_locked_and_never_open(text: str) -> None:
    assert parse(text, HEAD) is None


def test_text_around_the_block_does_not_matter() -> None:
    text = "intro\n" + encode(FACTS, HEAD) + "\ntrailing words"
    assert parse(text, HEAD) is not None


# ---- in the check run ---------------------------------------------------------------------------------------------


def test_the_check_run_carries_the_block_only_when_there_are_facts() -> None:
    locked = plan(snap(POLICY, labels=frozenset(), check_runs=()), POLICY)
    with_locks = payload(locked, HEAD)
    assert "osac-ci-locks:v1" in with_locks["output"]["text"] and "summary" in with_locks["output"]
    plain = parse_policy(BASE + "jobs: {a: {check: a}}\n")
    assert "text" not in payload(plan(snap(plain), plain), HEAD)["output"]


def test_a_changed_fact_changes_what_is_posted() -> None:
    open_ = plan(snap(POLICY, labels=frozenset({"lgtm"}), changed_files=("go/a.go",), check_runs=()), POLICY)
    locked = plan(snap(POLICY, labels=frozenset(), changed_files=("go/a.go",), check_runs=()), POLICY)
    assert payload(open_, HEAD)["external_id"] != payload(locked, HEAD)["external_id"]
    assert payload(open_, HEAD)["output"]["text"] != payload(locked, HEAD)["output"]["text"]


def test_the_block_appearing_on_a_commit_posts_the_check_again_even_if_the_verdict_is_the_same() -> None:
    """The case from review: a policy gains locks, or the format changes, and nothing else about the verdict does."""
    from dataclasses import replace

    verdict = plan(snap(POLICY, labels=frozenset({"lgtm"}), changed_files=("go/a.go",), check_runs=()), POLICY)
    without = replace(verdict, lock_facts=())
    assert payload(without, HEAD)["output"]["summary"] == payload(verdict, HEAD)["output"]["summary"]
    assert "text" not in payload(without, HEAD)["output"]
    assert payload(without, HEAD)["external_id"] != payload(verdict, HEAD)["external_id"]


def test_without_facts_the_digest_is_what_it_was_before_locks_existed() -> None:
    """A policy without locks must keep its external id, or every open pull request would be posted again on upgrade."""
    import hashlib

    plain = parse_policy(BASE + "jobs: {a: {check: a}}\n")
    body = payload(plan(snap(plain), plain), HEAD)
    expected = hashlib.sha256(
        f"{body['status']}|{body.get('conclusion')}|{body['output']['summary']}".encode()
    ).hexdigest()
    assert body["external_id"] == f"osac-ci:{expected[:16]}"


def test_the_same_facts_post_the_same_thing() -> None:
    a = plan(snap(POLICY, labels=frozenset(), check_runs=()), POLICY)
    b = plan(snap(POLICY, labels=frozenset(), check_runs=()), POLICY)
    assert payload(a, HEAD) == payload(b, HEAD)
