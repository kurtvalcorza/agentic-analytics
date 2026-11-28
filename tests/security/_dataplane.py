"""Typed data-plane requests shared by the security tests (C10)."""

from __future__ import annotations

from agentic_analytics.dataplane import DataPlaneClient
from agentic_analytics.dataplane.requests import ProfileRequest


def profile_request(
    client: DataPlaneClient,
    timeout: float,
    path: str = "/nonexistent/source.csv",
    *,
    kind: str = "csv",
    sample_limit: int = 5,
    max_profile_columns: int = 10,
) -> ProfileRequest:
    """A well-formed profile request; tests that fake or break the worker never read it."""

    return ProfileRequest(
        path=path,
        kind=kind,
        sample_limit=sample_limit,
        max_profile_columns=max_profile_columns,
        limits=client.limits(timeout),
    )
