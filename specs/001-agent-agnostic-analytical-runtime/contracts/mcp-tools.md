# MCP Tool Contract

**Protocol contract version**: `0.1.0`

The names below are canonical capability names for v1. Implementations MAY namespace them (for example `analytics.session.create`) if required by the MCP framework, but semantics and schemas must remain equivalent.

## `create_session`

Creates or resumes an analysis workspace policy context.

### Input

```json
{
  "workspace_root": ".",
  "mode": "strict"
}
```

### Output

```json
{
  "session_id": "ses_...",
  "mode": "strict",
  "protocol_version": "0.1.0",
  "capabilities": {
    "managed_python": true,
    "duckdb": true,
    "external_execution_registration": true
  }
}
```

## `close_session`

Transitions an active session to a terminal status, releasing its workspace so a new session can reuse it and freeing managed backend resources.

### Input

```json
{
  "session_id": "ses_...",
  "status": "completed"
}
```

`status` must be `completed` or `cancelled` (default `completed`).

### Output

```json
{
  "session_id": "ses_...",
  "status": "completed",
  "workspace_root": "/abs/workspace"
}
```

### Requirements

- The status transition is persisted durably, so a workspace is no longer treated as occupied after a server restart.
- Backend resource cleanup is best-effort; the persisted status transition is authoritative.
- Closing an already-terminal session is idempotent and returns its current status.

## `list_sources`

Discovers supported data sources within the authorized workspace.

### Input

```json
{
  "session_id": "ses_...",
  "include": ["*.csv", "*.parquet"],
  "recursive": true
}
```

### Output

Bounded array of source descriptors with source IDs, type, display name, relative path, size, and registration state.

## `inspect_source`

Registers (if necessary) and inspects one source.

### Input

```json
{
  "session_id": "ses_...",
  "source": "data/survey.csv",
  "profile": "standard",
  "sample_rows": 20
}
```

### Output

```json
{
  "source_id": "src_...",
  "kind": "csv",
  "fingerprint": {"sha256": "...", "size_bytes": 12345},
  "schema": [{"name": "age", "type": "BIGINT", "nullable": true}],
  "row_count": 120,
  "profile": {
    "null_counts": {"age": 2},
    "duplicate_row_count": 0
  },
  "sample": [...],
  "sample_truncated": false
}
```

## `query_data`

Executes read-only DuckDB analytical SQL against registered sources.

### Input

```json
{
  "session_id": "ses_...",
  "sql": "SELECT region, count(*) AS n FROM source('src_...') GROUP BY region",
  "max_rows": 200
}
```

### Requirements

- SQL must be read-only.
- Source references must resolve only to session-authorized sources.
- Oversized result sets produce an artifact and bounded preview. The preview is bounded before values are materialized — per cell (in SQL), per row (byte budget, including the first row), and by column count — so neither a very large cell, a very wide row, nor unbounded column metadata can exhaust server memory. Dropped rows/cells/columns are preserved in the artifact.
- A spilled artifact is subject to the configured artifact byte limits; the write is interrupted at the remaining session budget so it cannot over-write on disk, and a spill that exceeds the limits is rejected rather than persisted. The session-cumulative quota is reserved atomically per session, so concurrent spills cannot jointly exceed it.
- Returns an `execution_id` even for successful inline results.

### Output

```json
{
  "execution_id": "exe_...",
  "columns": ["region", "n"],
  "rows": [["NCR", 42]],
  "row_count_returned": 1,
  "truncated": false,
  "artifact_id": null
}
```

## `execute_python`

Runs Python inside the managed execution backend.

### Input

```json
{
  "session_id": "ses_...",
  "code": "...",
  "source_ids": ["src_..."],
  "timeout_seconds": 120
}
```

### Output

```json
{
  "execution_id": "exe_...",
  "status": "succeeded",
  "stdout_preview": "...",
  "stderr_preview": "",
  "truncated": false,
  "artifact_ids": ["art_..."]
}
```

## `register_external_execution`

> ⏳ **Planned — not implemented in this release.** Deferred to a later `0.1.x`; the runtime currently reports `external_execution_registration: false` in `create_session` capabilities.

Registers computation performed by the host or another environment.

### Input

```json
{
  "session_id": "ses_...",
  "kind": "python",
  "code_or_query": "...",
  "source_ids": ["src_..."],
  "runtime": {"provider": "host", "python": "3.13"},
  "result_summary": {"mean": 4.2},
  "artifact_paths": ["outputs/result.csv"]
}
```

### Requirements

- Available only in permissive mode for evidence validation unless explicitly allowed by future strict-mode policy.
- Server recomputes artifact hashes and verifies paths.
- Caller-provided source fingerprints must match registered sources or produce stale/unverified findings.

## `register_evidence`

Adds an immutable evidence item.

### Input

```json
{
  "session_id": "ses_...",
  "classification": "derived_fact",
  "claim": "Adoption intent was 64% in the observed sample.",
  "material": true,
  "source_ids": ["src_..."],
  "execution_ids": ["exe_..."],
  "artifact_ids": [],
  "evidence_ids": [],
  "value": {"proportion": 0.64, "n": 120},
  "units": "proportion",
  "method_summary": "Share of valid respondents selecting likely or very likely."
}
```

### Output

Canonical `EvidenceItem`.

## `list_evidence`

Returns evidence ledger entries with filters by classification, materiality, source, execution, or upstream relationship.

## `validate_analysis`

Runs deterministic provenance and analytical validation.

### Input

```json
{
  "session_id": "ses_...",
  "scope": "final",
  "claim_texts": [
    "Adoption intent was 64%.",
    "Training caused higher adoption intent."
  ],
  "checks": "default"
}
```

### Requirements

- `scope` selects the analytical stage under review (`final` or `interim`, default `final`) and is echoed back on the run.
- `checks` accepts the `default`/`all` selector or an explicit array. An explicit empty array has zero coverage and is rejected rather than reported as a clean pass.
- Unknown check names are rejected so a typo cannot silently skip every check.

### Output

```json
{
  "validation_run_id": "vrn_...",
  "status": "blocked",
  "scope": "final",
  "checks_run": ["evidence_coverage", "causal_language", "duplicates", "missingness"],
  "checks_skipped": [],
  "findings": [
    {
      "id": "val_...",
      "code": "UNSUPPORTED_CAUSAL_CLAIM",
      "severity": "blocking",
      "message": "Causal language is unsupported by the registered observational design.",
      "entity_refs": [],
      "remediation": "Use associational wording or register a supported causal design."
    }
  ]
}
```

## `challenge_analysis`

> ⏳ **Planned — not implemented in this release.** Deferred to a later `0.1.x`. Deterministic validation is available today via `validate_analysis`.

Runs adversarial diagnostics against the registered evidence graph and source metadata.

### Input

```json
{
  "session_id": "ses_...",
  "focus": ["denominator_shift", "simpsons_paradox", "missingness", "multiple_comparisons"]
}
```

### Output

Structured check coverage plus findings and optional recommended follow-up analyses.

## `list_artifacts`

Returns canonical artifact metadata and lineage.

## `get_artifact`

Returns a resource URI or file handle/reference supported by the MCP implementation. The canonical response must include artifact metadata even if host rendering differs.
