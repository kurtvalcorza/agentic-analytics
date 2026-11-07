"""Expose intentional request failures without exposing unexpected server errors."""

from collections.abc import Callable
from functools import wraps
from inspect import signature
from typing import Any, get_type_hints

from mcp.server.mcpserver.exceptions import ToolError

from agentic_analytics.execution_backends.docker import DockerExecutionError
from agentic_analytics.repositories import RecordNotFound
from agentic_analytics.services.artifact_registry import ArtifactIntegrityError, ArtifactLimitError
from agentic_analytics.services.query import QueryExecutionError


def tool_errors[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except (
            ValueError,
            PermissionError,
            RecordNotFound,
            ArtifactLimitError,
            ArtifactIntegrityError,
            QueryExecutionError,
            DockerExecutionError,
        ) as exc:
            raise ToolError(str(exc)) from exc

    # MCP resolves annotations from the callable's globals. Preserve concrete types
    # across modules rather than leaving this wrapper with another module's strings.
    wrapped.__annotations__ = get_type_hints(function)
    wrapped.__dict__["__signature__"] = signature(function, eval_str=True)
    return wrapped


def paginate(items: list[Any], limit: int | None, offset: int, max_page: int) -> dict[str, Any]:
    """Slice ``items`` into a bounded page and describe how to fetch the next one."""

    if offset < 0:
        raise ValueError("offset must be >= 0")
    size = max_page if limit is None else limit
    if size < 1:
        raise ValueError("limit must be >= 1")
    size = min(size, max_page)
    page = items[offset : offset + size]
    end = offset + len(page)
    return {
        "page": page,
        "count": len(page),
        "total": len(items),
        "offset": offset,
        "next_offset": end if end < len(items) else None,
        "truncated": end < len(items),
    }
