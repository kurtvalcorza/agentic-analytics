# Agentic Analytics

Agentic Analytics is an **agent-agnostic, MCP-native analytical runtime** for coding agents such as Codex, Claude Code, Antigravity, and other MCP-capable hosts.

The product boundary is the MCP protocol, not a host-specific plugin. Core correctness, provenance, validation, and execution rules live in the runtime and canonical contracts. Host adapters may improve ergonomics, but they must not contain unique analytical logic.

## Design goals

- Inspect and query local analytical data without serializing full datasets into model context.
- Execute reproducible analysis in a managed runtime.
- Record source → execution → artifact → evidence provenance.
- Validate material analytical claims independently of model self-review.
- Support both strict managed execution and explicitly marked permissive host-native execution.
- Remain portable across MCP-capable agents.

## Current implementation status

The first implementation slice establishes the Python package, MCP v2 server skeleton, canonical typed IDs, provenance models, append-only session-scoped record persistence, model invariants, evidence DAG cycle detection, and CI quality gates.

See `specs/001-agent-agnostic-analytical-runtime/` for the specification, plan, contracts, data model, and implementation task graph.

## Development

Requires Python 3.12+.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
python -m pip install -e '.[dev]'
ruff check .
mypy src
pytest
```

Run the MCP server over stdio:

```bash
agentic-analytics
```

The implementation targets the current stable MCP Python SDK v2 line.
