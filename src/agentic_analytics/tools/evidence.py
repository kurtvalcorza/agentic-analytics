from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from agentic_analytics.models import EvidenceClassification
from agentic_analytics.runtime import Runtime


def register_evidence_tools(server: MCPServer[Any], runtime: Runtime) -> None:
    @server.tool(description="Register an immutable evidence item with validated provenance links.")
    def register_evidence(
        session_id: str,
        classification: EvidenceClassification,
        claim: str,
        material: bool = True,
        source_ids: list[str] | None = None,
        execution_ids: list[str] | None = None,
        artifact_ids: list[str] | None = None,
        evidence_ids: list[str] | None = None,
        value: Any | None = None,
        units: str | None = None,
        method_summary: str | None = None,
    ) -> dict[str, Any]:
        runtime.sessions.get(session_id, session_id)
        item = runtime.evidence_ledger.register(
            session_id,
            classification,
            claim,
            material=material,
            source_ids=source_ids,
            execution_ids=execution_ids,
            artifact_ids=artifact_ids,
            evidence_ids=evidence_ids,
            value=value,
            units=units,
            method_summary=method_summary,
        )
        return item.model_dump(mode="json", by_alias=True)

    @server.tool(description="List session evidence with optional provenance filters.")
    def list_evidence(
        session_id: str,
        classification: EvidenceClassification | None = None,
        material: bool | None = None,
        source_id: str | None = None,
        execution_id: str | None = None,
        upstream_evidence_id: str | None = None,
    ) -> dict[str, Any]:
        runtime.sessions.get(session_id, session_id)
        items = runtime.evidence_ledger.list(
            session_id,
            classification=classification,
            material=material,
            source_id=source_id,
            execution_id=execution_id,
            upstream_evidence_id=upstream_evidence_id,
        )
        serialized = [item.model_dump(mode="json", by_alias=True) for item in items]
        return {"evidence": serialized, "count": len(serialized)}
