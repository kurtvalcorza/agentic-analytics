# Research: Agent-Agnostic Analytical Runtime

> **Prior art.** Several decisions below reference **DeepAnalyze: Agentic Large Language
> Models for Autonomous Data Science** (Zhang, Fan, Fan, Li & Du, 2025,
> [arXiv:2510.16872](https://arxiv.org/abs/2510.16872),
> [code](https://github.com/ruc-datalab/DeepAnalyze), MIT-licensed) as motivating prior
> work. Agentic Analytics is an independent, MCP-native runtime informed by that research,
> not a port of it. See the Acknowledgements in the project README for citation details.

## Decision 1 — MCP is the canonical integration boundary

**Decision**: Implement the durable analytical interface as an MCP server. Treat Codex, Claude Code, Antigravity, and other coding agents as clients rather than architectural dependencies.

**Rationale**: The core requirement is host portability. MCP provides a shared tool protocol and allows tool semantics to live at the server boundary rather than in vendor-specific orchestration code.

**Alternatives considered**:

- Separate native plugins per agent: rejected because correctness logic would drift.
- REST API only: useful internally but does not provide the common agent-facing discovery/tool contract.
- LangGraph/CrewAI orchestration layer: rejected as a core dependency because host agents already provide planning/tool selection.

## Decision 2 — Use a small capability-oriented tool surface

**Decision**: Start with tools for session management, source discovery/inspection, querying, managed execution, external execution registration, evidence, validation/challenge, and artifacts.

**Rationale**: DeepAnalyze demonstrates that a thin runtime can be effective when the agent/model owns planning. Coding agents already provide native reasoning and tool use; duplicating the planner creates unnecessary coupling.

**Alternatives considered**:

- One tool per statistical method: rejected as premature tool proliferation.
- One generic `execute(anything)` tool: rejected because it makes schemas weak, discovery poor, and validation/provenance inconsistent.

## Decision 3 — DuckDB for local analytical query

**Decision**: Use DuckDB as the primary embedded query engine for CSV and Parquet, with optional JSON/Excel support through validated extensions.

**Rationale**: DuckDB supports analytical SQL directly over local files, predicate/projection pushdown, Parquet, large-than-memory workflows, and bounded result retrieval without sending entire datasets to model context.

**Alternatives considered**:

- Pandas-only: rejected as the sole query layer due to memory scaling and weaker pushdown.
- Polars-only: strong candidate but less natural as a cross-source SQL surface.
- SQLite: less suitable for direct analytical querying over Parquet and large local files.

## Decision 4 — Python remains the general escape hatch

**Decision**: Provide managed Python execution for analyses not covered by semantic/query tools.

**Rationale**: Arbitrary analytical work requires flexible libraries and custom transformations. Python also mirrors the successful compute-observe loop used by DeepAnalyze.

**Alternatives considered**:

- R as the initial general runtime: deferred; can be added behind the same execution interface.
- WASM-only execution: attractive for isolation but insufficient library compatibility for v1.

## Decision 5 — Managed execution is session-scoped and out-of-process

**Decision**: Run strict-mode Python in an isolated session execution backend. Prefer a container backend with mounted workspace and resource/time policies.

**Rationale**: DeepAnalyze's evolution from in-process `exec()` to session-scoped Docker execution demonstrates the correct boundary. Host permission systems cannot be assumed consistent.

**Alternatives considered**:

- In-process `exec()`: rejected for security and failure isolation.
- Host shell execution only: rejected as the strict-mode execution boundary because it makes security host-dependent.
- Fresh container per code call: simpler isolation but loses useful session state and adds startup overhead; may be supported as a hardened mode later.

## Decision 6 — Evidence ledger is canonical provenance

**Decision**: Store explicit evidence records classified as source fact, derived fact, interpretation, or recommendation.

**Rationale**: Generated code and chat logs are not sufficient provenance. Evidence must be queryable and linked to sources, executions, artifacts, and downstream claims.

**Alternatives considered**:

- Rely on final report citations only: rejected because citations do not encode computation lineage.
- Store model reasoning traces: rejected because private chain of thought is neither necessary nor desirable for auditability.

## Decision 7 — Validation is a separate deterministic service

**Decision**: Implement structured validation checks independent of the model/host agent. The first release covers provenance completeness and a limited set of high-value analytical checks.

**Rationale**: Agent self-review is useful but insufficient as the sole correctness mechanism. Deterministic checks provide consistent behavior across hosts.

**Alternatives considered**:

- A second LLM critic only: rejected as non-deterministic and still model-dependent.
- Full automated statistical verification: deferred as too broad for v1.

## Decision 8 — Support strict and permissive provenance modes

**Decision**: `strict` mode requires managed execution for material derived facts. `permissive` mode permits host-native computation if registered as an external execution with explicit provenance metadata.

**Rationale**: Coding agents already have strong local execution tools. Forcing all computation through MCP would reduce usability; allowing untracked host computation would undermine provenance.

## Decision 9 — Canonical records use append-oriented storage first

**Decision**: Use a session manifest plus append-oriented JSONL/JSON records for v1, behind repository interfaces that can later move to SQLite.

**Rationale**: Human-readable files are easy to inspect and version during early development. The entity relationships remain explicit and migration to SQLite is straightforward when concurrency/query complexity justifies it.

**Alternatives considered**:

- SQLite immediately: reasonable, but adds migration/schema lifecycle before the model stabilizes.
- Pure directory naming conventions: rejected because relationships and validation state need structured records.

## Decision 10 — No essential behavior in host skills

**Decision**: Host adapters contain only installation, discovery hints, and optional workflow guidance. Tool descriptions and schemas must be sufficient for direct use.

**Rationale**: This is the main defense against accidentally building a Claude/Codex-specific plugin while claiming MCP portability.
