from __future__ import annotations

import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentic_analytics.execution_backends import ExecutionBackend
from agentic_analytics.ids import EntityType, new_id
from agentic_analytics.models import (
    AnalysisSession,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionType,
    SessionMode,
    SessionStatus,
)
from agentic_analytics.repositories import ExecutionRepository, SourceRepository
from agentic_analytics.settings import Settings

from .artifact_registry import ArtifactRegistry, snapshot_workspace
from .inspector import fingerprint_file
from .workspace import WorkspaceService


class ExecutionPolicyError(PermissionError):
    pass


def _bounded_text(value: str, max_chars: int) -> tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars], True


class ExecutionService:
    def __init__(
        self,
        backend: ExecutionBackend,
        executions: ExecutionRepository,
        sources: SourceRepository,
        artifacts: ArtifactRegistry,
        workspace: WorkspaceService,
        settings: Settings,
    ) -> None:
        self.backend = backend
        self.executions = executions
        self.sources = sources
        self.artifacts = artifacts
        self.workspace = workspace
        self.settings = settings
        self._locks_guard = threading.Lock()
        self._session_locks: dict[str, threading.Lock] = {}

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._session_locks.setdefault(session_id, threading.Lock())

    def execute_python(
        self,
        session: AnalysisSession,
        code: str,
        source_ids: list[str] | None = None,
        timeout_seconds: int | None = None,
    ) -> ExecutionRecord:
        if session.status is not SessionStatus.ACTIVE:
            raise ExecutionPolicyError("session is closed; create a new analysis session")
        if session.mode is SessionMode.STRICT and not self.backend.conformant:
            raise ExecutionPolicyError("strict sessions require a conformant managed backend")
        workspace_root = self.workspace.authorize_workspace(session.workspace_root)
        state_root = self.settings.state_dir.resolve(strict=False)
        if workspace_root == state_root or state_root in workspace_root.parents:
            raise ExecutionPolicyError("workspace must not be inside server state")
        source_ids = list(dict.fromkeys(source_ids or []))
        source_fingerprints: dict[str, Any] = {}
        for source_id in source_ids:
            source = self.sources.get(session.id, source_id)
            if source.relative_path is None:
                source_fingerprints[source_id] = source.fingerprint
                continue
            path = self.workspace.resolve_file(session.workspace_root, source.relative_path)
            source_fingerprints[source_id] = fingerprint_file(path)

        timeout = min(
            max(timeout_seconds or self.settings.execution_timeout_seconds, 1),
            self.settings.max_execution_timeout_seconds,
        )
        readonly_paths = tuple(
            dict.fromkeys(
                self.workspace.resolve_file(workspace_root, source.relative_path)
                for source in self.sources.list(session.id)
                if source.read_only
                and source.relative_path is not None
                and (workspace_root / source.relative_path).exists()
            )
        )
        execution_id = new_id(EntityType.EXECUTION)
        # Serialize executions within a session so overlapping before/after snapshots cannot
        # attribute one execution's file changes to another.
        with self._session_lock(session.id):
            started_at = datetime.now(UTC)
            before = snapshot_workspace(workspace_root, excluded_roots=(state_root,))
            # Never write executable code through a caller-controlled workspace path.
            # The Docker backend mounts this exact server-owned file read-only.
            temp_dir = state_root / "tmp"
            temp_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=f"{execution_id}-", dir=temp_dir) as request:
                script_path = Path(request) / "request.py"
                script_path.write_text(code, encoding="utf-8")
                try:
                    result = self.backend.execute(
                        session, script_path, timeout, readonly_paths=readonly_paths
                    )
                    after = snapshot_workspace(workspace_root, excluded_roots=(state_root,))
                    artifacts = self.artifacts.register_changes(
                        session.id, execution_id, workspace_root, before, after
                    )
                except Exception as exc:
                    # Registration failures happen after code has already changed files.
                    # Preserve their audit record and any artifacts published before failure.
                    self._persist_backend_failure(
                        session,
                        execution_id,
                        code,
                        source_ids,
                        source_fingerprints,
                        timeout,
                        started_at,
                        exc,
                    )
                    raise

        stdout, stdout_truncated = _bounded_text(result.stdout, self.settings.max_output_chars)
        stderr, stderr_truncated = _bounded_text(result.stderr, self.settings.max_output_chars)
        error = None
        if result.status is not ExecutionStatus.SUCCEEDED:
            error = {
                "type": result.status.value,
                "message": stderr or f"execution ended with status {result.status.value}",
                "exit_code": result.exit_code,
            }
        completed_at = datetime.now(UTC) if result.status.terminal else None
        record = ExecutionRecord(
            id=execution_id,
            session_id=session.id,
            execution_type=ExecutionType.MANAGED_PYTHON,
            status=result.status,
            request={"code": code, "source_ids": source_ids, "timeout_seconds": timeout},
            source_ids=source_ids,
            source_fingerprints=source_fingerprints,
            started_at=started_at,
            completed_at=completed_at,
            runtime=result.runtime,
            stdout_preview=stdout,
            stderr_preview=stderr,
            truncated=(
                stdout_truncated
                or stderr_truncated
                or result.stdout_truncated
                or result.stderr_truncated
            ),
            artifact_ids=[artifact.id for artifact in artifacts],
            error=error,
        )
        self.executions.add(record)
        return record

    def _persist_backend_failure(
        self,
        session: AnalysisSession,
        execution_id: str,
        code: str,
        source_ids: list[str],
        source_fingerprints: dict[str, Any],
        timeout: int,
        started_at: datetime,
        exc: BaseException,
    ) -> None:
        record = ExecutionRecord(
            id=execution_id,
            session_id=session.id,
            execution_type=ExecutionType.MANAGED_PYTHON,
            status=ExecutionStatus.FAILED,
            request={"code": code, "source_ids": source_ids, "timeout_seconds": timeout},
            source_ids=source_ids,
            source_fingerprints=source_fingerprints,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            artifact_ids=[
                artifact.id
                for artifact in self.artifacts.repository.list(session.id)
                if artifact.execution_id == execution_id
            ],
            error={"type": type(exc).__name__, "message": str(exc)},
        )
        self.executions.add(record)
