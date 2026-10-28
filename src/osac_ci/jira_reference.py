"""The Jira reference of a pull request as a line of the controller.

Prow's jira plugin checks that the title of a pull request names an existing Jira issue (``OSAC-1234:``) or says
``NO-ISSUE:``, and puts a label on it. A workflow used to turn that label into a check the merge rules could require,
and ran again whenever the labels or the title changed. Here the controller does the same as a line of the policy:
written when a pull request opens, its title or labels change, or it gets a commit, and part of the ``CI Autopilot``
check, with no workflow and no run of its own to tidy. A repository without Prow can ask for the title to be checked.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from osac_ci.e2e_check import check_sha, newest_status, post_status
from osac_ci.github.api import GitHubClient, check_repo, get
from osac_ci.policy import JiraReference, Policy

TITLE_MAX = 300


def evaluate(pull: Mapping[str, object], rule: JiraReference) -> tuple[str, str]:
    """(state, what the line says) for a pull request: ``success`` or ``failure``."""
    if rule.mode == "label":
        labels = pull.get("labels")
        names = {str(x.get("name")) for x in labels if isinstance(x, Mapping)} if isinstance(labels, list) else set()
        if rule.label in names:
            return "success", "Valid reference"
        return "failure", f"No valid Jira reference: start the title with OSAC-1234: or NO-ISSUE: (label {rule.label})"
    title = str(pull.get("title") or "")[:TITLE_MAX]
    if re.match(rule.title_pattern, title):
        return "success", "Valid reference"
    return "failure", "No Jira reference: start the title with OSAC-1234: or NO-ISSUE:"


def update(client: GitHubClient, repo: str, number: int, policy: Policy, lines_kind: str = "statuses") -> str:
    """Write the line for the head commit of the pull request, when its answer changed. Returns what was done."""
    rule = policy.jira_reference
    if rule is None:
        return "the policy has no jira_reference: nothing to do"
    repo = check_repo(repo)
    pull = get(client, f"/repos/{repo}/pulls/{number}")
    if pull.get("state") != "open":
        return "the pull request is not open: nothing to do"
    head = pull.get("head")
    sha = check_sha(str(head.get("sha", ""))) if isinstance(head, Mapping) else ""
    state, text = evaluate(pull, rule)
    current = newest_status(client, repo, sha, rule.check, lines_kind)
    if current is not None and current.get("state") == state and current.get("description") == text:
        return f"{rule.check}: unchanged ({state})"
    post_status(client, repo, sha, rule.check, state, text, "", lines_kind)
    return f"{rule.check}: {state}"
