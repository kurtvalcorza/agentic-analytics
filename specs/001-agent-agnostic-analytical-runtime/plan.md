# Implementation Plan: Agent-Agnostic Analytical Runtime

**Branch**: `001-agent-agnostic-analytical-runtime` | **Date**: 2026-08-12 | **Spec**: `spec.md`

## Summary

Build a local-first MCP analytical runtime whose durable interfaces are host-neutral tools and canonical provenance records. The v1 runtime will support session-scoped CSV/Parquet inspection, DuckDB queries, managed Python execution in Docker, artifact discovery, an evidence ledger, deterministic validation, and host-agnostic protocol conformance tests. Codex, Claude Code, and Antigravity adapters are thin optional integration layers and are not required for core correctness.

## Technical Context

**Language/Version**: Python 3.12+  
**Primary Dependencies**: MCP Python SDK, Pydantic v2, DuckDB, PyArrow, pandas or Polars for interoperability, Docker CLI/SDK abstraction, Typer for local admin/conformance CLI, pytest  
**Storage**: Append-oriented JSON/JSONL session records plus content-addressed artifact metadata for v1; repository interfaces allow later SQLite migration  
**Testing**: pytest, JSON Schema validation, protocol-level MCP tests, sandbox/security scenario tests  
**Target Platform**: macOS/Linux/Windows developer workstations with Docker-compatible execution for strict mode  
**Project Type**: Python package + MCP server + optional host adapters  
**Performance Goals**: bounded tool responses; interactive metadata operations for ordinary datasets; query 1 GB Parquet without model-context serialization  
**Constraints**: agent agnostic; server-side sandbox controls; source data local by default; no private chain-of-thought dependency  
**Scale/Scope**: single-user workstation v1; multiple concurrent sessions; local files first

## Constitution Check

### Pre-design gates

- Agent-agnostic core: **PASS** — all essential functions are MCP tools.
- MCP product boundary: **PASS** — no host-native API in core.
- Evidence before conclusion: **PASS** — evidence ledger is first-class.
- Reproducibility: **PASS** — execution/source/artifact lineage captured.
- Validation separate from generation: **PASS** — deterministic validator service.
- Server-side sandboxing: **PASS** — strict mode uses out-of-process container backend.
- Small tool surface: **PASS** — ten initial capability tools.
- No chain-of-thought requirement: **PASS** — only observable state persisted.

No constitutional exceptions requested.

## Project Structure

```text
agentic-analytics/
├── pyproject.toml
├── README.md
├── src/
│   └── agentic_analytics/
│       ├── server.py
│       ├── settings.py
│       ├── ids.py
│       ├── models/
│       │   ├── session.py
│       │   ├── source.py
│       │   ├── execution.py
│       │   ├── artifact.py
│       │   ├── evidence.py
│       │   └── validation.py
│       ├── repositories/
│       │   ├── sessions.py
│       │   ├── sources.py
│       │   ├── executions.py
│       │   ├── artifacts.py
│       │   ├── evidence.py
│       │   └── findings.py
│       ├── services/
│       │   ├── workspace.py
│       │   ├── inspector.py
│       │   ├── query.py
│       │   ├── execution.py
│       │   ├── artifact_registry.py
│       │   ├── evidence_ledger.py
│       │   ├── validation.py
│       │   └── challenge.py
│       ├── execution_backends/
│       │   ├── base.py
│       │   ├── docker.py
│       │   └── subprocess_dev.py
│       ├── validators/
│       │   ├── evidence_coverage.py
│       │   ├── stale_sources.py
│       │   ├── duplicates.py
│       │   ├── missingness.py
│       │   ├── denominator.py
│       │   └── causal_language.py
│       └── tools/
│           ├── sessions.py
│           ├── sources.py
│           ├── query.py
│           ├── execute.py
│           ├── evidence.py
│           ├── validation.py
│           └── artifacts.py
├── docker/
│   ├── Dockerfile.exec
│   └── requirements-exec.txt
├── adapters/
│   ├── codex/
│   ├── claude-code/
│   └── antigravity/
├── tests/
│   ├── unit/
│   ├── protocol/
│   ├── scenarios/
│   ├── security/
│   └── fixtures/
└── scripts/
    └── conformance.py
```

## Phase 0 — Research Outcomes

See `research.md`.

Resolved decisions:

- MCP is canonical interface.
- DuckDB is local query engine.
- Python is general managed execution runtime.
- Docker is strict-mode backend.
- Evidence ledger is canonical provenance layer.
- Deterministic validation is separate from model reasoning.
- Strict/permissive provenance modes are supported.
- JSON/JSONL storage is initial persistence format behind repositories.
- Host skills/adapters contain no unique correctness logic.

## Phase 1 — Data Model and Contracts

See:

- `data-model.md`
- `contracts/mcp-tools.md`
- `contracts/schemas.json`
- `quickstart.md`

### Tool surface

1. `create_session`
2. `list_sources`
3. `inspect_source`
4. `query_data`
5. `execute_python`
6. `register_external_execution`
7. `register_evidence`
8. `list_evidence`
9. `validate_analysis`
10. `challenge_analysis`
11. `list_artifacts`
12. `get_artifact`

The count is slightly above the preferred 10 because artifact retrieval and evidence listing are distinct durable capabilities; no further tool subdivision is planned for v1.

## Architecture

```text
MCP client (Codex / Claude Code / Antigravity / conformance harness)
                    │
                    ▼
              MCP tool layer
                    │
                    ▼
             service layer
       ┌────────────┼─────────────┐
       ▼            ▼             ▼
  inspection     provenance    validation
       │            │             │
       └──────┬─────┴──────┬─────┘
              ▼            ▼
           DuckDB     execution backend
                           │
                      Docker sandbox
                           │
                     session workspace
```

### Execution lifecycle

1. Resolve session and authorize workspace.
2. Capture source fingerprints.
3. Snapshot session workspace state.
4. Execute query/code with bounded runtime.
5. Capture stdout/stderr/result previews.
6. Snapshot workspace again.
7. Register created/modified artifacts.
8. Write immutable execution record.
9. Return bounded structured result to MCP client.

### Evidence lifecycle

1. Agent creates or identifies a claim.
2. Runtime validates evidence-class-specific required links.
3. Runtime verifies referenced entities and session ownership.
4. Runtime checks execution terminal state and source staleness.
5. Evidence item is appended immutably.
6. Downstream evidence may reference it through an acyclic dependency graph.

### Validation lifecycle

1. Select validation scope (`session`, `evidence`, `final`).
2. Run deterministic checks against registered state.
3. Persist findings.
4. Compute overall status:
   - `validated`: no blocking/error findings and material evidence complete.
   - `warnings`: no blockers, at least one warning.
   - `blocked`: any blocking finding.
5. Return check coverage, skipped checks, findings, and remediation.

## Security Design

- Resolve and canonicalize all paths before authorization.
- Read-only mount source roots into strict-mode containers when practical.
- Separate writable generated/output directory.
- Disable or restrict container network by default.
- Do not pass host environment wholesale to containers.
- Maintain explicit package/runtime image rather than permitting arbitrary host package installation in strict mode.
- Apply execution timeout and optional memory/CPU limits.
- Session-scoped container names use server-generated IDs, not raw caller IDs.
- Never return raw host absolute paths unless running in explicit debug mode.
- Reject symlink escapes across authorized roots.

## Validation Scope for v1

### Deterministic provenance checks

- evidence link completeness;
- cross-session entity references;
- unsuccessful execution references;
- stale source fingerprints;
- artifact hash/path validity;
- evidence DAG cycle detection.

### Analytical checks

- exact duplicate rows or configured key duplicates;
- high/structured missingness warning;
- denominator metadata inconsistency;
- unsupported causal language without registered causal-design metadata;
- result/sample truncation warnings when a claim appears to treat a preview as the full result.

Statistical test-specific validators are an extension point and not a v1 completeness requirement beyond selected scenario fixtures.

## Host Adapter Design

Each adapter may contain:

- install/config instructions;
- recommended tool permissions;
- optional skill/prompt explaining the evidence workflow;
- examples.

Adapters MUST NOT contain:

- hidden required tool sequences;
- unique evidence validation logic;
- host-only record formats;
- host-only execution semantics required for validation.

## Testing Strategy

### Unit

- model invariants;
- path authorization;
- fingerprints;
- evidence DAG;
- finding severity aggregation;
- bounded result serialization.

### Protocol

- every MCP tool input/output schema;
- malformed IDs and cross-session access;
- no host-specific fields required;
- schema version behavior.

### Scenarios

- descriptive CSV analysis;
- Parquet aggregation larger than context window;
- generated chart/table artifacts;
- permissive external execution;
- missing evidence blocker;
- causal wording blocker;
- Simpson's paradox challenge fixture.

### Security

- `../` traversal;
- absolute path escape;
- symlink escape;
- container attempts to read host secrets;
- timeout/infinite loop;
- excessive file creation;
- concurrent-session isolation.

### Portability

- generic MCP conformance harness is normative.
- optional smoke tests for Codex, Claude Code, and Antigravity run where their CLIs/SDKs are available.
- host smoke tests may fail/skipped due to missing host software without invalidating protocol tests; release requires at least two real-host smoke passes before declaring cross-host support stable.

## Observability

Persist observable records only:

- tool invocation metadata;
- execution requests/results;
- source and artifact metadata;
- evidence ledger;
- validation findings;
- report records.

Do not persist or request private model chain-of-thought.

## Post-Design Constitution Check

- No core host-specific dependency introduced: **PASS**.
- Evidence and reproducibility are enforced by canonical models: **PASS**.
- Validation remains independent: **PASS**.
- Sandbox boundary is server-side: **PASS**.
- Tool surface remains capability-oriented: **PASS with note** — 12 tools, justified by distinct list/retrieve operations.
- Simplicity maintained: **PASS** — no multi-agent layer or model training.
