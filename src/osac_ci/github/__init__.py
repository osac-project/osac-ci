"""GitHub adapter: the only place that does network I/O. The planner never imports from here."""

from osac_ci.github.api import GitHubClient, GitHubError, HttpClient, Response
from osac_ci.github.snapshot import fetch_snapshot

__all__ = ["GitHubClient", "GitHubError", "HttpClient", "Response", "fetch_snapshot"]
