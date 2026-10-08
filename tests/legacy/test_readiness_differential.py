"""Differential test: osac_ci.rules.readiness.decide vs the REAL ``decide_e2e_readiness`` in check-e2e-readiness.sh.

The bash script is sourced with CHECK_E2E_READINESS_LIB_ONLY=1 (its documented test hook). Both sides must agree on
allow/deny AND on the one-line reason (for plain denials the reason comes from ``explain_e2e_wait``).

Two complementary tests:

* a STRUCTURED SWEEP over every combination of the signals that decide the outcome (labels, earlier lgtm, who applied
  e2e-ready, CodeRabbit's latest decision and SHA, human change requests, empty head). This is what guarantees the
  rare-but-critical combinations are always exercised, for example an untrusted ``e2e-ready`` together with a
  CodeRabbit approval.
* a RANDOM test (hypothesis) over messier review and event histories.
"""

from __future__ import annotations

import itertools
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from helpers import HEAD, OTHER
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from legacy_sources import READINESS, require

from osac_ci.model import LabelEvent, Review
from osac_ci.rules.readiness import CODERABBIT_LOGIN, decide

pytestmark = pytest.mark.legacy

_BASH = """
set -uo pipefail
export CHECK_E2E_READINESS_LIB_ONLY=1
source "$SCRIPT"
set +e
out=$(decide_e2e_readiness "$1" "$2" "$3" "$4"); rc=$?
if [ "$rc" -ne 0 ] && [ -z "$out" ]; then out=$(explain_e2e_wait "$1" "$2" "$3" "$4"); fi
printf '%s\\n' "$rc"; printf '%s' "$out"
"""

BOT = "github-actions[bot]"


def _run_bash(labels: list[str], reviews: list[Review], head: str, events: list[LabelEvent]) -> tuple[int, str]:
    labels_json = json.dumps([{"name": n} for n in labels])
    reviews_json = json.dumps(
        [
            {
                "user": {"login": r.user, "type": r.user_type},
                "state": r.state,
                "submitted_at": r.submitted_at,
                "id": r.id,
                "commit_id": r.commit_id,
            }
            for r in reviews
        ]
    )
    events_json = json.dumps(
        [{"event": e.event, "label": {"name": e.label}, "actor": {"login": e.actor}} for e in events]
    )
    proc = subprocess.run(
        ["bash", "-c", _BASH, "bash", labels_json, reviews_json, head, events_json],
        env={"SCRIPT": str(require(READINESS)), "PATH": "/usr/bin:/bin:/usr/local/bin"},
        capture_output=True,
        text=True,
        check=True,
    )
    rc, _, out = proc.stdout.partition("\n")
    return int(rc), out


def _compare(labels: list[str], reviews: list[Review], head: str, events: list[LabelEvent]) -> str | None:
    rc, out = _run_bash(labels, reviews, head, events)
    mine = decide(set(labels), reviews, head, events)
    if (rc == 0) != mine.allowed:
        return f"allow/deny differs: bash rc={rc} out={out!r} python={mine}"
    if out != mine.reason:
        return f"reason differs: bash={out!r} python={mine.reason!r}"
    return None


def _cr(state: str, commit: str | None, i: int) -> Review:
    return Review(CODERABBIT_LOGIN, state, "Bot", f"2026-01-01T00:00:0{i}Z", i, commit)


# Each value is a list of CodeRabbit reviews, oldest first (the latest decision wins).
CODERABBIT_HISTORIES: dict[str, list[Review]] = {
    "none": [],
    "approved@head": [_cr("APPROVED", HEAD, 1)],
    "approved@other": [_cr("APPROVED", OTHER, 1)],
    "approved@none": [_cr("APPROVED", None, 1)],
    "changes_requested": [_cr("CHANGES_REQUESTED", HEAD, 1)],
    "dismissed": [_cr("DISMISSED", HEAD, 1)],
    "approved@head then changes_requested": [_cr("APPROVED", HEAD, 1), _cr("CHANGES_REQUESTED", HEAD, 2)],
    "changes_requested then approved@head": [_cr("CHANGES_REQUESTED", HEAD, 1), _cr("APPROVED", HEAD, 2)],
    "commented only": [_cr("COMMENTED", HEAD, 1)],
}
HUMAN_HISTORIES: dict[str, list[Review]] = {
    "none": [],
    "open changes_requested": [Review("alice", "CHANGES_REQUESTED", "User", "2026-01-01T00:00:03Z", 10, HEAD)],
    "changes_requested then dismissed": [
        Review("alice", "CHANGES_REQUESTED", "User", "2026-01-01T00:00:03Z", 10, HEAD),
        Review("alice", "DISMISSED", "User", "2026-01-01T00:00:04Z", 11, HEAD),
    ],
    "approved only": [Review("alice", "APPROVED", "User", "2026-01-01T00:00:03Z", 10, HEAD)],
}
E2E_READY_EVENTS: dict[str, list[LabelEvent]] = {
    "never applied": [],
    "applied by bot": [LabelEvent("labeled", "e2e-ready", BOT)],
    "applied by human": [LabelEvent("labeled", "e2e-ready", "alice")],
    "bot then human": [LabelEvent("labeled", "e2e-ready", BOT), LabelEvent("labeled", "e2e-ready", "alice")],
}


def test_structured_sweep_matches_the_real_bash_function() -> None:
    require(READINESS)
    cases = []
    for labels, lgtm_before, e2e_events, cr, human, head in itertools.product(
        [[], ["lgtm"], ["e2e-ready"], ["lgtm", "e2e-ready"]],
        [False, True],
        E2E_READY_EVENTS,
        CODERABBIT_HISTORIES,
        HUMAN_HISTORIES,
        [HEAD, ""],
    ):
        events = list(E2E_READY_EVENTS[e2e_events]) + ([LabelEvent("labeled", "lgtm", "alice")] if lgtm_before else [])
        reviews = CODERABBIT_HISTORIES[cr] + HUMAN_HISTORIES[human]
        cases.append((labels, reviews, head, events, (labels, lgtm_before, e2e_events, cr, human, head)))

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda c: _compare(c[0], c[1], c[2], c[3]), cases))
    failures = [(c[4], r) for c, r in zip(cases, results, strict=True) if r]
    assert len(cases) == 4 * 2 * 4 * 9 * 4 * 2 == 2304
    assert not failures, f"{len(failures)} of {len(cases)} scenarios differ; first: {failures[:2]}"


@st.composite
def scenarios(draw: st.DrawFn) -> dict[str, Any]:
    n = draw(st.integers(0, 5))
    reviews = [
        Review(
            user=draw(st.sampled_from(["alice", "bob", CODERABBIT_LOGIN, "dependabot[bot]"])),
            state=draw(st.sampled_from(["APPROVED", "CHANGES_REQUESTED", "DISMISSED", "COMMENTED", "PENDING"])),
            user_type=draw(st.sampled_from(["User", "Bot"])),
            submitted_at=draw(st.sampled_from([f"2026-01-01T00:00:0{i}Z" for i in range(1, 6)] + [None])),
            id=i + 1,  # unique so max_by ties cannot depend on jq's tie-breaking
            commit_id=draw(st.sampled_from([HEAD, OTHER, None])),
        )
        for i in range(n)
    ]
    events = [
        LabelEvent(
            draw(st.sampled_from(["labeled", "labeled", "unlabeled"])),
            draw(st.sampled_from(["lgtm", "e2e-ready", "approved"])),
            draw(st.sampled_from([BOT, "alice"])),
        )
        for _ in range(draw(st.integers(0, 3)))
    ]
    return {
        "labels": draw(st.lists(st.sampled_from(["lgtm", "e2e-ready", "approved"]), unique=True)),
        "reviews": reviews,
        "head": draw(st.sampled_from([HEAD, HEAD, OTHER, ""])),
        "events": events,
    }


@settings(max_examples=200, deadline=None, suppress_health_check=list(HealthCheck))
@given(scenarios())
def test_random_histories_match_the_real_bash_function(s: dict[str, Any]) -> None:
    problem = _compare(s["labels"], s["reviews"], s["head"], s["events"])
    assert problem is None, problem
