# Agentic Analytics Constitution

## Preamble

This project builds an agent-agnostic analytical runtime for coding agents. It enables agents such as Codex, Claude Code, Antigravity, and future MCP-capable hosts to inspect data, execute reproducible analyses, validate methods, retain evidence, generate artifacts, and produce traceable reports without depending on host-specific behavior for correctness.

The runtime is not an autonomous decision-maker. It is an analytical environment and evidence system. Host agents may plan and reason differently, but the same analytical contracts, provenance rules, and validation guarantees must apply across hosts.

## Principle 1 — Agent Agnosticism Is a Hard Requirement

No core analytical capability may require behavior unique to a specific coding agent, IDE, CLI, model provider, prompt format, or proprietary tool-calling interface.

Core functionality MUST be accessible through standard MCP tools and host-neutral data contracts. Host-specific adapters MAY improve ergonomics, discoverability, or presentation, but MUST NOT be required for correctness, provenance, validation, artifact production, or reproducibility.

A feature is not considered complete until it can be exercised through at least two independent MCP-capable hosts or through one host plus the protocol-level conformance test harness.

## Principle 2 — MCP Is the Product Boundary

The durable product surface is the MCP server and its versioned contracts.

The system MUST expose a small, coherent set of capability-oriented tools. Tool semantics MUST be discoverable from schemas and descriptions without requiring hidden prompt conventions.

The initial compatibility baseline MUST rely only on broadly supported MCP primitives: tools, structured JSON inputs/outputs, text content, and file/resource identifiers. Optional MCP capabilities such as prompts, resources, elicitation, sampling, progress notifications, or host-specific UI integrations MAY be added only as non-critical enhancements.

## Principle 3 — Evidence Before Conclusion

Material quantitative claims MUST be traceable to evidence.

Every reportable claim MUST be classified as exactly one of:

1. **Source fact** — directly present in an authoritative input source.
2. **Derived fact** — computed from one or more source facts through a reproducible method.
3. **Interpretation** — an analytical inference grounded in source or derived facts.
4. **Recommendation** — a proposed action grounded in explicitly cited evidence and interpretation.

Derived facts MUST identify the source inputs and the reproducible execution record that produced them. Interpretations and recommendations MUST identify the evidence items on which they depend.

The system MUST prevent a successful "validated" state when material claims lack evidence linkage.

## Principle 4 — Reproducibility Is a System Property

Any analysis supporting a material claim MUST have a reproducible execution record.

The system MUST record enough information to rerun or audit the analysis, including:

- input source identifiers and fingerprints where practical;
- executed query, script, or validated analytical operation;
- runtime/tool version metadata;
- execution status and relevant outputs;
- generated artifacts;
- evidence records derived from the execution.

Host-native computation MAY be used in permissive mode, but evidence derived from host-native computation MUST be registered with an explicit external-execution record. Strict mode MUST route analytical computation through the managed runtime.

## Principle 5 — Validation Is Separate From Generation

The model or host agent that produces an analysis MUST NOT be the only mechanism deciding whether the analysis is valid.

The runtime MUST implement deterministic or independently checkable validation for high-risk analytical properties, including where applicable:

- schema and type consistency;
- data grain and denominator consistency;
- missing-data conditions;
- duplicate observations;
- invalid or contradictory filters;
- statistical test assumptions;
- sample-size requirements;
- model leakage and target contamination;
- multiple-comparison risk;
- unsupported causal language;
- evidence coverage of final claims.

Validation results MUST be returned as structured findings with severity, evidence, and remediation guidance.

## Principle 6 — Progressive Analytical Freedom

The runtime SHOULD prefer validated semantic tools for common analytical operations and fall back to arbitrary code only when necessary.

The preferred hierarchy is:

1. metadata and dataset inspection;
2. validated query and analytical operations;
3. managed SQL/Python/R execution;
4. host-native computation in permissive mode.

The runtime MUST NOT force a semantic operation when arbitrary code is necessary to answer the question correctly.

## Principle 7 — Workspace State Is Explicit

Each analysis session MUST have an explicit workspace with bounded lifecycle and ownership.

A session workspace MUST distinguish:

- source inputs;
- temporary/intermediate files;
- execution records;
- generated artifacts;
- registered evidence;
- reports or exports.

Generated and modified files MUST be discoverable. Artifact registration MUST not depend on a host parsing console output.

## Principle 8 — Sandboxing Is Enforced Server-Side

The MCP server MUST NOT rely on host permission UX as the primary security boundary.

Managed execution MUST enforce server-side controls for:

- workspace filesystem boundaries;
- process timeouts;
- resource quotas where supported;
- secret isolation;
- environment sanitization;
- network policy;
- session separation;
- cleanup of transient execution environments.

Host permissions are defense in depth, not the security model.

## Principle 9 — Data Minimization and Locality

The runtime MUST avoid placing large raw datasets in model context when analysis can instead be performed through queries or code.

Dataset inspection SHOULD expose schemas, profiles, samples, and summaries appropriate to the task while allowing computation to operate on the underlying files or databases directly.

No data may leave the configured execution boundary unless the user explicitly configures a remote provider or external data source.

## Principle 10 — Transparent Failure

The runtime MUST fail explicitly when it cannot complete an operation safely or reproducibly.

It MUST NOT silently:

- drop rows or columns;
- coerce incompatible types;
- replace failed validation with a green status;
- ignore execution errors;
- fabricate evidence records;
- claim reproducibility when source state cannot be identified;
- claim causal effects from observational analyses without an explicit causal design.

Partial results MUST retain their failure or warning state.

## Principle 11 — Small, Stable Tool Surface

New MCP tools MUST represent durable capabilities, not one-off prompt conveniences.

Before adding a tool, maintainers MUST determine whether the capability can be expressed through an existing tool with a structured mode or operation. Tool proliferation that duplicates semantics is prohibited.

The public tool surface MUST be versioned. Breaking schema or semantic changes require a protocol version change or backward-compatible migration period.

## Principle 12 — Artifact and Evidence Portability

Artifacts and evidence MUST use host-neutral representations.

A host adapter MUST NOT be required to understand an artifact or evidence record. The canonical representation SHOULD use:

- stable IDs;
- media types;
- relative paths or resource URIs;
- structured metadata;
- explicit lineage relationships.

Host-specific renderers MAY transform canonical artifacts for display but MUST NOT mutate their analytical meaning.

## Principle 13 — Analytical State Is Observable, Not Hidden

The system MUST expose enough state to audit what happened without requiring access to a model's private chain of thought.

Observable analytical state includes:

- requested question or objective;
- selected sources;
- execution records;
- validation findings;
- evidence ledger;
- generated artifacts;
- final report metadata.

The runtime MUST NOT require or store private chain-of-thought traces as a correctness mechanism.

## Principle 14 — Quality Gates Before Release

A feature that affects analytical correctness MUST include tests at three levels:

1. **Unit tests** for deterministic logic and schemas.
2. **Protocol tests** for MCP tool behavior and structured outputs.
3. **Scenario tests** using realistic datasets and analytical questions.

Security-sensitive execution features additionally require sandbox escape, path traversal, timeout, and session-isolation tests.

Agent portability features require conformance tests showing that no host-specific prompt or API is needed for the core workflow.

## Principle 15 — Simplicity Before Specialization

The initial implementation MUST prioritize a small, dependable analytical runtime over a large catalog of domain-specific agents.

New domain skills, statistical procedures, and workflow adapters SHOULD be introduced only after repeated real-world failures or needs demonstrate that the generic toolset is insufficient.

Training or fine-tuning a specialized model is out of scope until the runtime, evidence protocol, validation layer, and agent-agnostic interface have demonstrated value independently.

## Governance

This constitution supersedes feature-level preferences when they conflict.

Any exception MUST be documented in the relevant implementation plan with:

- the violated principle;
- why compliance is impractical;
- the scope and duration of the exception;
- the mitigation;
- the condition for removing the exception.

Changes to Principles 1–5 or 8 require explicit architectural review because they define portability, evidence, reproducibility, validation, and security guarantees.

Version: 1.0.0
Ratified: 2026-08-12
Last amended: 2026-08-12
