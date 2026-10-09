"""Handle ``/ok-to-test <sha>``: record that an org member authorized one exact commit of a pull request.

The authorization is a check run on that commit, so it is bound to the commit by construction: a new push has a new SHA
and no such check, and there is no label to strip and no window between a push and its cleanup (see rules/fork.py).

The command must carry the commit's *full* SHA. A bare ``/ok-to-test`` would authorize "whatever the head is when the
workflow reads it", and an attacker watching the comments could push in the few seconds between the reviewer's comment
and that read. A short prefix is no better: seven hex digits are 28 bits, so the author of the PR can pre-compute a
malicious commit whose SHA starts like the reviewed one and push it after the review. With all 40 digits the reviewer
authorizes exactly the code they looked at, and a replaced head is refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get
from osac_ci.github.snapshot import (
    OVERRIDE_TITLE_PREFIX,
    authorization_external_id,
    is_org_member,
    override_approver_logins,
    override_external_id,
)
from osac_ci.policy import Policy
from osac_ci.rules.fork import COMMAND

OVERRIDE_COMMAND = "/override"
_OVERRIDE_START = re.compile(rf"^{re.escape(OVERRIDE_COMMAND)}(?:\s|$)")
_OVERRIDE = re.compile(
    rf"^{re.escape(OVERRIDE_COMMAND)}[ \t]+(?P<sha>[0-9a-fA-F]{{40}})[ \t]+(?P<reason>\S[^\r\n]{{9,139}})[ \t]*$"
)
_COMMAND = re.compile(rf"^{re.escape(COMMAND)}[ \t]+(?P<sha>[0-9a-fA-F]{{40}})[ \t]*$")


@dataclass(frozen=True)
class Authorization:
    handled: bool  # False: nothing to say (not a command, not from an org member, not enabled)
    granted: bool
    message: str
    head_sha: str = ""


def _ignore(message: str) -> Authorization:
    return Authorization(handled=False, granted=False, message=message)


def authorize(
    client: GitHubClient,
    org_client: GitHubClient,
    policy: Policy,
    repo: str,
    number: int,
    commenter: str,
    body: str,
    *,
    org: str | None = None,
    dry_run: bool = False,
) -> Authorization:
    repo = check_repo(repo)
    org = org or repo.split("/", 1)[0]
    text = body.strip()
    if policy.trust.authorization != "sha-bound":
        return _ignore("sha-bound authorization is not enabled in this policy")
    if not text.startswith(COMMAND):
        return _ignore("not an authorization command")
    if not is_org_member(org_client, org, commenter):
        return _ignore("the commenter is not an org member")

    pr = get(client, f"/repos/{repo}/pulls/{number}")
    if pr.get("state") != "open":
        return _ignore("the pull request is not open")
    head: str = pr["head"]["sha"]
    short = head[:7]  # for reading only; the command and the comparison always use the full SHA

    found = _COMMAND.match(text)
    if found is None:
        return Authorization(True, False, f"Authorize this exact commit by commenting `{COMMAND} {head}`.", head)
    if found["sha"].lower() != head:
        return Authorization(
            True,
            False,
            f"The head is now `{head}`, not `{found['sha'].lower()}`: the commit you looked at was replaced. "
            f"Review the new one and comment `{COMMAND} {head}`.",
            head,
        )

    if not dry_run:
        body_out = {
            "name": policy.trust.check_name,
            "head_sha": head,
            "status": "completed",
            "conclusion": "success",
            "external_id": authorization_external_id(commenter, head),
            "output": {
                "title": "Authorized to use secrets and start E2E",
                "summary": f"Authorized by `{commenter}` for commit `{head}`. A new push needs a new authorization.",
            },
        }
        response = client.request("POST", f"/repos/{repo}/check-runs", body=body_out)
        if response.status != 201:
            raise GitHubError(response.status, f"POST /repos/{repo}/check-runs")
    return Authorization(
        True, True, f"Authorized `{short}` (by `{commenter}`). A new push needs a new authorization.", head
    )


def reply(client: GitHubClient, repo: str, number: int, message: str) -> None:
    response = client.request("POST", f"/repos/{check_repo(repo)}/issues/{number}/comments", body={"body": message})
    if response.status != 201:
        raise GitHubError(response.status, f"POST /repos/{repo}/issues/{number}/comments")


def override(
    client: GitHubClient,
    org_client: GitHubClient,
    policy: Policy,
    repo: str,
    number: int,
    commenter: str,
    body: str,
    *,
    dry_run: bool = False,
) -> Authorization:
    """Handle ``/override <full sha> <reason>``: waive the protected-path approval for exactly one commit.

    Only the people the policy names may do it, the pull request's author never, and the reason (10 to 140 characters
    on one line) is part of the command. The waiver is a check run on that commit that names who and why, so it is the
    audit trail, and a new push has a new SHA and needs a new override. Anyone else's comment is ignored."""
    repo = check_repo(repo)
    text = body.strip()
    if policy.override is None:
        return _ignore("overrides are not enabled in this policy")
    if not _OVERRIDE_START.match(text):
        return _ignore("not an override command")
    if commenter.lower() not in override_approver_logins(org_client, policy.override, {}):
        return _ignore("the commenter may not override")

    pr = get(client, f"/repos/{repo}/pulls/{number}")
    if pr.get("state") != "open":
        return _ignore("the pull request is not open")
    head: str = pr["head"]["sha"]
    if pr["user"]["login"].lower() == commenter.lower():
        return Authorization(True, False, "You opened this pull request, so someone else has to override it.", head)

    found = _OVERRIDE.match(text)
    if found is None:
        return Authorization(
            True,
            False,
            f"Waive the protected-path approval for this exact commit by commenting "
            f"`{OVERRIDE_COMMAND} {head} <reason, 10 to 140 characters, on one line>`.",
            head,
        )
    if found["sha"].lower() != head:
        return Authorization(
            True,
            False,
            f"The head is now `{head}`, not `{found['sha'].lower()}`: the commit you looked at was replaced. "
            f"Review the new one and comment `{OVERRIDE_COMMAND} {head} <reason>`.",
            head,
        )
    reason = " ".join(found["reason"].split())
    if not dry_run:
        body_out = {
            "name": policy.override.check_name,
            "head_sha": head,
            "status": "completed",
            "conclusion": "success",
            "external_id": override_external_id(commenter, head),
            "output": {
                "title": f"{OVERRIDE_TITLE_PREFIX}{commenter}: {reason}",
                "summary": (
                    f"Protected-path approval waived by `{commenter}` for commit `{head}`.\n\n"
                    f"Reason: {reason}\n\nA new push needs a new override."
                ),
            },
        }
        response = client.request("POST", f"/repos/{repo}/check-runs", body=body_out)
        if response.status != 201:
            raise GitHubError(response.status, f"POST /repos/{repo}/check-runs")
    return Authorization(
        True,
        True,
        f"Protected-path approval waived for `{head[:7]}` by `{commenter}`: {reason}. A new push needs a new override.",
        head,
    )
