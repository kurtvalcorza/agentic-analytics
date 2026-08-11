# Quickstart: Agent-Agnostic Analytical Runtime

## Goal

Run the reference MCP server locally, connect any MCP-capable coding agent, and complete a reproducible CSV analysis with registered evidence and validation.

## 1. Install development environment

```bash
uv sync --all-extras
```

Build the strict-mode execution image:

```bash
docker build -t agentic-analytics-exec:dev -f docker/Dockerfile.exec .
```

## 2. Start the MCP server

```bash
uv run agentic-analytics serve --transport stdio
```

For clients that require streamable HTTP:

```bash
uv run agentic-analytics serve --transport streamable-http --host 127.0.0.1 --port 8765
```

## 3. Configure an MCP client

Point the host at either:

- the local stdio command `uv run agentic-analytics serve --transport stdio`; or
- the configured local streamable HTTP endpoint.

No host-specific adapter is required for correctness.

## 4. Reference workflow

Ask the coding agent:

> Inspect `tests/fixtures/survey.csv`. Report the share of respondents with positive adoption intent, break it down by organization type, create one chart, register the key findings as evidence, and validate the final claims. Do not use causal language.

A correct host-independent tool sequence will usually include:

1. `create_session`
2. `list_sources` / `inspect_source`
3. `query_data` or `execute_python`
4. `register_evidence`
5. `list_artifacts`
6. `validate_analysis`

The exact planning order may differ by host. Correctness is judged from resulting execution, evidence, artifact, and validation records—not from matching a prescribed hidden sequence.

## 5. Run protocol conformance

```bash
uv run python scripts/conformance.py
```

Expected result:

```text
PASS tool discovery
PASS source inspection
PASS bounded query
PASS managed execution
PASS artifact registration
PASS evidence lineage
PASS validation blocker fixture
PASS cross-session authorization
```

## 6. Strict versus permissive mode

Strict mode:

```json
{"workspace_root":".","mode":"strict"}
```

Material derived facts must be backed by managed execution.

Permissive mode:

```json
{"workspace_root":".","mode":"permissive"}
```

Host-native execution may be registered through `register_external_execution`, but it remains explicitly marked external.

## 7. Expected session outputs

```text
.agentic-analytics/
└── sessions/
    └── ses_.../
        ├── manifest.json
        ├── sources.jsonl
        ├── executions.jsonl
        ├── evidence.jsonl
        ├── findings.jsonl
        ├── artifacts.jsonl
        ├── generated/
        └── reports/
```

The exact internal persistence layout is not part of the public MCP contract and may change behind repository interfaces.
