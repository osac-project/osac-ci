"""Run E2E suites by hand: the control comment of a pull request.

osac-ci keeps one comment on a pull request (marked by a hidden line) that lists the E2E suites of the repository, each
with a checkbox. A member of the organization who checks a box starts that suite for the head commit, whatever the
approval state of the pull request, on a draft, on their own pull request and on a fork. The box clears itself, and the
comment says what happened: started (with the run), already running, already passed. Nothing is started twice. The same
can be asked in a comment of one line, ``/test`` (the suites the change needs) or ``/test vmaas caas``.

* ``render_comment`` writes the comment, ``requested_suites`` reads the boxes that are checked;
* ``parse_command`` reads a ``/test`` comment; ``read_request`` checks that an edited comment is the control comment of
  this pull request (or that a command is the person's own), that the person is a member of the organization, and gives
  the suites asked for;
* ``refresh`` writes (creates or updates) the comment from the state of the head commit;
* the first line of the comment is the status of the head commit: the ``CI`` check (with the checks that failed) and
  what the E2E suites say. It is read from the check runs ``refresh`` already lists, so it costs no request.

Only the OSAC CI app writes the comment, so a comment of anyone else with the same marker is never read as a request.
The first line of defense is that a person needs write access to edit a comment of the app at all; the code does not
rely on it and checks the member itself. The decision (``e2e_start``) never claims the E2E check for a run started by
hand: see ``e2e_check.finish_by_hand``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from osac_ci.e2e_start import (
    CHECK_APP,
    NEEDED,
    Review,
    Suite,
    approvals_of,
    approvals_seen,
    bot_blockers,
    line_state,
    reviews_with_roles,
    reviews_with_roles_unread,
    suites_of,
    unqualified_approvers,
)
from osac_ci.github.api import GitHubClient, GitHubError, check_repo, get, paginate
from osac_ci.model import CheckRun
from osac_ci.planner import latest_checks
from osac_ci.policy import Policy
from osac_ci.suite_lines import suite_line_runs_if_readable

MARKER = "<!-- osac-ci:manual-run -->"
BOT_LOGIN = "osac-ci[bot]"  # who writes the control comment: the OSAC CI app
NOTES_START = "<!-- notes:start -->"
NOTES_END = "<!-- notes:end -->"
HEAD_MARK = re.compile(r"<!-- head:(?P<sha>[0-9a-f]{40}) -->")
SUITE_ID = r"[a-z0-9][a-z0-9_-]{0,39}"
BOX = re.compile(rf"^- \[(?P<mark>[ xX])\] <!-- suite:(?P<suite>{SUITE_ID}) -->", re.MULTILINE)
# The command: "/test" alone, or "/test" and suite ids, alone on the first line of a comment. Anything else is not one.
COMMAND = re.compile(rf"/test(?:[ \t]+(?P<names>{SUITE_ID}(?:[ \t]+{SUITE_ID}){{0,9}}))?[ \t]*")
LINE_PREFIX = "E2E / "
# The icons of the comment: small images kept in docs/icons of this repository (GitHub shows no custom emoji), like the
# ones CodeRabbit uses. The alt text is what shows when an image cannot load.
ICON_BASE = "https://raw.githubusercontent.com/osac-project/osac-ci/main/docs/icons"
ICON_ALT = {
    "passed": "Passed",
    "error": "Error",
    "warning": "Warning",
    "pending": "Pending",
    "neutral": "Not applicable",
    "closed": "Closed",
    "merged": "Merged",
}


def icon(name: str, *, tall: bool = False) -> str:
    """The image of an icon, inline with the text that follows it. ``tall`` makes the box of the image taller than the
    icon (which stays 16 pixels, centred), so the rows of the status block have some air between them."""
    alt = ICON_ALT[name]
    height = 28 if tall else 16
    return (
        f'<img src="{ICON_BASE}/{name}.svg" width="16" height="{height}" align="absmiddle" alt="{alt}" title="{alt}">'
    )


def lead(name: str) -> str:
    """What a row of the status block starts with: the icon, then some space (no bullet)."""
    return f"{icon(name, tall=True)}&nbsp;&nbsp;&nbsp;"


# What a suite's line says, with an icon the eye catches: the same four states as ``e2e_start.line_state``.
STATE_TEXT = {
    "none": f"{icon('neutral')} not run on this commit",
    "active": f"{icon('pending')} running",
    "passed": f"{icon('passed')} passed",
    "failed": f"{icon('error')} failed",
}
RULES_URL = "https://github.com/osac-project/osac-ci/blob/main/docs/RULES.md#starting-suites-by-hand"
# The checks the status block shows. CI and the Jira reference are jobs of the repository's own workflows (GitHub
# Actions), whose names every repository of the flow shares because they are required checks; the E2E check is written
# by this app. Only the app of that name counts: another app can create a check with the same name.
CI_CHECK = "CI"
JIRA_CHECK = "jira-reference"
E2E_CHECK = "E2E"
ACTIONS_APP = "github-actions"
STATUS_START = "<!-- status:start -->"
STATUS_END = "<!-- status:end -->"
STATUS_BLOCK = re.compile(rf"{re.escape(STATUS_START)}.*?{re.escape(STATUS_END)}", re.DOTALL)
CHECK_ICON = {"active": "pending", "passed": "passed", "failed": "error", "cancelled": "closed", "other": "neutral"}
CHECK_WORD = {
    "active": "running",
    "passed": "passed",
    "failed": "failed",
    "cancelled": "cancelled",
    "other": "no result",
}
FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure", "action_required"})
FAILED_SHOWN = 5  # how many failed checks the CI row names
# The merge queue of a pull request: its entry (where it is in the queue) and its last events (added, removed and why).
QUEUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    mergeQueueEntry { state position }
    timelineItems(last: 30, itemTypes: [ADDED_TO_MERGE_QUEUE_EVENT, REMOVED_FROM_MERGE_QUEUE_EVENT]) {
      nodes { __typename
        ... on AddedToMergeQueueEvent { createdAt }
        ... on RemovedFromMergeQueueEvent { createdAt reason } } } } } }
"""
QUEUE_ENTRY_WORDS = {
    "QUEUED": "waiting its turn",
    "AWAITING_CHECKS": "checks are running",
    "MERGEABLE": "ready to merge",
    "LOCKED": "locked, a pull request before it is failing",
    "UNMERGEABLE": "cannot be merged",
}
# Why GitHub took a pull request out of the queue, in words. Anything else is shown as GitHub wrote it.
QUEUE_REMOVAL_WORDS = {
    "failed_checks": "checks failed",
    "checks_timed_out": "checks timed out",
    "merge_conflict": "merge conflict",
    "manual": "taken out by a person",
}


@dataclass(frozen=True)
class SuiteView:
    """One box of the comment: a suite (its id, its line name and what its line says on the head commit), or a variant
    of a suite (``variant``): a second box that starts the same suite, so it has no state of its own, and ``title`` is
    what its box says."""

    suite: str
    check: str
    state: str  # none | active | passed | failed
    url: str = ""
    variant: bool = False
    title: str = ""

    @property
    def label(self) -> str:
        if self.title:
            return self.title
        return self.check[len(LINE_PREFIX) :] if self.check.startswith(LINE_PREFIX) else self.check


@dataclass(frozen=True)
class CheckView:
    """A check of the head commit as the status block shows it: active | passed | failed | cancelled | other, the link
    of its run, what it says (the E2E check's title) and, for CI, the names of the other checks that failed."""

    state: str
    url: str = ""
    title: str = ""
    failed: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueueView:
    """Where a pull request is in the merge queue: queued (``detail`` says where) | removed (``detail`` says why;
    ``by_person`` when a person took it out) | none (not queued)."""

    state: str
    detail: str = ""
    by_person: bool = False


@dataclass(frozen=True)
class PrStatus:
    """Where a pull request stands, for the status block. ``approval`` is approved | older | changes | waiting |
    unchecked (the reviews show an approval but who may approve could not be read), with the logins behind it, and
    ``ignored`` the people whose approval does not count; ``blocked_by`` the bots (CodeRabbit) whose request for changes
    GitHub still holds against the merge, though the decision to start the E2E ignores it; ``auto_merge`` is the merge
    method when auto-merge is on, empty when it is off; ``queue`` the merge queue (None when it could not be read: the
    row is then left out); ``closed`` merged | closed once the pull request is over: the block then says so, and says
    nothing of auto-merge and the queue, which are no more."""

    approval: str
    approvers: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    ci: CheckView | None = None
    jira: CheckView | None = None
    e2e: CheckView | None = None
    auto_merge: str = ""
    draft: bool = False
    conflict: bool = False
    queue: QueueView | None = None
    closed: str = ""  # "" while open | merged | closed (without merging)
    base: str = ""  # the branch it was or would be merged into
    blocked_by: tuple[str, ...] = ()


def _alert(kind: str, lines: Sequence[str]) -> list[str]:
    """A GitHub alert block (a note, a tip): a quote whose first line names the kind."""
    return [f"> [!{kind}]", *(f"> {line}".rstrip() for line in lines)]


def _check_row(name: str, view: CheckView) -> str:
    mark = lead(CHECK_ICON.get(view.state, "neutral"))
    text = view.title or CHECK_WORD.get(view.state, view.state)
    text = f"[{text}]({view.url})" if view.url else text
    row = f"{mark}**{name}:** {text}"
    if view.failed:
        names = ", ".join(f"`{n}`" for n in view.failed[:FAILED_SHOWN])
        more = len(view.failed) - FAILED_SHOWN
        row += f" · failed: {names}" + (f" and {more} more" if more > 0 else "")
    return row


def _approval_row(status: PrStatus, sha: str) -> str:
    who = ", ".join(f"`{login}`" for login in status.approvers)
    if status.approval == "approved":
        return f"{lead('passed')}**Approval:** approved by {who} on `{sha[:7]}`"
    if status.approval == "older":
        return (
            f"{lead('warning')}**Approval:** approved by {who} on an older commit; needs a new approval on `{sha[:7]}`"
        )
    if status.approval == "changes":
        return f"{lead('error')}**Approval:** changes requested by {who}"
    if status.approval == "unchecked" and status.approvers:
        return (
            f"{lead('warning')}**Approval:** the reviews show an approval by {who}; "
            "who may approve could not be checked"
        )
    row = f"{lead('pending')}**Approval:** waiting for an approval"
    if status.ignored:
        names = ", ".join(f"`{login}`" for login in status.ignored)
        row += f" (an approval by {names} does not count: not from someone who may approve here)"
    return row


def _queue_row(queue: QueueView) -> str:
    if queue.state == "queued":
        return f"{lead('pending')}**Merge queue:** in the queue ({queue.detail})"
    if queue.state == "removed":
        if queue.by_person:
            return f"{lead('neutral')}**Merge queue:** {queue.detail}"
        return f"{lead('error')}**Merge queue:** removed from the queue: {queue.detail}"
    return f"{lead('neutral')}**Merge queue:** not queued"


def status_block(sha: str, status: PrStatus | None = None) -> list[str]:
    """The status of the pull request in one alert, between two markers so that it can be refreshed on its own. Rows
    that do not apply are left out (a check the commit does not have, a draft that is not one)."""
    rows: list[str] = []
    if status is None:
        rows = [f"Loading the status of the head commit `{sha[:7]}`..."]
    else:
        over = status.closed in ("merged", "closed")
        if status.closed == "merged":
            rows.append(
                f"{lead('merged')}**Merged** into `{status.base}`" if status.base else f"{lead('merged')}**Merged**"
            )
        elif over:
            rows.append(f"{lead('closed')}**Closed** without merging")
        if status.draft and not over:
            rows.append(f"{lead('warning')}**Draft:** mark it ready for review")
        if status.conflict and not over:
            rows.append(f"{lead('error')}**Merge conflict:** rebase on the target branch")
        rows.append(_approval_row(status, sha))
        if status.blocked_by and not over:
            who = ", ".join(f"`{login}`" for login in status.blocked_by)
            rows.append(
                f"{lead('error')}**Review:** {who} requested changes; GitHub will not merge until "
                "it approves or the review is dismissed"
            )
        for name, view in (("CI", status.ci), ("Jira reference", status.jira), ("E2E", status.e2e)):
            if view is not None:
                rows.append(_check_row(name, view))
        if not over:
            queued = status.queue is not None and status.queue.state == "queued"
            if status.auto_merge:
                rows.append(f"{lead('passed')}**Auto-merge:** on ({status.auto_merge})")
            elif queued:
                # GitHub clears the auto-merge of a pull request the moment the queue takes it: it merges by itself
                rows.append(f"{lead('passed')}**Auto-merge:** on (the pull request is in the merge queue)")
            else:
                rows.append(f"{lead('neutral')}**Auto-merge:** off")
            if status.queue is not None:
                row = _queue_row(status.queue)
                if status.blocked_by and status.queue.state == "none":
                    row += " (blocked by the review above)"
                rows.append(row)
    return [STATUS_START, *_alert("TIP", ["## 🚦 CI status", "", *rows]), STATUS_END]


def suites_summary(views: Sequence[SuiteView]) -> str:
    """What the suites say, as counts in the fixed order failed, running, passed, not run: the E2E section's title."""
    counts = [
        (state, mark, word, sum(v.state == state and not v.variant for v in views))
        for state, mark, word in (
            ("failed", icon("error"), "failed"),
            ("active", icon("pending"), "running"),
            ("passed", icon("passed"), "passed"),
            ("none", icon("neutral"), "not run"),
        )
    ]
    return " · ".join(f"{mark} {n} {word}" for _, mark, word, n in counts if n)


def render_comment(sha: str, views: Sequence[SuiteView], notes: str = "", status: PrStatus | None = None) -> str:
    """The comment: the status of the pull request, then the E2E tests section (how to start a suite, and the suites
    with a checkbox each, collapsed) and the answer to the last request. Every box is unchecked: a box that was checked
    is cleared when the request has been answered."""
    summary = suites_summary(views)
    lines = [
        MARKER,
        f"<!-- head:{sha} -->",
        *status_block(sha, status),
        "",
        f"<details><summary><b>🧪 E2E tests</b>{' · ' + summary if summary else ''}"
        f" · run them now ({len(views)} available)</summary>",
        "",
        "**Who:** any member of the organization, on any pull request (a draft, a fork or your own).",
        "",
        "**How to start a suite:**",
        "- Check a box below. The box clears itself.",
        "- Or comment `/test` for the suites this change needs.",
        "- Or comment `/test <suite>` for the ones you name (the ids are in the boxes below).",
        "",
        f"**Good to know:** suites run on the head commit `{sha[:7]}`. A suite you ask for runs again, whatever it "
        "ended with. If it is running, the same ask does nothing; asking for another version of it stops that run "
        "and starts the one you asked for.",
        "",
    ]
    for view in views:
        if view.variant:  # the state and the run are those of the suite's own line
            lines.append(f"- [ ] <!-- suite:{view.suite} --> **{view.label}**")
            continue
        link = f" ([run]({view.url}))" if view.url else ""
        lines.append(
            f"- [ ] <!-- suite:{view.suite} --> **{view.label}** · {STATE_TEXT.get(view.state, view.state)}{link}"
        )
    lines += ["", "</details>", "", NOTES_START]
    if notes.strip():
        lines += _alert("NOTE", notes.strip().splitlines())
    lines += [
        NOTES_END,
        "",
        f"<sub>[How suites are started by hand]({RULES_URL}) · The status is refreshed when CI starts or ends, a "
        "review, auto-merge or the merge queue changes, or someone comments · osac-ci</sub>",
        "",
    ]
    return "\n".join(lines)


def requested_suites(body: str) -> tuple[str, ...]:
    """The suites whose box is checked in the comment, in the order they are listed."""
    return tuple(m.group("suite") for m in BOX.finditer(body) if m.group("mark") in "xX")


def parse_command(body: str) -> tuple[str, ...] | None:
    """The suites a ``/test`` comment asks for: the names after it, or ``(NEEDED,)`` for a bare ``/test`` (the suites
    the change needs). None when the comment is not a command. Only the first line counts, and it must be the command
    and nothing else, so a quote (``> /test``), a code fence, a sentence that mentions the command and a longer word
    (``/testing``) are not requests."""
    first = body.split("\n", 1)[0].rstrip("\r")
    found = COMMAND.fullmatch(first)
    if found is None:
        return None
    names = found.group("names")
    return tuple(dict.fromkeys(names.split())) if names else (NEEDED,)


def head_of(body: str) -> str:
    """The full head commit the comment was written for ("" for a comment that has none)."""
    found = HEAD_MARK.search(body)
    return found.group("sha") if found else ""


def notes_of(body: str) -> str:
    """The last answer the comment shows (its alert, without the quote marks), so a refresh keeps it until there is a
    newer one."""
    start, end = body.find(NOTES_START), body.find(NOTES_END)
    if start < 0 or end < start:
        return ""
    lines = [ln for ln in body[start + len(NOTES_START) : end].strip().splitlines() if ln.strip() != "> [!NOTE]"]
    return "\n".join(ln[2:] if ln.startswith("> ") else ln.removeprefix(">") for ln in lines).strip()


def answer_text(notes: Sequence[tuple[str, str]], by: str, run_url: str = "") -> str:
    """What happened to a request, one line per suite."""
    if not notes:
        return ""
    lines = []
    for suite, note in notes:
        subject = f"**{suite}**" if suite else "The request"
        lines.append(f"- {subject} {note}")
    tail = f" ([run]({run_url}))" if run_url else ""
    return f"**Last request by @{by}**{tail}\n" + "\n".join(lines)


def is_control_comment(comment: Mapping[str, object], bot: str = BOT_LOGIN) -> bool:
    user = comment.get("user")
    login = str(user.get("login", "")) if isinstance(user, Mapping) else ""
    return login == bot and MARKER in str(comment.get("body", ""))


def find_control_comment(
    client: GitHubClient, repo: str, number: int, bot: str = BOT_LOGIN
) -> Mapping[str, object] | None:
    for raw in paginate(client, f"/repos/{check_repo(repo)}/issues/{number}/comments"):
        if isinstance(raw, Mapping) and is_control_comment(raw, bot):
            return raw
    return None


def is_org_member(client: GitHubClient, org: str, login: str) -> bool:
    """A public or private member of the organization (204). Anything else - not a member, a bot, an unknown account -
    is not."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", org) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login
    ):
        return False
    response = client.request("GET", f"/orgs/{org}/members/{login}")
    if response.status == 204:
        return True
    if response.status in (302, 404):
        return False
    raise GitHubError(response.status, f"GET the membership of {login} in {org}")


def read_request(
    client: GitHubClient,
    repo: str,
    number: int,
    comment_id: int,
    sender: str,
    bot: str = BOT_LOGIN,
    org_client: GitHubClient | None = None,
    event_body: str | None = None,
) -> tuple[tuple[str, ...], str, str]:
    """The suites asked for by ``sender`` in the comment ``comment_id`` of pull request ``number``, the reason a
    request was refused (empty when none was) and the head commit the comment was written for (the decision refuses
    the request when the pull request has moved on). Two kinds of comment ask: an edit of the control comment of this
    pull request, written by the app, whose checked boxes name the suites; and a ``/test`` comment written by the
    person themselves (an account of a person, not a bot), which names them or asks for the suites the change needs. A
    command is not tied to a commit: it means "now". Only a member of the organization may ask. Nothing asked for is
    not a refusal: an edit that unchecks a box (the app clearing it) asks for nothing. ``org_client`` is the client
    that may read the organization's members (the main client when none is given). ``event_body`` is the text of the
    control comment as the event delivered it, which is what the person checked: the app rewrites the comment all the
    time while CI and the suites change, and a rewrite between the click and the moment the decision reads the comment
    would take the box away, so the boxes are read from the event when it is given."""
    repo = check_repo(repo)
    comment = get(client, f"/repos/{repo}/issues/comments/{comment_id}")
    if not str(comment.get("issue_url", "")).endswith(f"/issues/{number}"):
        return (), "", ""
    body = str(comment.get("body", ""))
    if is_control_comment(comment, bot):
        if event_body is not None:
            body = event_body
        asked = requested_suites(body)
        seen = head_of(body)
    else:
        author = comment.get("user")
        login = str(author.get("login", "")) if isinstance(author, Mapping) else ""
        kind = str(author.get("type", "")) if isinstance(author, Mapping) else ""
        command = parse_command(body)
        if command is None or kind != "User" or login.lower() != sender.lower():
            return (), "", ""
        asked, seen = command, ""
    if not asked:
        return (), "", ""
    org = repo.split("/", 1)[0]
    if not is_org_member(org_client or client, org, sender):
        return (), f"@{sender} is not a member of the {org} organization, so nothing was started", ""
    return asked, "", seen


def _conclusion(run: Mapping[str, object]) -> str | None:
    value = run.get("conclusion")
    return value if isinstance(value, str) else None


def _is_app(run: Mapping[str, object]) -> bool:
    app = run.get("app")
    return isinstance(app, Mapping) and app.get("slug") == CHECK_APP


def views_for(suites: Sequence[Suite], runs: Sequence[Mapping[str, object]]) -> tuple[SuiteView, ...]:
    """The suites with what their lines say, from the raw check runs of the head commit. Only the OSAC CI app's lines
    count: another app's check with the same name says nothing about a suite."""
    runs = [r for r in runs if _is_app(r)]
    latest = latest_checks(
        tuple(
            CheckRun(str(r["name"]), str(r["status"]), _conclusion(r), str(r.get("started_at") or ""), app=CHECK_APP)
            for r in runs
        )
    )
    urls: dict[str, str] = {}
    for r in sorted(runs, key=lambda r: str(r.get("started_at") or "")):
        urls[str(r["name"])] = str(r.get("details_url") or r.get("html_url") or "")
    return tuple(
        SuiteView(s.name, s.check, line_state(latest, s.check), urls.get(s.check, ""), bool(s.variant_of), s.label)
        for s in suites
    )


def _started(r: Mapping[str, object]) -> str:
    return str(r.get("started_at") or "")


def _slug(r: Mapping[str, object]) -> object:
    app = r.get("app")
    return app.get("slug") if isinstance(app, Mapping) else None


def check_view(
    runs: Sequence[Mapping[str, object]], name: str, app: str, *, keep_other: bool = False, with_failed: bool = False
) -> CheckView | None:
    """The newest run of the check ``name`` written by ``app`` on the head commit, from its raw check runs; None when
    there is none. A run that ended with neither success nor failure (skipped, neutral) says nothing and gives None,
    unless ``keep_other`` (the E2E check, whose title says what it is). With ``with_failed`` the view also names the
    other checks whose newest run failed (this app's own lines and the E2E ones are left out: the E2E section shows
    them)."""
    own = [r for r in runs if r.get("name") == name and _slug(r) == app]
    if not own:
        return None
    run = max(own, key=_started)
    conclusion = _conclusion(run)
    if run.get("status") != "completed":
        state = "active"
    elif conclusion == "success":
        state = "passed"
    elif conclusion in FAILED_CONCLUSIONS:
        state = "failed"
    elif conclusion == "cancelled":
        state = "cancelled"
    elif keep_other:
        state = "other"
    else:
        return None
    failed: tuple[str, ...] = ()
    if with_failed and state == "failed":
        newest: dict[str, Mapping[str, object]] = {}
        for r in sorted(runs, key=_started):
            if _slug(r) != CHECK_APP and r.get("name") != name:
                newest[str(r.get("name"))] = r
        failed = tuple(
            sorted(n for n, r in newest.items() if not n.startswith("E2E") and _conclusion(r) in FAILED_CONCLUSIONS)
        )
    output = run.get("output")
    title = str(output.get("title") or "") if isinstance(output, Mapping) else ""
    url = str(run.get("details_url") or run.get("html_url") or "")
    return CheckView(state, url, title if keep_other else "", failed)


def approval_of(
    reviews: Sequence[Review], head: str, author: str, fork: bool
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Where the approval of the pull request stands on ``head``, by the rule of the decision: who may approve,
    CodeRabbit never on a fork and never blocking, the author never. Returns the kind (changes: someone who may
    approve asks for changes; approved: an approval on this commit; older: an approval of an older commit; waiting),
    the logins the kind is about, and the people whose approval does not count."""
    approvers, stale, blocking = approvals_of(reviews, head, author, fork)
    ignored = unqualified_approvers(reviews, author, fork)
    for kind, found in (("changes", blocking), ("approved", approvers), ("older", stale)):
        if found:
            return kind, found, ignored
    return "waiting", (), ignored


def _closed_of(pr: Mapping[str, object]) -> str:
    if str(pr.get("state", "open")) != "closed":
        return ""
    return "merged" if pr.get("merged") or pr.get("merged_at") else "closed"


def _base_of(pr: Mapping[str, object]) -> str:
    base = pr.get("base")
    return str(base.get("ref", "")) if isinstance(base, Mapping) else ""


def pr_status(
    pr: Mapping[str, object],
    reviews: Sequence[Review],
    runs: Sequence[Mapping[str, object]],
    *,
    repo: str,
    roles_known: bool = True,
) -> PrStatus:
    """The status of the pull request from its own data, its reviews (with the reviewers' roles, read the way the
    decision reads them) and the check runs of the head commit. ``roles_known`` False when the roles could not be read:
    the approval row then says what the reviews show and that it was not checked, never "approved"."""
    head = pr.get("head")
    sha = str(head.get("sha", "")) if isinstance(head, Mapping) else ""
    user = pr.get("user")
    author = str(user.get("login", "")) if isinstance(user, Mapping) else ""
    source = head.get("repo") if isinstance(head, Mapping) else None
    fork = str(source.get("full_name", "") if isinstance(source, Mapping) else "").lower() != repo.lower()
    if roles_known:
        approval, approvers, ignored = approval_of(reviews, sha, author, fork)
    else:
        approval, approvers, ignored = "unchecked", approvals_seen(reviews, sha), ()
    auto = pr.get("auto_merge")
    return PrStatus(
        approval=approval,
        approvers=approvers,
        ignored=ignored,
        blocked_by=bot_blockers(reviews),
        ci=check_view(runs, CI_CHECK, ACTIONS_APP, with_failed=True),
        jira=check_view(runs, JIRA_CHECK, ACTIONS_APP),
        e2e=check_view(runs, E2E_CHECK, CHECK_APP, keep_other=True),
        auto_merge=str(auto.get("merge_method") or "on") if isinstance(auto, Mapping) else "",
        draft=bool(pr.get("draft")),
        conflict=pr.get("mergeable_state") == "dirty",
        closed=_closed_of(pr),
        base=_base_of(pr),
    )


def queue_view(client: GitHubClient, repo: str, number: int) -> QueueView | None:
    """Where the pull request is in the merge queue, from GitHub's GraphQL: queued with its place, removed (the latest
    thing that happened to it in the queue, and why) or not queued. None when the answer cannot be had (the token may
    not read the queue, or GitHub says no): the status then has no queue row rather than a wrong one."""
    owner, _, name = check_repo(repo).partition("/")
    try:
        response = client.request(
            "POST",
            "/graphql",
            body={"query": QUEUE_QUERY, "variables": {"owner": owner, "name": name, "number": int(number)}},
        )
    except GitHubError:
        return None
    data = response.data
    if response.status != 200 or not isinstance(data, Mapping) or data.get("errors"):
        return None
    repository = (data.get("data") or {}).get("repository") or {}
    pr = repository.get("pullRequest") if isinstance(repository, Mapping) else None
    if not isinstance(pr, Mapping):
        return None
    entry = pr.get("mergeQueueEntry")
    if isinstance(entry, Mapping):
        word = QUEUE_ENTRY_WORDS.get(str(entry.get("state")), "in the queue")
        place = entry.get("position")
        return QueueView("queued", f"position {place}, {word}" if isinstance(place, int) else word)
    items = (pr.get("timelineItems") or {}).get("nodes") or []
    events = sorted((e for e in items if isinstance(e, Mapping)), key=lambda e: str(e.get("createdAt") or ""))
    last = events[-1] if events else None
    if last is None or last.get("__typename") != "RemovedFromMergeQueueEvent" or last.get("reason") == "merged":
        return QueueView("none")
    reason = str(last.get("reason") or "")
    by_person = reason == "manual"
    return QueueView(
        "removed", QUEUE_REMOVAL_WORDS.get(reason, reason.replace("_", " ")) or "no reason given", by_person
    )


def status_of(
    client: GitHubClient, repo: str, number: int, pr: Mapping[str, object], runs: Sequence[Mapping[str, object]]
) -> PrStatus:
    """The status of a pull request, reading its reviews and the role of each reviewer as the decision does. When the
    roles cannot be read (no right to), the approval row says so instead of guessing from the raw reviews."""
    raw = [r for r in paginate(client, f"/repos/{repo}/pulls/{number}/reviews") if isinstance(r, Mapping)]
    user = pr.get("user")
    author = str(user.get("login", "")) if isinstance(user, Mapping) else ""
    queue = queue_view(client, repo, number) if _closed_of(pr) == "" else None
    try:
        status = pr_status(pr, reviews_with_roles(client, repo, raw, author), runs, repo=repo)
    except GitHubError:
        status = pr_status(pr, reviews_with_roles_unread(raw), runs, repo=repo, roles_known=False)
    return replace(status, queue=queue)


def refresh_status(client: GitHubClient, repo: str, number: int, *, bot: str = BOT_LOGIN) -> str:
    """Rewrite only the status block of the control comment from the pull request as it is now. For what changes after
    the comment was written, such as the app turning on auto-merge, or the pull request being merged or closed (the
    block then says so). It needs no policy, creates nothing and leaves the suites and the last answer alone. On an open
    pull request a comment written for another head commit is left to the full refresh, which the new commit brings; a
    pull request that is over gets no full refresh any more, so its block is written whatever commit the comment was
    written for."""
    repo = check_repo(repo)
    pr = get(client, f"/repos/{repo}/pulls/{number}")
    existing = find_control_comment(client, repo, number, bot)
    if existing is None:
        return "skipped: the pull request has no control comment"
    body = str(existing.get("body", ""))
    sha = str(pr["head"]["sha"])
    if not STATUS_BLOCK.search(body) or (_closed_of(pr) == "" and head_of(body) != sha):
        return "skipped: the comment has no status block for this commit"
    runs = [
        r
        for r in paginate(client, f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs")
        if isinstance(r, Mapping)
    ]
    block = "\n".join(status_block(sha, status_of(client, repo, number, pr, runs)))
    new = STATUS_BLOCK.sub(lambda _: block, body, count=1)
    if new == body:
        return "unchanged"
    response = client.request("PATCH", f"/repos/{repo}/issues/comments/{existing['id']}", body={"body": new})
    if response.status != 200:
        raise GitHubError(response.status, "updating the status block")
    return "updated"


def refresh(
    client: GitHubClient,
    policy: Policy,
    repo: str,
    number: int,
    *,
    notes: str | None = None,
    bot: str = BOT_LOGIN,
    org_client: GitHubClient | None = None,
) -> str:
    """Create or update the control comment of the pull request from the state of its head commit. Nothing is written
    when the pull request is closed or its author is not a member of the organization, or when the text is the same
    (an edit would only wake the workflow again). ``notes`` replaces the last answer; None keeps the one the comment
    has."""
    repo = check_repo(repo)
    pr = get(client, f"/repos/{repo}/pulls/{number}")
    if str(pr.get("state")) != "open":
        # Nothing to list or start any more; a comment that is there still shows where the pull request ended.
        return refresh_status(client, repo, number, bot=bot)
    suites = suites_of(policy)
    if not suites:
        return "skipped: the policy has no E2E suite"
    author = str((pr.get("user") or {}).get("login", ""))
    org = repo.split("/", 1)[0]
    existing = find_control_comment(client, repo, number, bot)
    if existing is None and not is_org_member(org_client or client, org, author):
        return "skipped: the author is not a member of the organization"
    sha = str(pr["head"]["sha"])
    runs = [
        r
        for r in paginate(client, f"/repos/{repo}/commits/{sha}/check-runs", key="check_runs")
        if isinstance(r, Mapping)
    ]
    kept = notes if notes is not None else (notes_of(str(existing.get("body", ""))) if existing else "")
    lines = suite_line_runs_if_readable(client, repo, sha, [s.check for s in suites], CHECK_APP)
    body = render_comment(sha, views_for(suites, [*runs, *lines]), kept, status_of(client, repo, number, pr, runs))
    if existing is None:
        response = client.request("POST", f"/repos/{repo}/issues/{number}/comments", body={"body": body})
        if response.status != 201:
            raise GitHubError(response.status, "creating the control comment")
        # The early job (see e2e-pr-decide.yml) posts a placeholder at the same moment: two runs can both have found
        # none. Keep the oldest, the one that has the place in the conversation, and fill it in.
        oldest = find_control_comment(client, repo, number, bot)
        created = response.data.get("id") if isinstance(response.data, Mapping) else None
        if oldest is None or created is None or int(str(oldest["id"])) == int(created):
            return "created"
        gone = client.request("DELETE", f"/repos/{repo}/issues/comments/{created}")
        if gone.status not in (
            204,
            404,
        ):  # 404: it is already gone; anything else leaves a duplicate for the next refresh
            raise GitHubError(gone.status, "deleting the duplicate control comment")
        existing = oldest
        if str(existing.get("body", "")) == body:
            return "created (kept the older comment)"
        response = client.request("PATCH", f"/repos/{repo}/issues/comments/{existing['id']}", body={"body": body})
        if response.status != 200:
            raise GitHubError(response.status, "updating the control comment")
        return "created (kept the older comment)"
    if str(existing.get("body", "")) == body:
        return "unchanged"
    response = client.request("PATCH", f"/repos/{repo}/issues/comments/{existing['id']}", body={"body": body})
    if response.status != 200:
        raise GitHubError(response.status, "updating the control comment")
    return "updated"
