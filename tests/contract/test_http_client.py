"""The real transport, driven through an injected opener so no network is touched."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from osac_ci.github.api import GitHubError, HttpClient

pytestmark = pytest.mark.contract


class _Raw(io.BytesIO):
    status = 200

    def __enter__(self) -> _Raw:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def make(opener: Any) -> HttpClient:
    return HttpClient("tok-123", opener=opener)


def test_sends_auth_and_version_headers_and_parses_json() -> None:
    seen: list[urllib.request.Request] = []

    def opener(request: urllib.request.Request, timeout: float) -> _Raw:
        seen.append(request)
        return _Raw(json.dumps({"ok": True}).encode())

    response = make(opener).request("GET", "/repos/o/r/pulls/1", params={"per_page": "100", "page": "2"})
    assert response.status == 200 and response.data == {"ok": True}
    request = seen[0]
    assert request.full_url == "https://api.github.com/repos/o/r/pulls/1?per_page=100&page=2"
    assert request.get_header("Authorization") == "Bearer tok-123"
    assert request.get_header("X-github-api-version") == "2022-11-28"


def test_http_error_statuses_are_returned_not_raised() -> None:
    def opener(request: urllib.request.Request, timeout: float) -> _Raw:
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b'{"message":"nope"}'))  # type: ignore[arg-type]

    response = make(opener).request("GET", "/orgs/o/members/x")
    assert response.status == 404 and response.data == {"message": "nope"}


def test_transport_failure_raises_with_status_zero_and_no_token_in_the_message() -> None:
    def opener(request: urllib.request.Request, timeout: float) -> _Raw:
        raise urllib.error.URLError("connection refused tok-123")

    with pytest.raises(GitHubError) as err:
        make(opener).request("GET", "/x")
    assert err.value.status == 0 and "tok-123" not in str(err.value)


def test_post_sends_a_json_body() -> None:
    seen: list[urllib.request.Request] = []

    def opener(request: urllib.request.Request, timeout: float) -> _Raw:
        seen.append(request)
        return _Raw(b"{}")

    make(opener).request("POST", "/graphql", body={"query": "{ a }"})
    assert json.loads(seen[0].data) == {"query": "{ a }"}  # type: ignore[arg-type]
    assert seen[0].get_header("Content-type") == "application/json"


@pytest.mark.parametrize("path", ["repos/o/r", "https://evil.example/x", "//evil/x"])
def test_only_absolute_api_paths_are_accepted(path: str) -> None:
    with pytest.raises(ValueError, match="absolute API path"):
        make(lambda *a, **k: _Raw()).request("GET", path)


def test_empty_token_is_rejected() -> None:
    with pytest.raises(ValueError, match="token"):
        HttpClient("")


# ---- rate limit ---------------------------------------------------------------------------------------------------


class _WithHeaders(_Raw):
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        super().__init__(body)
        self.headers = headers


def test_the_remaining_requests_are_read_from_the_response_headers() -> None:
    def opener(request: urllib.request.Request, timeout: float) -> _Raw:
        return _WithHeaders(b"{}", {"X-RateLimit-Remaining": "4321", "X-RateLimit-Limit": "5000"})

    client = make(opener)
    assert client.rate_limit_remaining() is None  # nothing seen yet
    response = client.request("GET", "/x")
    assert client.rate_limit_remaining() == 4321
    assert response.headers["x-ratelimit-remaining"] == "4321"  # names are lower-cased


def test_the_remaining_requests_are_also_read_from_an_error_response() -> None:
    def opener(request: urllib.request.Request, timeout: float) -> _Raw:
        raise urllib.error.HTTPError(
            request.full_url,
            403,
            "rate limited",
            {"X-RateLimit-Remaining": "0"},
            io.BytesIO(b"{}"),  # type: ignore[arg-type]
        )

    client = make(opener)
    assert client.request("GET", "/x").status == 403 and client.rate_limit_remaining() == 0


@pytest.mark.parametrize("value", ["", "abc", "-1", "1.5", "²", "٣", "１２３"])  # the last three are non-ASCII digits
def test_a_malformed_remaining_header_is_ignored(value: str) -> None:
    def opener(request: urllib.request.Request, timeout: float) -> _Raw:
        return _WithHeaders(b"{}", {"X-RateLimit-Remaining": value})

    client = make(opener)
    client.request("GET", "/x")
    assert client.rate_limit_remaining() is None


def test_a_double_without_headers_gives_none() -> None:
    from osac_ci.github.api import rate_remaining

    assert rate_remaining(object()) is None and rate_remaining(make(lambda r, timeout: _Raw(b"{}"))) is None


def test_the_quota_is_tracked_per_bucket_so_a_graphql_answer_never_stands_in_for_the_rest_quota() -> None:
    answers = iter(
        [
            {"X-RateLimit-Remaining": "2735", "X-RateLimit-Resource": "core"},
            {
                "X-RateLimit-Remaining": "4856",
                "X-RateLimit-Resource": "graphql",
            },  # the last response, a much fuller bucket
        ]
    )

    def opener(request: urllib.request.Request, timeout: float) -> _Raw:
        return _WithHeaders(b"{}", next(answers))

    client = make(opener)
    client.request("GET", "/repos/o/r/pulls/1")
    client.request("POST", "/graphql", body={"query": "{}"})
    assert client.rate_limit_remaining() == 2735 and client.rate_limit_remaining("graphql") == 4856


def test_a_response_without_a_resource_header_counts_as_rest() -> None:
    client = make(lambda request, timeout: _WithHeaders(b"{}", {"X-RateLimit-Remaining": "77"}))
    client.request("GET", "/x")
    assert client.rate_limit_remaining() == 77 and client.rate_limit_remaining("graphql") is None
