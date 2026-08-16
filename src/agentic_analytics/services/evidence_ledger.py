from __future__ import annotations

from typing import Any

from agentic_analytics.models import (
    EvidenceClassification,
    EvidenceItem,
    ExecutionStatus,
    ensure_acyclic_evidence,
)
from agentic_analytics.repositories import (
    ArtifactRepository,
    EvidenceRepository,
    ExecutionRepository,
    SourceRepository,
)


class EvidenceRegistrationError(ValueError):
    pass


class EvidenceLedger:
    def __init__(
        self,
        evidence: EvidenceRepository,
        sources: SourceRepository,
        executions: ExecutionRepository,
        artifacts: ArtifactRepository,
    ) -> None:
        self.evidence = evidence
        self.sources = sources
        self.executions = executions
        self.artifacts = artifacts

    def register(
        self,
        session_id: str,
        classification: EvidenceClassification,
        claim: str,
        *,
        material: bool = True,
        source_ids: list[str] | None = None,
        execution_ids: list[str] | None = None,
        artifact_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        value: Any | None = None,
        units: str | None = None,
        method_summary: str | None = None,
    ) -> EvidenceItem:
        item = EvidenceItem(
            session_id=session_id,
            classification=classification,
            claim=claim,
            material=material,
            source_ids=list(dict.fromkeys(source_ids or [])),
            execution_ids=list(dict.fromkeys(execution_ids or [])),
            artifact_ids=list(dict.fromkeys(artifact_ids or [])),
            evidence_ids=list(dict.fromkeys(evidence_ids or [])),
            value=value,
            units=units,
            method_summary=method_summary,
        )
        self._validate_references(item)
        existing = self.evidence.list(session_id)
        ensure_acyclic_evidence([*existing, item])
        return self.evidence.add(item)

    def _validate_references(self, item: EvidenceItem) -> None:
        for source_id in item.source_ids:
            self.sources.get(item.session_id, source_id)
        executions = [
            self.executions.get(item.session_id, execution_id)
            for execution_id in item.execution_ids
        ]
        for artifact_id in item.artifact_ids:
            self.artifacts.get(item.session_id, artifact_id)
        upstream_evidence = [
            self.evidence.get(item.session_id, evidence_id)
            for evidence_id in item.evidence_ids
        ]

        if item.classification is EvidenceClassification.DERIVED_FACT:
            unsuccessful = [
                execution.id
                for execution in executions
                if execution.status is not ExecutionStatus.SUCCEEDED
            ]
            if unsuccessful:
                joined = ", ".join(unsuccessful)
                raise EvidenceRegistrationError(
                    f"derived_fact requires successful executions; not successful: {joined}"
                )
            # The source -> execution -> evidence chain must be real: every cited source must
            # actually have been used by one of the referenced executions.
            covered_sources: set[str] = set()
            for execution in executions:
                covered_sources.update(execution.source_ids)
            missing = [
                source_id for source_id in item.source_ids if source_id not in covered_sources
            ]
            if missing:
                joined = ", ".join(missing)
                raise EvidenceRegistrationError(
                    "derived_fact sources must be used by a referenced execution; "
                    f"unlinked sources: {joined}"
                )

        if item.classification is EvidenceClassification.INTERPRETATION:
            # Interpretation inputs are limited to facts and other interpretations; a
            # recommendation is a downstream classification and cannot be an input.
            recommendations = [
                upstream.id
                for upstream in upstream_evidence
                if upstream.classification is EvidenceClassification.RECOMMENDATION
            ]
            if recommendations:
                joined = ", ".join(recommendations)
                raise EvidenceRegistrationError(
                    "interpretation inputs must be source facts, derived facts, or "
                    f"interpretations; recommendations rejected: {joined}"
                )

    def list(
        self,
        session_id: str,
        *,
        classification: EvidenceClassification | None = None,
        material: bool | None = None,
        source_id: str | None = None,
        execution_id: str | None = None,
        upstream_evidence_id: str | None = None,
    ) -> list[EvidenceItem]:
        items = self.evidence.list(session_id)
        return [
            item
            for item in items
            if (classification is None or item.classification is classification)
            and (material is None or item.material is material)
            and (source_id is None or source_id in item.source_ids)
            and (execution_id is None or execution_id in item.execution_ids)
            and (
                upstream_evidence_id is None
                or upstream_evidence_id in item.evidence_ids
            )
        ]
