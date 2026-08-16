from __future__ import annotations

from dataclasses import dataclass

from .execution_backends import DockerBackend, ExecutionBackend, SubprocessDevBackend
from .repositories import (
    ArtifactRepository,
    EvidenceRepository,
    ExecutionRepository,
    FindingRepository,
    SessionRepository,
    SourceRepository,
    ValidationRunRepository,
)
from .services.artifact_registry import ArtifactRegistry
from .services.evidence_ledger import EvidenceLedger
from .services.execution import ExecutionService
from .services.inspector import InspectorService
from .services.query import QueryService
from .services.validation import ValidationService
from .services.workspace import WorkspaceService
from .settings import Settings


@dataclass(slots=True)
class Runtime:
    settings: Settings
    sessions: SessionRepository
    sources: SourceRepository
    executions: ExecutionRepository
    artifacts: ArtifactRepository
    evidence: EvidenceRepository
    findings: FindingRepository
    validation_runs: ValidationRunRepository
    workspace: WorkspaceService
    inspector: InspectorService
    query: QueryService
    execution_backend: ExecutionBackend
    execution: ExecutionService
    evidence_ledger: EvidenceLedger
    validation: ValidationService
    artifact_registry: ArtifactRegistry

    @classmethod
    def create(cls, settings: Settings | None = None) -> Runtime:
        resolved = settings or Settings()
        resolved.ensure_directories()
        sessions = SessionRepository(resolved.state_dir)
        sources = SourceRepository(resolved.state_dir)
        executions = ExecutionRepository(resolved.state_dir)
        artifact_repository = ArtifactRepository(resolved.state_dir)
        evidence_repository = EvidenceRepository(resolved.state_dir)
        findings = FindingRepository(resolved.state_dir)
        validation_runs = ValidationRunRepository(resolved.state_dir)
        workspace = WorkspaceService(resolved.normalized_allowed_roots())
        inspector = InspectorService(sources, workspace, resolved)
        query = QueryService(sources, executions, workspace, resolved)
        backend: ExecutionBackend
        if resolved.execution_backend == "docker":
            backend = DockerBackend(
                resolved.docker_image,
                memory=resolved.docker_memory,
                cpus=resolved.docker_cpus,
                pids_limit=resolved.docker_pids_limit,
                max_output_chars=resolved.max_output_chars,
            )
        else:
            backend = SubprocessDevBackend()
        registry = ArtifactRegistry(
            artifact_repository,
            resolved.state_dir / "artifacts",
            max_artifacts=resolved.max_artifacts_per_execution,
            max_artifact_bytes=resolved.max_artifact_bytes,
            max_total_bytes=resolved.max_total_artifact_bytes,
        )
        execution = ExecutionService(
            backend, executions, sources, registry, workspace, resolved
        )
        evidence_ledger = EvidenceLedger(
            evidence_repository,
            sources,
            executions,
            artifact_repository,
        )
        validation = ValidationService(
            evidence_repository,
            sources,
            findings,
            validation_runs,
        )
        return cls(
            resolved,
            sessions,
            sources,
            executions,
            artifact_repository,
            evidence_repository,
            findings,
            validation_runs,
            workspace,
            inspector,
            query,
            backend,
            execution,
            evidence_ledger,
            validation,
            registry,
        )
