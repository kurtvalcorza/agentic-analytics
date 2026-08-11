# Specification Quality Checklist: Agent-Agnostic Analytical Runtime

**Purpose**: Validate that the feature specification is complete, testable, host-neutral, and consistent with the constitution before implementation.

## Agent Agnosticism

- [ ] Every core requirement can be satisfied using MCP without a vendor-specific API.
- [ ] No correctness requirement depends on `SKILL.md`, `CLAUDE.md`, `AGENTS.md`, Antigravity instructions, or another host-only prompt file.
- [ ] At least one acceptance scenario explicitly tests direct MCP use without an adapter.
- [ ] Host adapters are clearly non-normative.

## Evidence and Reproducibility

- [ ] All four evidence classes are defined.
- [ ] Derived facts require both source and execution lineage.
- [ ] Final validation blocks material claims without evidence.
- [ ] Managed and external execution are distinguishable.
- [ ] Source staleness/fingerprinting behavior is specified.
- [ ] Generated artifacts have canonical IDs and lineage.

## Analytical Validation

- [ ] Validation is independent from model self-review.
- [ ] Blocking versus warning behavior is defined.
- [ ] At least duplicate, missingness, denominator, and causal-language cases are covered.
- [ ] Unknown/inconclusive validation states cannot become silent success.
- [ ] Challenge analysis reports performed/skipped checks.

## Security

- [ ] Managed execution occurs outside the MCP server process.
- [ ] Workspace boundaries are server-enforced.
- [ ] Path traversal and symlink escape are covered.
- [ ] Timeout and session isolation are covered.
- [ ] Host permissions are explicitly defense in depth.
- [ ] Raw host secrets are not inherited into the sandbox by default.

## Context and Data Locality

- [ ] Full datasets are not required to enter model context.
- [ ] Tool results have bounded previews.
- [ ] Truncation is explicit.
- [ ] Oversized outputs can be promoted to artifacts.

## Scope Control

- [ ] No specialized model training is required for v1.
- [ ] No multi-agent orchestration is required for v1.
- [ ] Tool count remains small and capability-oriented.
- [ ] Domain-specific statistical tools are deferred until demonstrated need.

## Testability

- [ ] Every P1 user story has an independent test.
- [ ] Portability has a protocol-level conformance test.
- [ ] Known-bad analytical fixtures are specified.
- [ ] Security fixtures are specified.
- [ ] Success criteria are measurable and not host-specific.
