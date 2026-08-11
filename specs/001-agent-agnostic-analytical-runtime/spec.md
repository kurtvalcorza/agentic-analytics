# Feature Specification: Agent-Agnostic Analytical Runtime

**Feature Branch**: `001-agent-agnostic-analytical-runtime`  
**Created**: 2026-08-12  
**Status**: Draft  
**Input**: Build an MCP-native analytical runtime that lets coding agents such as Codex, Claude Code, Antigravity, and future MCP-capable hosts inspect datasets, execute reproducible analysis, validate methods, retain evidence, create artifacts, and produce traceable reports without depending on host-specific behavior for correctness.

## User Scenarios & Testing

### User Story 1 — Analyze a local dataset from any supported coding agent (Priority: P1)

A user opens a repository containing one or more datasets and asks their coding agent a quantitative question. The agent discovers the analytical MCP tools, inspects the dataset, executes an analysis, and returns findings supported by registered evidence and artifacts.

**Why this priority**: This is the minimum useful product and proves that the runtime—not a particular host—is the durable capability.

**Independent Test**: Run the same local CSV analysis through two independent MCP clients or through one client and the protocol conformance harness. Both must produce valid tool calls, execution records, evidence, and a final evidence-complete state without host-specific server changes.

**Acceptance Scenarios**:

1. **Given** a workspace containing a CSV, **When** an agent calls source discovery and inspection tools, **Then** the runtime returns schema, shape, sample/profile metadata, and a stable source identifier without placing the complete dataset in the response.
2. **Given** an inspected dataset and an analytical question, **When** the agent executes SQL or Python in strict mode, **Then** execution occurs inside the managed session sandbox and produces an immutable execution record.
3. **Given** a successful execution, **When** the agent registers a derived fact, **Then** the evidence record identifies its source(s), execution record, result values, and claim classification.
4. **Given** a final set of material claims, **When** validation is requested, **Then** the runtime reports whether every material claim is evidence-backed and whether blocking analytical findings exist.

---

### User Story 2 — Produce reproducible analysis artifacts (Priority: P1)

A user asks the agent to create charts, tables, transformed datasets, scripts, or reports. The runtime tracks generated and modified files, associates them with the execution that created them, and exposes canonical artifact metadata independent of host UI.

**Why this priority**: Analytical work must result in durable outputs, not just chat responses.

**Independent Test**: Execute a script that generates a PNG and CSV. Confirm both appear in the artifact registry with media type, canonical path, source execution ID, and stable artifact IDs.

**Acceptance Scenarios**:

1. **Given** an execution that creates a file, **When** execution completes, **Then** the file is discoverable as a generated artifact without requiring the host to parse stdout.
2. **Given** an execution that modifies an existing file, **When** execution completes, **Then** the runtime records the modification while preserving source/derived lineage.
3. **Given** an artifact registry entry, **When** a host requests it, **Then** it receives host-neutral metadata sufficient to render or retrieve the artifact.

---

### User Story 3 — Validate analytical correctness before reporting (Priority: P1)

A user wants confidence that an AI-generated analysis did not use an invalid denominator, silently discard data, misuse a statistical test, or make unsupported causal claims. The runtime performs deterministic and evidence-based checks separately from the model's own reasoning.

**Why this priority**: Without independent validation, the product is only a code executor with bookkeeping.

**Independent Test**: Feed known-bad analysis scenarios (duplicate rows, wrong denominator, invalid t-test assumptions, observational causal wording, unlinked final claim) and verify the validator returns expected blocking or warning findings.

**Acceptance Scenarios**:

1. **Given** a dataset with duplicate primary observations, **When** validation is run, **Then** a duplication finding identifies the affected grain/key and severity.
2. **Given** a material claim with no evidence record, **When** final validation is run, **Then** the analysis cannot reach a validated state.
3. **Given** an observational comparison using causal language, **When** validation is run without an explicit causal design, **Then** the validator raises an unsupported-causality finding.
4. **Given** a statistical test with unmet documented assumptions, **When** validation is run, **Then** the runtime reports the failed assumption and an alternative or remediation path.

---

### User Story 4 — Work with data too large for model context (Priority: P2)

A user asks questions about large CSV, Parquet, JSON, or database-backed data. The agent can inspect metadata and execute pushdown queries without sending the full dataset to the model.

**Why this priority**: Data locality and context minimization are central to the architecture but can follow the initial local-file workflow.

**Independent Test**: Analyze a file larger than a configured context threshold using query tools while verifying that tool responses remain bounded and no operation serializes the complete dataset into model-facing output.

**Acceptance Scenarios**:

1. **Given** a large Parquet file, **When** the agent requests a profile, **Then** the runtime computes bounded summaries without returning all rows.
2. **Given** a SQL aggregation request, **When** the agent runs a query, **Then** the runtime returns a bounded result with row count/truncation metadata.
3. **Given** a result exceeding the response limit, **When** the query completes, **Then** the runtime stores the complete result as an artifact and returns a bounded preview plus artifact reference.

---

### User Story 5 — Use host-native computation without losing provenance (Priority: P2)

A user or agent prefers to use the host's own shell/notebook tooling for some calculations. In permissive mode, the agent can register externally executed analyses and evidence while clearly marking that execution as external to the managed sandbox.

**Why this priority**: Agent hosts already have strong execution capabilities. Interoperability should not require replacing them, while strict mode remains available when managed execution is needed.

**Independent Test**: Register an external execution with source fingerprints, code/query text, result metadata, and produced artifacts; then register evidence against it and validate the analysis successfully with an explicit provenance-mode marker.

**Acceptance Scenarios**:

1. **Given** permissive mode, **When** an agent registers host-native computation, **Then** the execution record is marked `external` and cannot be confused with sandboxed execution.
2. **Given** strict mode, **When** an agent attempts to register a material derived fact from unregistered external computation, **Then** final validation blocks the analysis.

---

### User Story 6 — Challenge an existing analysis (Priority: P2)

A user asks the agent to adversarially review prior findings. The runtime checks the evidence graph and runs targeted diagnostics for common failure modes such as denominator shifts, missingness, duplicates, Simpson's paradox, multiple comparisons, leakage, or unsupported causal language.

**Why this priority**: Independent challenge turns the evidence ledger into an active quality-control mechanism.

**Independent Test**: Build a fixture with a Simpson's paradox pattern and verify challenge analysis identifies segment reversal and marks the aggregate interpretation as needing review.

**Acceptance Scenarios**:

1. **Given** a completed analysis, **When** challenge analysis runs, **Then** findings reference the affected evidence or claims.
2. **Given** no detected issue, **When** challenge analysis completes, **Then** it reports what checks were actually performed rather than returning an unexplained green state.

---

### User Story 7 — Add host adapters without forking core logic (Priority: P3)

A maintainer wants to improve ergonomics for a new coding agent. They add a thin adapter containing installation/configuration guidance and optional skill instructions without modifying analytical correctness logic.

**Why this priority**: The product must remain portable as the coding-agent ecosystem changes.

**Independent Test**: Add a fixture adapter for a generic MCP host using only configuration and instruction files. All core conformance tests must pass unchanged.

**Acceptance Scenarios**:

1. **Given** a new MCP-capable host, **When** an adapter is added, **Then** no MCP tool implementation changes are required solely for that host.
2. **Given** an adapter is absent, **When** the host connects directly to the MCP server, **Then** core tools remain discoverable and usable.

## Edge Cases

- Empty datasets or files containing headers only.
- Duplicate column names, mixed types, invalid encodings, malformed CSV rows, nested JSON, and very wide tables.
- Multiple files with identical names in different directories.
- Files changed after inspection but before execution.
- Analysis session interrupted during code execution.
- Execution creates thousands of files or very large artifacts.
- User code attempts path traversal, host filesystem access, network access, fork bombs, or excessive memory use.
- Agent submits syntactically valid but semantically contradictory evidence metadata.
- An evidence item references deleted or stale artifacts.
- External execution registration omits source fingerprints.
- Host does not support optional MCP resources or prompts.
- Two agents connect concurrently to different sessions using the same source workspace.
- A statistical method is unknown to the validator.
- A final report includes claims that were not explicitly registered.
- Source data is modified after evidence is registered.

## Requirements

### Functional Requirements

- **FR-001**: The system MUST expose all core analytical capabilities through MCP tools with structured schemas.
- **FR-002**: The system MUST NOT require host-specific prompts, APIs, or tool-call formats for analytical correctness.
- **FR-003**: The system MUST create an isolated logical analysis session with a stable session ID.
- **FR-004**: The system MUST discover supported data sources within configured workspace boundaries.
- **FR-005**: The system MUST inspect supported data sources and return bounded metadata including schema, shape where available, null statistics, samples or summaries, and source fingerprint metadata.
- **FR-006**: The system MUST support CSV and Parquet sources in the initial release.
- **FR-007**: The system SHOULD support JSON and Excel when dependency and parser behavior are validated.
- **FR-008**: The system MUST provide read-only analytical querying through DuckDB or an equivalent embedded analytical engine.
- **FR-009**: The system MUST provide managed Python execution in strict mode.
- **FR-010**: Managed execution MUST run outside the MCP server process.
- **FR-011**: Managed execution MUST enforce workspace, timeout, environment, and session boundaries server-side.
- **FR-012**: The system MUST record every managed execution as an immutable execution record with status, code/query, timestamps, inputs, output summary, runtime metadata, and produced artifacts.
- **FR-013**: The system MUST support explicit registration of external executions in permissive mode.
- **FR-014**: External execution records MUST be distinguishable from managed execution records.
- **FR-015**: The system MUST detect generated and modified files associated with managed executions.
- **FR-016**: The system MUST expose generated files through a canonical artifact registry with stable artifact IDs, media types, paths/resource URIs, lineage, size, and hashes where feasible.
- **FR-017**: The system MUST provide an evidence ledger with immutable evidence IDs.
- **FR-018**: Every evidence item MUST have one of the four classifications: source fact, derived fact, interpretation, recommendation.
- **FR-019**: Derived facts MUST reference at least one source and one execution record.
- **FR-020**: Interpretations MUST reference at least one source fact or derived fact.
- **FR-021**: Recommendations MUST reference at least one evidence item and SHOULD identify the interpretation motivating the recommendation.
- **FR-022**: The system MUST support relationships among sources, executions, artifacts, evidence, validation findings, and reports.
- **FR-023**: The system MUST provide a validation operation that evaluates evidence coverage and analytical integrity.
- **FR-024**: Final validation MUST block a `validated` result when a material claim lacks evidence linkage.
- **FR-025**: Validation findings MUST include a stable code, severity, message, affected entity IDs, and remediation guidance.
- **FR-026**: The initial validator MUST check at minimum evidence completeness, duplicate-observation risk, missing-data risk, denominator consistency when denominator metadata is provided, and unsupported causal language.
- **FR-027**: The system SHOULD provide validated semantic operations for frequently used analyses when they can be implemented more safely than arbitrary code.
- **FR-028**: The system MUST allow arbitrary managed Python when semantic operations are insufficient.
- **FR-029**: The system MUST return bounded model-facing results and MUST spill oversized results to artifacts.
- **FR-030**: The system MUST expose whether any returned table/sample is truncated.
- **FR-031**: The system MUST support cancellation or timeout of managed execution without corrupting session metadata.
- **FR-032**: The system MUST prevent access outside configured workspace roots through path normalization and authorization checks.
- **FR-033**: The system MUST treat source files as read-only by default.
- **FR-034**: The system MUST maintain source fingerprints sufficient to detect common stale-source conditions.
- **FR-035**: The system MUST expose a challenge-analysis operation or workflow that evaluates registered evidence for common analytical failure modes.
- **FR-036**: Challenge analysis MUST report which checks were performed, skipped, or inconclusive.
- **FR-037**: The system MUST provide a machine-readable conformance interface or test harness that does not depend on any particular coding agent.
- **FR-038**: Host adapters MUST be optional and MUST contain no unique analytical correctness logic.
- **FR-039**: Public tool schemas and canonical record schemas MUST be versioned.
- **FR-040**: Breaking changes MUST be rejected without an explicit schema/protocol version transition.

### Non-Functional Requirements

- **NFR-001 Portability**: Core conformance tests MUST pass without host-specific server branches.
- **NFR-002 Security**: Managed code execution MUST be isolated from the MCP server process and host secrets.
- **NFR-003 Privacy**: Raw source data MUST remain inside the configured workspace/execution boundary unless explicitly configured otherwise.
- **NFR-004 Observability**: Tool calls, execution records, validation outcomes, and artifact/evidence registration MUST be auditable without storing model chain of thought.
- **NFR-005 Determinism**: Given unchanged source fingerprints and deterministic code/query, execution metadata MUST be sufficient to explain result provenance.
- **NFR-006 Performance**: Metadata inspection and bounded query responses SHOULD complete interactively on ordinary local datasets; long-running operations MUST expose timeout/cancellation semantics.
- **NFR-007 Extensibility**: Additional source types, semantic validators, or execution backends MUST be addable behind stable capability interfaces.
- **NFR-008 Maintainability**: The public MCP tool count SHOULD remain small; new tools require documented justification against extending an existing tool.

## Key Entities

- **AnalysisSession**: Logical unit of work containing workspace policy, runtime mode, and entity relationships.
- **DataSource**: Registered file, table, or database source with type, location, metadata, and fingerprint.
- **ExecutionRecord**: Immutable record of managed or external computation.
- **Artifact**: File or structured output generated or modified by an execution.
- **EvidenceItem**: Source fact, derived fact, interpretation, or recommendation with lineage.
- **ValidationFinding**: Structured result of an analytical or provenance check.
- **AnalysisReport**: Final or intermediate report linked to claims/evidence and validation state.
- **HostAdapter**: Optional configuration/instructions for a specific agent host; never part of analytical correctness.

## Success Criteria

### Measurable Outcomes

- **SC-001**: A reference CSV analysis scenario completes through at least two MCP-capable clients with zero server-side host-specific branches.
- **SC-002**: 100% of material derived facts in a validated reference report are traceable to both source IDs and execution IDs.
- **SC-003**: Known-bad fixtures for missing evidence, duplicate observations, stale sources, and unsupported causal language are detected with expected blocking/warning severity in automated tests.
- **SC-004**: A 1 GB Parquet fixture can be summarized and queried without serializing the complete dataset into a model-facing response.
- **SC-005**: Path traversal, cross-session access, timeout, and secret-exposure security fixtures fail safely.
- **SC-006**: An agent can connect with no project-specific skill file and still discover enough MCP tool semantics to complete the reference analysis workflow.
- **SC-007**: Adding a new host adapter requires no changes to core execution, evidence, validation, or artifact services.
- **SC-008**: Oversized query/execution outputs are automatically converted to artifacts while returning bounded previews and explicit truncation metadata.

## Assumptions

- Initial deployment is local or single-user developer workstation usage.
- MCP tool calling is the minimum common integration layer across target coding agents.
- Python and DuckDB cover the majority of initial analytical needs.
- Container-based isolation is the preferred initial managed-execution backend where Docker or a compatible runtime is available.
- The system can provide a degraded subprocess backend for development only, but it MUST be clearly marked non-isolated and MUST NOT satisfy strict-mode security conformance.

## Out of Scope for Initial Release

- Training or fine-tuning a specialized analytical model.
- Multi-agent orchestration as a core architecture.
- Autonomous external web research.
- Arbitrary production database write access.
- Distributed compute clusters.
- GPU workloads.
- Full causal-inference automation.
- Automatic publication of reports to third-party systems.
- Host-specific UI components beyond optional adapters.
