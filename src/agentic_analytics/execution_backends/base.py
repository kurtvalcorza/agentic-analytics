from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agentic_analytics.models import AnalysisSession, ExecutionStatus


@dataclass(frozen=True, slots=True)
class BackendResult:
    status: ExecutionStatus
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    runtime: dict[str, Any] = field(default_factory=dict)
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    # Set when the backend terminated the workload for exceeding a host resource budget
    # (for example ``"disk"``); the execution service then discards its partial outputs.
    resource_limit: str | None = None
    # Files and directories the backend copied into the workspace for this run (Docker
    # outputs/). Removed again if artifact registration fails.
    output_paths: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class Outcome:
    """How a run ended, decided before its captured output is attached (C7).

    Both backends judge a run into one of these and then build the :class:`BackendResult`
    with :meth:`result`, so the rules for stderr and runtime facts live in one place.
    """

    status: ExecutionStatus
    exit_code: int | None = None
    # Replaces whatever the workload wrote to stderr (a server-side explanation).
    stderr: str | None = None
    # Used only when the workload wrote nothing to stderr.
    stderr_fallback: str | None = None
    resource_limit: str | None = None
    # Facts merged over the backend's own runtime record.
    runtime: dict[str, Any] = field(default_factory=dict)
    output_paths: tuple[str, ...] = ()

    def result(
        self,
        *,
        stdout: str,
        stderr: str,
        runtime: dict[str, Any],
        stdout_truncated: bool,
        stderr_truncated: bool,
    ) -> BackendResult:
        if self.stderr is not None:
            stderr = self.stderr
        elif not stderr and self.stderr_fallback is not None:
            stderr = self.stderr_fallback
        return BackendResult(
            status=self.status,
            stdout=stdout,
            stderr=stderr,
            exit_code=self.exit_code,
            runtime={**runtime, **self.runtime},
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            resource_limit=self.resource_limit,
            output_paths=self.output_paths,
        )


class ExecutionBackend(Protocol):
    name: str
    conformant: bool

    def execute(
        self,
        session: AnalysisSession,
        script_path: Path,
        timeout_seconds: int,
        *,
        readonly_paths: tuple[Path, ...] = (),
    ) -> BackendResult: ...

    def rollback_outputs(self, result: BackendResult, script_path: Path) -> None:
        """Undo what ``result`` copied into the workspace, after registration failed (C12)."""
        ...

    def close_session(self, session_id: str) -> None: ...
