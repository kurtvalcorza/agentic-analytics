# Tasks: Agent-Agnostic Analytical Runtime

**Input**: Design documents from `/specs/001-agent-agnostic-analytical-runtime/`  
**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`

## Phase 1 — Setup

- [x] T001 Create Python package skeleton and `pyproject.toml` with Python 3.12+, MCP SDK, Pydantic, DuckDB, pytest, and dev tooling.
- [x] T002 Create `src/agentic_analytics/` package structure from `plan.md`.
- [x] T003 [P] Add formatting, linting, and type-check configuration.
- [x] T004 [P] Add initial `README.md` explaining MCP-native/agent-agnostic architecture and strict/permissive modes.
- [x] T005 Create test directories and fixture conventions under `tests/`.

## Phase 2 — Foundational Models and Persistence

**Checkpoint**: Canonical records can be created, serialized, validated, and stored without MCP or execution backends.

- [x] T006 Implement opaque typed ID generation in `src/agentic_analytics/ids.py`.
- [x] T007 [P] Implement `AnalysisSession` model in `models/session.py`.
- [x] T008 [P] Implement `DataSource` model in `models/source.py`.
- [x] T009 [P] Implement `ExecutionRecord` model in `models/execution.py`.
- [x] T010 [P] Implement `Artifact` model in `models/artifact.py`.
- [x] T011 [P] Implement `EvidenceItem` and evidence classification invariants in `models/evidence.py`.
- [x] T012 [P] Implement `ValidationFinding` and validation run status models in `models/validation.py`.
- [x] T013 Implement append-oriented repository base with atomic writes and per-session scoping.
- [x] T014 [P] Implement session/source/execution/artifact/evidence/finding repositories under `repositories/`.
- [x] T015 Add unit tests for schema invariants and cross-session reference rejection.
- [x] T016 Add evidence DAG cycle-detection unit tests.

## Phase 3 — User Story 1: Analyze Local Data from Any MCP Client [P1]

**Goal**: Host-independent source discovery, inspection, query, and managed analysis.

**Independent Test**: Generic protocol harness completes a CSV analysis without host-specific fields.

- [x] T017 Implement canonical path authorization and workspace root handling in `services/workspace.py`.
- [x] T018 Add security unit tests for `../`, absolute-path, and symlink escape attempts.
- [x] T019 Implement file discovery for CSV/Parquet in `services/workspace.py`.
- [x] T020 Implement source fingerprinting (SHA-256, size, mtime metadata) in `services/inspector.py`.
- [x] T021 Implement CSV/Parquet schema and bounded profile inspection through DuckDB.
- [x] T022 Add malformed/empty/wide-table inspection fixtures and tests.
- [x] T023 Implement read-only DuckDB query service with registered source aliases in `services/query.py`.
- [x] T024 Implement bounded query result serialization and truncation metadata.
- [x] T025 Add query tests proving oversized results do not serialize complete datasets to model-facing output.
- [x] T026 Implement MCP `create_session`, `list_sources`, `inspect_source`, and `query_data` tools.
- [x] T027 Add protocol schema tests for the four tools.

## Phase 4 — Managed Execution and Artifacts [P1]

**Goal**: Strict-mode Python execution outside the server process with artifact detection.

- [ ] T028 Define execution backend interface in `execution_backends/base.py`.
- [ ] T029 Create `docker/Dockerfile.exec` with pinned analytical runtime dependencies.
- [ ] T030 Implement session-scoped Docker backend in `execution_backends/docker.py`.
- [ ] T031 Enforce timeout, sanitized environment, disabled/restricted network, and workspace mounts in Docker backend.
- [ ] T032 Implement clearly marked non-conformant subprocess development backend in `execution_backends/subprocess_dev.py`.
- [ ] T033 Implement execution lifecycle service with source fingerprint capture in `services/execution.py`.
- [ ] T034 Implement workspace before/after snapshots for generated/modified file detection.
- [ ] T035 Implement artifact hashing, type detection, versioning, and registration in `services/artifact_registry.py`.
- [ ] T036 Implement MCP `execute_python`, `list_artifacts`, and `get_artifact` tools.
- [ ] T037 Add execution timeout and infinite-loop scenario tests.
- [ ] T038 Add secret-isolation and cross-session sandbox tests.
- [ ] T039 Add scenario test generating PNG + CSV and verifying canonical artifact lineage.

## Phase 5 — Evidence Ledger [P1]

**Goal**: Material claims are represented as explicit, queryable provenance records.

- [ ] T040 Implement evidence registration service and class-specific validation rules in `services/evidence_ledger.py`.
- [ ] T041 Enforce successful execution references for derived facts.
- [ ] T042 Enforce upstream evidence relationships for interpretations/recommendations.
- [ ] T043 Enforce same-session entity ownership and evidence DAG acyclicity.
- [ ] T044 Implement MCP `register_evidence` and `list_evidence` tools.
- [ ] T045 Add protocol tests for valid and invalid evidence registration.
- [ ] T046 Add scenario test where final material claim can be traced source → execution → evidence.

## Phase 6 — Validation [P1]

**Goal**: Independent deterministic checks can block invalid final analyses.

- [ ] T047 Implement validator interface and check registry.
- [ ] T048 [P] Implement evidence coverage validator.
- [ ] T049 [P] Implement stale source validator.
- [ ] T050 [P] Implement duplicate-observation validator using whole-row and configured-key modes.
- [ ] T051 [P] Implement missingness validator with configurable thresholds and explicit inconclusive state.
- [ ] T052 [P] Implement denominator consistency validator for registered denominator metadata.
- [ ] T053 [P] Implement unsupported causal-language validator using conservative lexical/pattern rules plus registered design metadata.
- [ ] T054 Implement validation aggregation and `validated`/`warnings`/`blocked` status logic.
- [ ] T055 Implement MCP `validate_analysis` tool.
- [ ] T056 Add known-bad fixture tests for all v1 validators.
- [ ] T057 Add scenario proving a material unlinked claim cannot receive `validated` status.

## Phase 7 — Large Data and Spill-to-Artifact [P2]

- [ ] T058 Add 1 GB-or-synthetic-equivalent Parquet performance fixture generation script.
- [ ] T059 Ensure DuckDB inspection/query avoids full model-facing serialization.
- [ ] T060 Implement automatic result spill to generated Parquet/CSV artifact when response limits are exceeded.
- [ ] T061 Add scenario test verifying bounded preview + artifact reference + explicit truncation.

## Phase 8 — Permissive Host-Native Execution [P2]

- [ ] T062 Implement `register_external_execution` service with explicit `external` provenance marker.
- [ ] T063 Verify referenced source fingerprints and artifact paths during external registration.
- [ ] T064 Implement MCP `register_external_execution` tool.
- [ ] T065 Enforce strict-mode blocking for material evidence backed only by external execution.
- [ ] T066 Add permissive-mode scenario using externally computed result and successful evidence validation.

## Phase 9 — Challenge Analysis [P2]

- [ ] T067 Implement challenge service returning checks-run/checks-skipped/checks-inconclusive metadata.
- [ ] T068 [P] Add denominator-shift diagnostic.
- [ ] T069 [P] Add missingness sensitivity diagnostic.
- [ ] T070 [P] Add segment-reversal/Simpson's-paradox diagnostic for supported tabular cases.
- [ ] T071 [P] Add multiple-comparison risk metadata/check when many parallel tests are registered.
- [ ] T072 Implement MCP `challenge_analysis` tool.
- [ ] T073 Add Simpson's paradox fixture and scenario test.

## Phase 10 — Protocol Conformance and Agent Portability [P1/P3]

**Goal**: Demonstrate that the product boundary is MCP, not a host plugin.

- [ ] T074 Implement `scripts/conformance.py` that drives the MCP server without Codex/Claude/Antigravity dependencies.
- [ ] T075 Add conformance checks for tool discovery, schemas, source inspection, bounded query, execution, artifact, evidence, validation, and cross-session authorization.
- [ ] T076 Add a test that starts with no host adapter files present and completes the reference workflow through the protocol harness.
- [ ] T077 Create `adapters/codex/` with optional setup and analyst workflow guidance only.
- [ ] T078 Create `adapters/claude-code/` with optional setup and analyst workflow guidance only.
- [ ] T079 Create `adapters/antigravity/` with optional setup and analyst workflow guidance only.
- [ ] T080 Add static test/lint ensuring adapter directories do not define canonical schemas or validation code.
- [ ] T081 Document real-host smoke-test procedure; keep it non-normative relative to protocol conformance.

## Phase 11 — Hardening and Release Readiness

- [ ] T082 Version all public tool and canonical record schemas as `0.1.0`.
- [ ] T083 Add backward-compatibility test fixture for protocol version negotiation/rejection.
- [ ] T084 Add structured logging for tool calls, execution lifecycle, validation runs, and repository write failures without logging raw sensitive datasets by default.
- [ ] T085 Add cancellation cleanup tests for interrupted Docker execution.
- [ ] T086 Add excessive-file-generation guard and scenario test.
- [ ] T087 Run full specification checklist in `checklists/requirements.md` and resolve all unchecked items.
- [ ] T088 Run `/speckit.analyze` equivalent cross-artifact consistency check and reconcile any spec/plan/task gaps.
- [ ] T089 Run complete unit/protocol/scenario/security suite.
- [ ] T090 Complete smoke tests on at least two real MCP-capable hosts before declaring cross-host support stable.
- [ ] T091 Tag `0.1.0` only after constitution checks, conformance suite, and release checklist pass.

## Dependencies and Execution Order

```text
Setup
  ↓
Models + repositories
  ↓
Workspace/source/query ─────────────┐
  ↓                                │
Managed execution + artifacts      │
  ↓                                │
Evidence ledger                    │
  ↓                                │
Validation                         │
  ├──→ Large-data spill            │
  ├──→ External execution          │
  └──→ Challenge analysis          │
                    \              /
                     \            /
                  Protocol conformance
                           ↓
                    Host adapters
                           ↓
                       Hardening
```

## Parallel Opportunities

- Model classes T007–T012.
- Initial validators T048–T053.
- Challenge diagnostics T068–T071.
- Host adapter documentation T077–T079 after MCP contracts stabilize.

## MVP Boundary

A viable `0.1.0-alpha` exists after T057 if the following pass:

- generic MCP conformance for session/source/query/execution/evidence/validation;
- strict sandbox security tests;
- artifact generation scenario;
- material evidence completeness blocker.

Large-data spill, permissive external execution, challenge analysis, and polished host adapters may follow in `0.1.x` if schedule requires, but cross-host architecture MUST NOT be compromised to accelerate MVP.
