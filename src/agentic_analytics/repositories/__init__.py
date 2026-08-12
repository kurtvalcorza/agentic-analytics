from pathlib import Path

from agentic_analytics.models import (
    AnalysisSession, Artifact, DataSource, EvidenceItem, ExecutionRecord, ValidationFinding,
)

from .base import Repository, SessionRepository


class Repositories:
    def __init__(self, root: Path) -> None:
        self.sessions = SessionRepository(root, AnalysisSession)
        self.sources = Repository(root, "sources.jsonl", DataSource)
        self.executions = Repository(root, "executions.jsonl", ExecutionRecord)
        self.artifacts = Repository(root, "artifacts.jsonl", Artifact)
        self.evidence = Repository(root, "evidence.jsonl", EvidenceItem)
        self.findings = Repository(root, "findings.jsonl", ValidationFinding)

__all__ = ["Repositories", "Repository", "SessionRepository"]

