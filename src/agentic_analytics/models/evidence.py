from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field, model_validator

from agentic_analytics.ids import EntityType, new_id

from .common import CanonicalModel, utc_now


class EvidenceClassification(StrEnum):
    SOURCE_FACT = "source_fact"
    DERIVED_FACT = "derived_fact"
    INTERPRETATION = "interpretation"
    RECOMMENDATION = "recommendation"


class EvidenceItem(CanonicalModel):
    id: str = Field(default_factory=lambda: new_id(EntityType.EVIDENCE))
    session_id: str
    classification: EvidenceClassification
    claim: str = Field(min_length=1)
    material: bool = True
    source_ids: list[str] = Field(default_factory=list)
    execution_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    value: Any | None = None
    units: str | None = None
    method_summary: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    created_by: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_classification_links(self) -> EvidenceItem:
        match self.classification:
            case EvidenceClassification.SOURCE_FACT:
                if not self.source_ids:
                    raise ValueError("source_fact requires at least one source_id")
            case EvidenceClassification.DERIVED_FACT:
                if not self.source_ids or not self.execution_ids:
                    raise ValueError("derived_fact requires source_ids and execution_ids")
            case EvidenceClassification.INTERPRETATION | EvidenceClassification.RECOMMENDATION:
                if not self.evidence_ids:
                    raise ValueError(f"{self.classification.value} requires upstream evidence_ids")
        if self.id in self.evidence_ids:
            raise ValueError("evidence item may not depend on itself")
        return self


def ensure_acyclic_evidence(items: Iterable[EvidenceItem]) -> None:
    """Raise ValueError when the supplied evidence dependency graph contains a cycle."""

    by_id = {item.id: item for item in items}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError(f"evidence dependency cycle detected at {node_id}")
        if node_id in visited:
            return
        visiting.add(node_id)
        item = by_id.get(node_id)
        if item is not None:
            for upstream_id in item.evidence_ids:
                if upstream_id in by_id:
                    visit(upstream_id)
        visiting.remove(node_id)
        visited.add(node_id)

    for evidence_id in by_id:
        visit(evidence_id)
