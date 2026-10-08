"""Minimal GitHub REST/GraphQL transport.

Standard library only (no new dependency). The ``GitHubClient`` protocol is what the snapshot code needs; tests
use a fake that implements it, and ``HttpClient`` is the real one. HTTP statuses are returned, not raised, so a
caller can treat 404 as an answer (for example "not an org member"); only transport failures raise.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

API_ROOT = "https://api.github.com"
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
DEFAULT_PER_PAGE = 100
MAX_PAGES = 50  # same cap as the legacy readiness script, to avoid looping on a bad pagination answer


class GitHubError(RuntimeError):
    """A request failed (transport error or an unexpected HTTP status). ``status`` is 0 for transport errors."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub API error (HTTP {status}): {message}")
        self.status = status


@dataclass(frozen=True)
class Response:
    status: int
    data: Any = None


class GitHubClient(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Any = None,
    ) -> Response: ...


def check_repo(repo: str) -> str:
    """Repo names end up in URL paths: accept only owner/name made of safe characters."""
    if not _REPO.match(repo):
        raise ValueError(f"invalid repo name: {repo!r}")
    return repo


def get(client: GitHubClient, path: str, params: Mapping[str, str] | None = None) -> Any:
    response = client.request("GET", path, params=params)
    if not 200 <= response.status < 300:
        raise GitHubError(response.status, f"GET {path}")
    return response.data


def paginate(
    client: GitHubClient,
    path: str,
    *,
    key: str | None = None,
    params: Mapping[str, str] | None = None,
    per_page: int = DEFAULT_PER_PAGE,
) -> Iterator[Any]:
    """Yield items page by page. ``key`` names the list inside an object response (for example ``check_runs``)."""
    for page in range(1, MAX_PAGES + 1):
        data = get(client, path, {**(params or {}), "per_page": str(per_page), "page": str(page)})
        items = data[key] if key else data
        yield from items
        if len(items) < per_page:
            return
    raise GitHubError(0, f"too many pages for {path} (more than {MAX_PAGES})")


class HttpClient:
    """Real transport. The token is read by the caller (from the environment) and never logged."""

    def __init__(
        self,
        token: str,
        *,
        root: str = API_ROOT,
        timeout: float = 30.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not token:
            raise ValueError("a GitHub token is required")
        self._token = token
        self._root = root.rstrip("/")
        self._timeout = timeout
        self._opener = opener

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Any = None,
    ) -> Response:
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise ValueError(f"path must be an absolute API path, got {path!r}")
        url = self._root + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "osac-ci",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)  # noqa: S310 - https root only
        try:
            with self._opener(request, timeout=self._timeout) as raw:
                return Response(raw.status, _parse(raw.read()))
        except urllib.error.HTTPError as exc:
            return Response(exc.code, _parse(exc.read()))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GitHubError(0, f"{method} {path}: {type(exc).__name__}") from exc


def _parse(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", errors="replace")[:200]
