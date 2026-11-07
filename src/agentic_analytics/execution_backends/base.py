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

    def close_session(self, session_id: str) -> None: ...
