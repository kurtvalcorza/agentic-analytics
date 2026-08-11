# Data Model: Agent-Agnostic Analytical Runtime

## Overview

The model is a provenance graph scoped to an `AnalysisSession`. Canonical IDs are opaque strings with type prefixes for debuggability but MUST NOT encode mutable filesystem locations.

## AnalysisSession

| Field | Type | Required | Notes |
|---|---|---:|---|
| `id` | string | yes | `ses_*` |
| `created_at` | datetime | yes | UTC |
| `updated_at` | datetime | yes | UTC |
| `workspace_root` | string | yes | canonical server-side path, not necessarily returned to clients |
| `mode` | enum | yes | `strict`, `permissive` |
| `status` | enum | yes | `active`, `completed`, `cancelled`, `failed` |
| `protocol_version` | string | yes | canonical contract version |
| `metadata` | object | no | non-critical host/user metadata |

### Invariants

- All referenced entities belong to exactly one session.
- Session IDs never grant filesystem authority by themselves.
- `strict` sessions may not validate material derived facts backed only by external execution.

## DataSource

| Field | Type | Required | Notes |
|---|---|---:|---|
| `id` | string | yes | `src_*` |
| `session_id` | string | yes | parent session |
| `kind` | enum | yes | `csv`, `parquet`, `json`, `excel`, `database`, `other` |
| `display_name` | string | yes | human readable |
| `relative_path` | string/null | conditional | for workspace files |
| `uri` | string/null | conditional | for non-file sources |
| `read_only` | boolean | yes | default true |
| `fingerprint` | object | yes | hash/size/mtime or source-specific fingerprint |
| `schema` | array | no | field name/type/nullability metadata |
| `row_count` | integer/null | no | may be expensive/unknown |
| `profile` | object | no | bounded profiling metadata |
| `registered_at` | datetime | yes | UTC |

### Invariants

- At least one of `relative_path` or `uri` is present.
- Workspace file paths resolve inside authorized roots.
- Fingerprints are immutable per source record; changed files produce stale-source findings or new source records.

## ExecutionRecord

| Field | Type | Required | Notes |
|---|---|---:|---|
| `id` | string | yes | `exe_*` |
| `session_id` | string | yes | parent session |
| `execution_type` | enum | yes | `managed_python`, `managed_sql`, `semantic`, `external` |
| `status` | enum | yes | `pending`, `running`, `succeeded`, `failed`, `timed_out`, `cancelled` |
| `request` | object | yes | code/query/operation and parameters |
| `source_ids` | array[string] | yes | may be empty for source-independent work |
| `source_fingerprints` | object | yes | captured at execution start |
| `started_at` | datetime | yes | UTC |
| `completed_at` | datetime/null | no | UTC |
| `runtime` | object | yes | backend, Python/DuckDB version, package metadata where feasible |
| `stdout_preview` | string | no | bounded |
| `stderr_preview` | string | no | bounded |
| `result_preview` | object/string | no | bounded |
| `truncated` | boolean | yes | indicates preview truncation |
| `artifact_ids` | array[string] | yes | produced or modified artifacts |
| `error` | object/null | no | structured failure metadata |

### Invariants

- Records are immutable after terminal status except for append-only indexing fields.
- Material evidence cannot reference pending/running executions.
- `external` records must contain caller-provided reproducibility metadata and are explicitly marked non-managed.

## Artifact

| Field | Type | Required | Notes |
|---|---|---:|---|
| `id` | string | yes | `art_*` |
| `session_id` | string | yes | parent session |
| `execution_id` | string/null | no | origin execution |
| `kind` | enum | yes | `table`, `chart`, `dataset`, `script`, `report`, `file`, `other` |
| `display_name` | string | yes | human readable |
| `relative_path` | string | yes | inside session workspace/artifact root |
| `media_type` | string | yes | IANA media type where known |
| `size_bytes` | integer | yes | |
| `sha256` | string | yes | |
| `created_at` | datetime | yes | UTC |
| `lineage` | object | no | source/execution/artifact parent relationships |
| `metadata` | object | no | dimensions, row count, chart info, etc. |

### Invariants

- Artifact paths remain inside allowed artifact roots.
- Hash is computed by server for managed artifacts.
- Same physical file modified later becomes a new artifact version/record.

## EvidenceItem

| Field | Type | Required | Notes |
|---|---|---:|---|
| `id` | string | yes | `evd_*` |
| `session_id` | string | yes | parent session |
| `classification` | enum | yes | `source_fact`, `derived_fact`, `interpretation`, `recommendation` |
| `claim` | string | yes | concise human-readable statement |
| `material` | boolean | yes | whether required for final validation |
| `source_ids` | array[string] | conditional | required for source/derived facts as applicable |
| `execution_ids` | array[string] | conditional | required for derived facts |
| `artifact_ids` | array[string] | no | supporting outputs |
| `evidence_ids` | array[string] | conditional | upstream evidence for interpretation/recommendation |
| `value` | any | no | normalized scalar/object where useful |
| `units` | string/null | no | |
| `method_summary` | string/null | no | human-readable method |
| `created_at` | datetime | yes | UTC |
| `created_by` | object | no | host/agent metadata, never required for semantics |

### Validation Rules

- `source_fact`: at least one `source_id`; no execution required.
- `derived_fact`: at least one `source_id` and one successful `execution_id`.
- `interpretation`: at least one upstream `evidence_id` of class source/derived/interpretation.
- `recommendation`: at least one upstream `evidence_id`.
- Evidence graph must be acyclic.

## ValidationFinding

| Field | Type | Required | Notes |
|---|---|---:|---|
| `id` | string | yes | `val_*` |
| `session_id` | string | yes | |
| `code` | string | yes | stable machine-readable finding code |
| `severity` | enum | yes | `info`, `warning`, `error`, `blocking` |
| `status` | enum | yes | `open`, `resolved`, `accepted` |
| `message` | string | yes | |
| `entity_refs` | array[object] | yes | affected source/execution/evidence/artifact/report IDs |
| `check` | string | yes | validator/check name |
| `details` | object | no | observed/expected values |
| `remediation` | string/null | no | |
| `created_at` | datetime | yes | UTC |

## AnalysisReport

| Field | Type | Required | Notes |
|---|---|---:|---|
| `id` | string | yes | `rpt_*` |
| `session_id` | string | yes | |
| `artifact_id` | string | yes | report artifact |
| `material_evidence_ids` | array[string] | yes | |
| `validation_run_id` | string/null | no | latest validation basis |
| `validation_status` | enum | yes | `unvalidated`, `warnings`, `validated`, `blocked` |
| `created_at` | datetime | yes | UTC |

## Relationship Summary

```text
AnalysisSession
 ├── DataSource
 ├── ExecutionRecord ──> DataSource
 │       └── Artifact
 ├── EvidenceItem
 │     ├── DataSource
 │     ├── ExecutionRecord
 │     ├── Artifact
 │     └── EvidenceItem (upstream DAG)
 ├── ValidationFinding ──> any scoped entity
 └── AnalysisReport ──> Artifact + Evidence + Validation run
```
