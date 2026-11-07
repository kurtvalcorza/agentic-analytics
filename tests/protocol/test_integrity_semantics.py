"""INT-01/02/03/05 and OPS-04: guarantees are explicit at the MCP tool boundary."""

from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from agentic_analytics.server import build_server
from agentic_analytics.settings import Settings


async def _call(server: Any, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await server.call_tool(tool, arguments)
    assert result.structured_content is not None
    body: dict[str, Any] = result.structured_content
    return body


async def _session(
    tmp_path: Path, rows: int = 10, **overrides: Any
) -> tuple[Any, str, dict[str, str], Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.csv").write_text(
        "value\n" + "".join(f"{index}\n" for index in range(rows)), encoding="utf-8"
    )
    (workspace / "b.csv").write_text("value\n100\n200\n", encoding="utf-8")
    settings: dict[str, Any] = {
        "state_dir": tmp_path / "state",
        "allowed_workspace_roots": [workspace],
        "execution_backend": "subprocess_dev",
        "min_free_disk_bytes": 0,
    }
    settings.update(overrides)
    server = build_server(Settings(**settings))
    created = await _call(server, "create_session", {"workspace_root": ".", "mode": "permissive"})
    session_id = created["session_id"]
    sources = {}
    for name in ("a.csv", "b.csv"):
        inspected = await _call(
            server, "inspect_source", {"session_id": session_id, "source": name}
        )
        sources[name] = inspected["source_id"]
    return server, session_id, sources, workspace


# --- INT-03: query result completeness -------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("rows", "complete"), [(5, True), (6, False)], ids=["at-boundary", "over-boundary"]
)
async def test_query_reports_full_result_completeness(
    tmp_path: Path, rows: int, complete: bool
) -> None:
    server, session_id, sources, _ = await _session(
        tmp_path, rows=rows, max_spill_rows=5, max_query_rows=2
    )
    # Deliberately no ORDER BY: completeness must not depend on ordering.
    body = await _call(
        server,
        "query_data",
        {"session_id": session_id, "sql": f"SELECT * FROM source('{sources['a.csv']}')"},
    )

    assert body["truncated"] is True
    assert body["result_complete"] is complete
    assert body["spill_truncated"] is (not complete)
    assert body["max_spill_rows"] == 5
    assert body["artifact_row_count"] == 5
    assert body["result_row_count"] == (rows if complete else None)
    assert (body["completeness_note"] is None) is complete
    artifact = await _call(
        server, "get_artifact", {"session_id": session_id, "artifact_id": body["artifact_id"]}
    )
    metadata = artifact["artifact"]["metadata"]
    assert metadata["spill_row_count"] == 5
    assert metadata["result_complete"] is complete


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("rows", "complete", "artifact_rows"), [(6, True, None), (20, False, 10)],
    ids=["fits-preview", "overflows-both"],
)
async def test_spill_cap_below_preview_limit_stays_consistent(
    tmp_path: Path, rows: int, complete: bool, artifact_rows: int | None
) -> None:
    # Asymmetric limits: max_spill_rows (5) < max_query_rows (10).
    server, session_id, sources, _ = await _session(
        tmp_path, rows=rows, max_spill_rows=5, max_query_rows=10
    )
    body = await _call(
        server,
        "query_data",
        {
            "session_id": session_id,
            "sql": f"SELECT * FROM source('{sources['a.csv']}')",
            "max_rows": 10,
        },
    )

    assert body["result_complete"] is complete
    # An incomplete result always has a truncated preview and a published artifact.
    assert body["truncated"] is (not complete)
    assert (body["artifact_id"] is None) is complete
    assert body["artifact_row_count"] == artifact_rows
    if complete:
        assert body["row_count_returned"] == rows


@pytest.mark.anyio
async def test_small_query_is_complete_without_artifact(tmp_path: Path) -> None:
    server, session_id, sources, _ = await _session(tmp_path, rows=3)
    body = await _call(
        server,
        "query_data",
        {"session_id": session_id, "sql": f"SELECT * FROM source('{sources['a.csv']}')"},
    )

    assert body["truncated"] is False
    assert body["result_complete"] is True
    assert body["artifact_id"] is None
    assert body["result_row_count"] == 3


@pytest.mark.anyio
async def test_require_complete_fails_closed_and_is_audited(tmp_path: Path) -> None:
    server, session_id, sources, _ = await _session(
        tmp_path, rows=20, max_spill_rows=5, max_query_rows=2
    )
    with pytest.raises(ToolError, match="require_complete"):
        await server.call_tool(
            "query_data",
            {
                "session_id": session_id,
                "sql": f"SELECT * FROM source('{sources['a.csv']}')",
                "require_complete": True,
            },
        )
    listed = await _call(server, "list_artifacts", {"session_id": session_id})
    assert listed["total"] == 0


# --- INT-02 / INT-05: coverage-aware validation ------------------------------------------


@pytest.mark.anyio
async def test_single_trivial_check_cannot_report_validated(tmp_path: Path) -> None:
    server, session_id, _, _ = await _session(tmp_path)
    body = await _call(
        server,
        "validate_analysis",
        {"session_id": session_id, "checks": ["causal_language"]},
    )

    assert body["status"] == "partial"
    assert body["coverage_complete"] is False
    assert body["total_findings"] == 0
    assert "evidence_coverage" in body["checks_skipped"]
    assert "Not verified" in body["assurance"]


@pytest.mark.anyio
async def test_full_baseline_without_claims_is_not_validated(tmp_path: Path) -> None:
    server, session_id, _, _ = await _session(tmp_path)
    body = await _call(server, "validate_analysis", {"session_id": session_id})

    assert body["status"] != "validated"
    assert body["coverage_complete"] is False


@pytest.mark.anyio
async def test_declared_causal_design_is_disclosed_as_assertion(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = build_server(
        Settings(
            state_dir=tmp_path / "state",
            allowed_workspace_roots=[workspace],
            execution_backend="subprocess_dev",
        )
    )
    created = await _call(
        server,
        "create_session",
        {"workspace_root": ".", "mode": "permissive", "causal_design": True},
    )
    body = await _call(
        server,
        "validate_analysis",
        {
            "session_id": created["session_id"],
            "checks": ["causal_language"],
            "claim_texts": ["The campaign caused higher sales."],
        },
    )

    codes = {finding["code"]: finding["severity"] for finding in body["findings"]}
    assert codes == {"CAUSAL_DESIGN_ASSERTED": "info"}
    assert body["status"] == "partial"


# --- INT-01: declared versus enforced lineage ---------------------------------------------


@pytest.mark.anyio
async def test_python_declaring_a_but_reading_b_is_only_declared_lineage(
    tmp_path: Path,
) -> None:
    server, session_id, sources, _ = await _session(tmp_path)
    executed = await _call(
        server,
        "execute_python",
        {
            "session_id": session_id,
            # Declares a.csv but reads b.csv.
            "code": "print(sum(int(v) for v in open('b.csv').read().split()[1:]))",
            "source_ids": [sources["a.csv"]],
        },
    )
    assert executed["status"] == "succeeded"
    assert executed["source_attestation"] == "declared"

    evidence = await _call(
        server,
        "register_evidence",
        {
            "session_id": session_id,
            "classification": "derived_fact",
            "claim": "The total of a is 300.",
            "source_ids": [sources["a.csv"]],
            "execution_ids": [executed["execution_id"]],
            "value": 300,
        },
    )
    # Accepted as declared provenance, never labelled as enforced data usage.
    assert evidence["source_lineage"] == "declared"

    interpretation = await _call(
        server,
        "register_evidence",
        {
            "session_id": session_id,
            "classification": "interpretation",
            "claim": "Totals are large.",
            "evidence_ids": [evidence["id"]],
        },
    )
    assert interpretation["source_lineage"] == "declared"


@pytest.mark.anyio
async def test_sql_evidence_lineage_is_enforced(tmp_path: Path) -> None:
    server, session_id, sources, _ = await _session(tmp_path)
    sql = f"SELECT sum(value) AS s FROM source('{sources['b.csv']}')"
    queried = await _call(server, "query_data", {"session_id": session_id, "sql": sql})
    assert queried["source_attestation"] == "enforced"
    fact = await _call(
        server,
        "register_evidence",
        {
            "session_id": session_id,
            "classification": "derived_fact",
            "claim": "The total of b is 300.",
            "source_ids": [sources["b.csv"]],
            "execution_ids": [queried["execution_id"]],
            "value": 300,
        },
    )
    assert fact["source_lineage"] == "enforced"
    direct = await _call(
        server,
        "register_evidence",
        {
            "session_id": session_id,
            "classification": "source_fact",
            "claim": "b has a value column.",
            "source_ids": [sources["b.csv"]],
        },
    )
    assert direct["source_lineage"] == "declared"


# --- OPS-04: bounded list responses --------------------------------------------------------


@pytest.mark.anyio
async def test_list_evidence_and_artifacts_paginate(tmp_path: Path) -> None:
    server, session_id, sources, _ = await _session(tmp_path, max_list_page_size=2)
    for index in range(5):
        await _call(
            server,
            "register_evidence",
            {
                "session_id": session_id,
                "classification": "source_fact",
                "claim": f"Fact {index}.",
                "source_ids": [sources["a.csv"]],
            },
        )
    await _call(
        server,
        "execute_python",
        {
            "session_id": session_id,
            "code": "for i in range(3):\n    open(f'out{i}.txt', 'w').write(str(i))",
        },
    )

    seen: list[str] = []
    offset: int | None = 0
    while offset is not None:
        page = await _call(
            server,
            "list_evidence",
            {"session_id": session_id, "offset": offset, "limit": 50},
        )
        assert page["count"] <= 2
        assert page["total"] == 5
        seen.extend(item["id"] for item in page["evidence"])
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == 5

    artifacts = await _call(server, "list_artifacts", {"session_id": session_id, "limit": 1})
    assert artifacts["count"] == 1
    assert artifacts["total"] == 3
    assert artifacts["truncated"] is True
    assert artifacts["next_offset"] == 1
    with pytest.raises(ToolError):
        await server.call_tool("list_artifacts", {"session_id": session_id, "offset": -1})


@pytest.mark.anyio
async def test_source_discovery_reports_scan_cap(tmp_path: Path) -> None:
    server, session_id, _, workspace = await _session(tmp_path, max_discovery_scan_entries=1)
    for index in range(5):
        (workspace / f"extra{index}.csv").write_text("v\n1\n", encoding="utf-8")
    body = await _call(server, "list_sources", {"session_id": session_id})

    assert body["scan_truncated"] is True
    assert body["truncated"] is True
