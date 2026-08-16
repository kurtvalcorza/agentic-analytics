from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from agentic_analytics.models import ValidationScope
from agentic_analytics.runtime import Runtime


def register_validation_tools(server: MCPServer[Any], runtime: Runtime) -> None:
    @server.tool(description="Run deterministic provenance and analytical validation checks.")
    def validate_analysis(
        session_id: str,
        claim_texts: list[str] | None = None,
        checks: list[str] | str | None = None,
        duplicate_keys: dict[str, list[str]] | None = None,
        scope: str = "final",
    ) -> dict[str, Any]:
        session = runtime.sessions.get(session_id, session_id)
        # The canonical contract carries a `scope` field ("final"/"interim"); accept and
        # validate it here so a client following the contract does not fail schema validation.
        try:
            resolved_scope = ValidationScope(scope)
        except ValueError as exc:
            allowed = ", ".join(member.value for member in ValidationScope)
            raise ValueError(f"scope must be one of: {allowed}") from exc
        # Accept the documented "default"/"all" selector alongside an explicit list.
        selected_checks: list[str] | None
        if isinstance(checks, str):
            selected_checks = None if checks.lower() in {"default", "all"} else [checks]
        else:
            selected_checks = checks
        run, findings = runtime.validation.validate(
            session,
            claim_texts=claim_texts,
            checks=selected_checks,
            duplicate_keys=duplicate_keys,
            scope=resolved_scope,
        )
        # Return a bounded preview so a run with thousands of findings cannot exhaust client
        # context; the full run is persisted and retrievable via its finding_ids.
        limit = runtime.settings.max_validation_findings
        preview = findings[:limit]
        return {
            "validation_run_id": run.id,
            "status": run.status.value,
            "scope": run.scope.value,
            "checks_run": run.checks_run,
            "checks_skipped": run.checks_skipped,
            "checks_inconclusive": run.checks_inconclusive,
            "total_findings": len(findings),
            "findings_truncated": len(findings) > len(preview),
            "findings": [
                finding.model_dump(mode="json", by_alias=True) for finding in preview
            ],
        }
