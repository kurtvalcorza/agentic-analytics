from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from .base import Record, utc_now


class EvidenceItem(Record):
    id: str
    session_id: str
    classification: Literal["source_fact", "derived_fact", "interpretation", "recommendation"]
    claim: str
    material: bool = False
    source_ids: list[str] = Field(default_factory=list)
    execution_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    value: Any = None
    units: str | None = None
    method_summary: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def valid_lineage(self) -> "EvidenceItem":
        if self.classification == "source_fact" and not self.source_ids:
            raise ValueError("source facts require source lineage")
        if self.classification == "derived_fact" and (not self.source_ids or not self.execution_ids):
            raise ValueError("derived facts require source and execution lineage")
        if self.classification in {"interpretation", "recommendation"} and not self.evidence_ids:
            raise ValueError(f"{self.classification}s require upstream evidence")
        return self
