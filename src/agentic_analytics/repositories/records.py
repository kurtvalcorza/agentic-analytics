from __future__ import annotations

from pathlib import Path

from agentic_analytics.models import (
    AnalysisSession,
    Artifact,
    DataSource,
    EvidenceItem,
    ExecutionRecord,
    ValidationFinding,
)

from .base import JsonRecordRepository


class SessionRepository(JsonRecordRepository[AnalysisSession]):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "sessions", AnalysisSession)


class SourceRepository(JsonRecordRepository[DataSource]):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "sources", DataSource)


class ExecutionRepository(JsonRecordRepository[ExecutionRecord]):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "executions", ExecutionRecord)


class ArtifactRepository(JsonRecordRepository[Artifact]):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "artifacts", Artifact)


class EvidenceRepository(JsonRecordRepository[EvidenceItem]):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "evidence", EvidenceItem)


class FindingRepository(JsonRecordRepository[ValidationFinding]):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "findings", ValidationFinding)
