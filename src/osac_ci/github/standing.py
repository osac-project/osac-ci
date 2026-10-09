"""One listing of every open pull request with its latest ``OSAC CI`` check run (GraphQL, about 7 points per 100 PRs).

This is what lets a sweep decide which PRs need a new verdict without reading each one (see ``osac_ci.stale``). It uses
the GraphQL rate limit, which is separate from the REST one the rest of a sweep spends.
"""

from __future__ import annotations

from typing import Any

from osac_ci.github.api import GitHubClient, GitHubError, check_repo
from osac_ci.stale import LastVerdict, Standing

# Check runs posted with a workflow's built-in token belong to the GitHub Actions app. A check posted with the token of
# another app has another id; ``None`` accepts any app.
ACTIONS_APP_ID = 15368
_QUERY = """
query($owner: String!, $name: String!, $after: String, $check: String!, $app: Int) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        updatedAt
        headRefOid
        commits(last: 1) { nodes { commit {
          checkSuites(last: 20, filterBy: {appId: $app, checkName: $check}) { nodes {
            checkRuns(last: 10, filterBy: {checkName: $check}) { nodes { status conclusion startedAt completedAt } }
          } }
        } } }
      }
    }
  }
}
"""


def _newest_run(node: dict[str, Any]) -> LastVerdict | None:
    runs: list[dict[str, Any]] = []
    for commit in (node.get("commits") or {}).get("nodes") or ():
        for suite in ((commit.get("commit") or {}).get("checkSuites") or {}).get("nodes") or ():
            runs += (suite.get("checkRuns") or {}).get("nodes") or []
    if not runs:
        return None
    # The newest check run of a name decides, wherever it was posted.
    newest = max(runs, key=lambda r: str(r.get("startedAt") or r.get("completedAt") or ""))
    return LastVerdict(
        status=str(newest["status"]).lower(),
        conclusion=str(newest["conclusion"]).lower() if newest.get("conclusion") else None,
        started_at=newest.get("startedAt"),
        completed_at=newest.get("completedAt"),
    )


def fetch_standings(
    client: GitHubClient, repo: str, check_name: str, app_id: int | None = ACTIONS_APP_ID
) -> list[Standing]:
    """Every open PR with the newest check run of ``check_name`` on its head commit, posted by the app ``app_id``
    (``None``: any app). Fails loudly, never partially."""
    owner, _, name = check_repo(repo).partition("/")
    standings: list[Standing] = []
    cursor: str | None = None
    while True:
        variables = {"owner": owner, "name": name, "after": cursor, "check": check_name, "app": app_id}
        response = client.request("POST", "/graphql", body={"query": _QUERY, "variables": variables})
        data = response.data if isinstance(response.data, dict) else {}
        if response.status != 200 or data.get("errors") or not data.get("data"):
            raise GitHubError(response.status, "listing open pull requests failed")
        page = data["data"]["repository"]["pullRequests"]
        for node in page["nodes"]:
            standings.append(Standing(node["number"], node["headRefOid"], node["updatedAt"], _newest_run(node)))
        if not page["pageInfo"]["hasNextPage"]:
            return standings
        cursor = page["pageInfo"]["endCursor"]
