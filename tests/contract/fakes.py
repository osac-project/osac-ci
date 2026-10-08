"""A fake GitHub that implements the GitHubClient protocol. Fixtures are synthetic: they follow the real response
shapes but contain no real logins, so nothing sensitive is committed."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from osac_ci.github.api import Response

REPO = "example/app"
SHA = "c" * 40


class FakeGitHub:
    """Routes (method, path) to a canned answer. List endpoints are paged by the ``page``/``per_page`` params."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Any] = {}
        self.calls: list[tuple[str, str]] = []

    def add(self, method: str, path: str, data: Any, status: int = 200) -> None:
        self.routes[(method, path)] = (status, data)

    def request(self, method: str, path: str, *, params: Mapping[str, str] | None = None, body: Any = None) -> Response:
        self.calls.append((method, path))
        if (method, path) not in self.routes:
            return Response(404, {"message": f"no route for {method} {path}"})
        status, data = self.routes[(method, path)]
        if params and "page" in params and isinstance(data, (list, dict)):
            page, per_page = int(params["page"]), int(params["per_page"])
            if isinstance(data, list):
                data = data[(page - 1) * per_page : page * per_page]
            elif "check_runs" in data:
                data = {**data, "check_runs": data["check_runs"][(page - 1) * per_page : page * per_page]}
        return Response(status, data)


def pr_payload(**overrides: Any) -> dict[str, Any]:
    pr: dict[str, Any] = {
        "number": 7,
        "node_id": "PR_node7",
        "draft": False,
        "user": {"login": "alice"},
        "author_association": "CONTRIBUTOR",
        "labels": [{"name": "lgtm"}, {"name": "approved"}],
        "head": {"sha": SHA, "repo": {"full_name": "alice/app", "owner": {"login": "alice"}}},
        "base": {"ref": "main"},
    }
    pr.update(overrides)
    return pr


def check_runs(n: int) -> dict[str, Any]:
    return {
        "total_count": n,
        "check_runs": [
            {
                "name": f"check-{i}",
                "status": "completed",
                "conclusion": "success",
                "started_at": f"2026-01-01T00:00:{i % 60:02d}Z",
            }
            for i in range(n)
        ],
    }


def standard_fake(**pr_overrides: Any) -> FakeGitHub:
    """A fork PR by a non-member with a clean history. Tests tweak routes on top of it."""
    fake = FakeGitHub()
    base = f"/repos/{REPO}"
    fake.add("GET", f"{base}/pulls/7", pr_payload(**pr_overrides))
    fake.add("GET", f"{base}/pulls/7/reviews", [])
    fake.add("GET", f"{base}/issues/7/events", [])
    fake.add("GET", f"{base}/pulls/7/files", [{"filename": "a.go"}, {"filename": "docs/b.md"}])
    fake.add("GET", f"{base}/commits/{SHA}/check-runs", check_runs(3))
    fake.add("POST", "/graphql", {"data": {"node": {"mergeQueueEntry": None}}})
    fake.add("GET", "/orgs/example/members/alice", None, status=404)
    return fake
