"""Canonical MCP product boundary."""

from typing import Any

from mcp.server.fastmcp import FastMCP

from .runtime import AnalyticsRuntime

mcp = FastMCP("agentic-analytics")
runtime = AnalyticsRuntime()


@mcp.tool()
def create_session(workspace_root: str = ".", mode: str = "strict") -> dict[str, Any]:
    """Create a session-scoped analysis workspace in strict or permissive mode."""
    return runtime.create_session(workspace_root, mode)


@mcp.tool()
def list_sources(session_id: str, include: list[str] | None = None, recursive: bool = True) -> list[dict[str, Any]]:
    """Discover bounded CSV and Parquet source descriptors in the authorized workspace."""
    return runtime.list_sources(session_id, include, recursive)


@mcp.tool()
def inspect_source(session_id: str, source: str, profile: str = "standard", sample_rows: int = 20) -> dict[str, Any]:
    """Register, fingerprint, inspect, and sample an authorized data source."""
    return runtime.inspect_source(session_id, source, profile, sample_rows)


@mcp.tool()
def query_data(session_id: str, sql: str, max_rows: int = 200) -> dict[str, Any]:
    """Run bounded, read-only DuckDB SQL against source('src_...') references."""
    return runtime.query_data(session_id, sql, max_rows)


@mcp.tool()
def execute_python(session_id: str, code: str, source_ids: list[str] | None = None, timeout_seconds: int = 120) -> dict[str, Any]:
    """Execute Python in the configured managed backend and register generated artifacts."""
    return runtime.execute_python(session_id, code, source_ids, timeout_seconds)


@mcp.tool()
def register_external_execution(session_id: str, kind: str, code_or_query: str, source_ids: list[str], runtime_metadata: dict[str, Any], result_summary: dict[str, Any], artifact_paths: list[str] | None = None) -> dict[str, Any]:
    """Register explicitly external computation in a permissive session."""
    return runtime.register_external_execution(session_id, kind, code_or_query, source_ids, runtime_metadata, result_summary, artifact_paths)


@mcp.tool()
def register_evidence(session_id: str, classification: str, claim: str, material: bool = False,
                      source_ids: list[str] | None = None, execution_ids: list[str] | None = None,
                      artifact_ids: list[str] | None = None, evidence_ids: list[str] | None = None,
                      value: Any = None, units: str | None = None, method_summary: str | None = None) -> dict[str, Any]:
    """Append an immutable, classified evidence item with verified lineage."""
    return runtime.register_evidence(session_id=session_id, classification=classification, claim=claim,
        material=material, source_ids=source_ids or [], execution_ids=execution_ids or [],
        artifact_ids=artifact_ids or [], evidence_ids=evidence_ids or [], value=value, units=units,
        method_summary=method_summary)


@mcp.tool()
def list_evidence(session_id: str, classification: str | None = None, material: bool | None = None) -> list[dict[str, Any]]:
    """List evidence ledger entries with optional classification and materiality filters."""
    return runtime.list_evidence(session_id, classification, material)


@mcp.tool()
def validate_analysis(session_id: str, scope: str = "final", claim_texts: list[str] | None = None, checks: str = "default") -> dict[str, Any]:
    """Run independent deterministic provenance and analytical validation checks."""
    return runtime.validate_analysis(session_id, scope, claim_texts, checks)


@mcp.tool()
def challenge_analysis(session_id: str) -> dict[str, Any]:
    """Report performed, skipped, and inconclusive robustness diagnostics."""
    return runtime.challenge_analysis(session_id)


@mcp.tool()
def list_artifacts(session_id: str) -> list[dict[str, Any]]:
    """List canonical artifacts for a session."""
    return runtime.list_artifacts(session_id)


@mcp.tool()
def get_artifact(session_id: str, artifact_id: str) -> dict[str, Any]:
    """Get canonical artifact metadata and its local resource URI."""
    return runtime.get_artifact(session_id, artifact_id)

