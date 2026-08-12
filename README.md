# Agentic Analytics

An **agent-agnostic, MCP-native analytical runtime** for reproducible local data analysis.
The MCP server—not a host plugin—is the product boundary. It supplies bounded CSV/Parquet
inspection and DuckDB queries, managed Python, artifact tracking, immutable evidence, and
deterministic validation to any MCP-capable client.

## Install and run

```bash
python -m pip install -e '.[dev]'
agentic-analytics serve --transport stdio
python scripts/conformance.py
```

Use **strict mode** for material analysis backed by the Docker execution sandbox. Strict mode
never accepts external computation as material lineage. Use **permissive mode** during local
development or when host-native computation is explicitly registered and marked external.

Session records live under `.agentic-analytics/sessions/` by default. Source paths remain within
their authorized workspace, result previews are bounded, and raw datasets are not copied into
model context.

## Design package

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

The runtime deliberately does **not** add a vendor-specific control protocol or multi-agent
planner. Modern coding agents already plan and call tools; this project supplies the analytical
environment and evidence contract they can share. See the
[quickstart](specs/001-agent-agnostic-analytical-runtime/quickstart.md) for a reference workflow.
