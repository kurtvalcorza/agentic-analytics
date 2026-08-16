from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from agentic_analytics.runtime import Runtime


def register_validation_tools(server: MCPServer[Any], runtime: Runtime) -> None:
    @server.tool(description="Run deterministic provenance and analytical validation checks.")
    def validate_analysis(
        session_id: str,
        claim_texts: list[str] | None = None,
        checks: list[str] | None = None,
    ) -> dict[str, Any]:
        session = runtime.sessions.get(session_id, session_id)
        run, findings = runtime.validation.validate(
            session,
            claim_texts=claim_texts,
            checks=checks,
        )
        return {
            "validation_run_id": run.id,
            "status": run.status.value,
            "checks_run": run.checks_run,
            "checks_skipped": run.checks_skipped,
            "checks_inconclusive": run.checks_inconclusive,
            "findings": [
                finding.model_dump(mode="json", by_alias=True) for finding in findings
            ],
        }
