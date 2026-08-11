# Agentic Analytics — Spec Kit Package

This directory is a complete Spec Kit design package for an **agent-agnostic, MCP-native analytical runtime** inspired by the architectural lessons of DeepAnalyze while targeting coding agents such as Codex, Claude Code, Antigravity, and future MCP-capable hosts.

## Contents

```text
.specify/memory/constitution.md
specs/001-agent-agnostic-analytical-runtime/
├── spec.md
├── research.md
├── plan.md
├── data-model.md
├── quickstart.md
├── tasks.md
├── contracts/
│   ├── mcp-tools.md
│   └── schemas.json
└── checklists/
    └── requirements.md
```

## Design thesis

The MCP server is the product boundary. Coding-agent integrations are thin optional adapters. Core correctness, execution, evidence provenance, validation, artifacts, and security remain host-neutral.

The runtime deliberately does **not** recreate DeepAnalyze's model-specific XML control protocol or add a separate multi-agent planner. Modern coding agents already plan and call tools; this project supplies the analytical environment and evidence contract they can share.
