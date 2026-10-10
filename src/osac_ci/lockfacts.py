"""Lock facts: what the planner decided about each lockable job, in a form a workflow can read back.

The OSAC CI check run already exists for every commit. A job that wants to know whether it may start can read the
newest one and look for a hidden block in its output text, with no secret and no extra check:

    <!-- osac-ci-locks:v1 {"head":"<sha>","jobs":{"e2e-vmaas":{"lock":"cost","state":"locked"}}} -->

``locked`` means a lock holds the job, ``open`` that every lock is open, ``not-applicable`` that the job has nothing
to do for these files. A job that is not in the block has no locks.

This is a fact for advisory use. Any workflow of a repository can write a check run of any name, so a pull request can
post a block of its own; a job that reads it only decides whether to spend runner minutes, and a pull request could
simply delete that step too. Anything that spends money or touches a secret must not read this: it asks the planner
(``osac-ci``) from a workflow the pull request cannot edit. The verdict itself never depends on this block.
"""

from __future__ import annotations

import json
import re
from typing import Any

from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get
from osac_ci.github.snapshot import COMMIT_SHA
from osac_ci.model import LockFact

VERSION = "v1"
_BLOCK = re.compile(rf"<!-- osac-ci-locks:{VERSION} (?P<json>\{{[^\r\n]*?\}}) -->")
_STATES = frozenset({"locked", "open", "not-applicable"})
UNKNOWN = "unknown"


def encode(facts: tuple[LockFact, ...], head_sha: str) -> str:
    """The text for the check run's output: a small table for people and the hidden block for workflows.
    Empty when there is nothing to say, so a policy without locks posts exactly what it did before."""
    if not facts:
        return ""
    jobs = {f.job_id: ({"state": f.state, "lock": f.lock} if f.lock else {"state": f.state}) for f in facts}
    block = json.dumps({"head": head_sha, "jobs": jobs}, sort_keys=True, separators=(",", ":"))
    block = block.replace("--", "-\\u002d")  # a comment must not contain two hyphens in a row; this is still JSON
    rows = [f"| {f.job_id} | {f.check} | {f.state} | {f.lock or '-'} |" for f in facts]
    table = "\n".join(["### Locks", "", "| Job | Check | State | Lock |", "|---|---|---|---|", *rows])
    return f"{table}\n\n<!-- osac-ci-locks:{VERSION} {block} -->\n"


def parse(text: str, head_sha: str) -> dict[str, dict[str, str]] | None:
    """The jobs of the block in ``text``, or ``None`` if there is none for exactly ``head_sha`` or it is malformed.
    Anything unexpected is ``None``: a reader must treat that as "no fact", never as locked or as open."""
    found = _BLOCK.search(text or "")
    if found is None:
        return None
    try:
        data: Any = json.loads(found["json"])
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("head") != head_sha or not isinstance(data.get("jobs"), dict):
        return None
    jobs: dict[str, dict[str, str]] = {}
    for job_id, fact in data["jobs"].items():
        if not isinstance(job_id, str) or not isinstance(fact, dict):
            return None
        state, lock = fact.get("state"), fact.get("lock", "")
        # Check the type before the membership: a list or an object is not hashable and would raise, not be refused.
        if not isinstance(state, str) or state not in _STATES or not isinstance(lock, str):
            return None
        jobs[job_id] = {"state": state, "lock": lock}
    return jobs


def lookup(client: GitHubClient, repo: str, head_sha: str, job_id: str, check_name: str) -> str:
    """``locked``, ``open``, ``not-applicable`` or ``unknown`` for a job on a commit, read from the newest check run of
    ``check_name``. ``unknown`` covers every case where there is no usable fact (no such check, no block, another
    commit, a job without locks, an API error); a caller decides what that means, and for advisory use it means go
    on."""
    repo = check_repo(repo)
    if not COMMIT_SHA.match(head_sha):
        raise ValueError(f"invalid commit sha: {head_sha!r}")
    try:
        data = get(
            client,
            f"/repos/{repo}/commits/{head_sha}/check-runs",
            {"check_name": check_name, "filter": "latest", "per_page": "10"},
        )
    except GitHubError:
        return UNKNOWN
    runs = sorted(data.get("check_runs", ()), key=lambda r: r.get("id", 0))
    if not runs:
        return UNKNOWN
    jobs = parse((runs[-1].get("output") or {}).get("text") or "", head_sha)
    fact = (jobs or {}).get(job_id)
    return fact["state"] if fact else UNKNOWN
