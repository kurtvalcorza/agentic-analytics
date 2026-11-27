# Quickstart: Agent-Agnostic Analytical Runtime

## Goal

Run the reference MCP server locally, connect any MCP-capable coding agent, and complete a reproducible CSV analysis with registered evidence and validation.

## 1. Install development environment

```bash
uv sync --frozen --extra dev
```

Build the strict-mode execution image:

```bash
docker build -t agentic-analytics-exec:dev -f docker/Dockerfile.exec .
```

## 2. Start the MCP server

The server speaks MCP over stdio; the command takes no arguments. By default the current directory is the authorized workspace, so start it from the folder that holds your CSV/Parquet data:

```bash
cd /path/to/your/data
uv run --project /path/to/agentic-analytics agentic-analytics
```

Streamable HTTP transport is not implemented yet.

## 3. Configure an MCP client

Point the host at the stdio command `agentic-analytics`, and authorize the data directories through the environment (see README, Quickstart):

```json
{
  "mcpServers": {
    "agentic-analytics": {
      "command": "agentic-analytics",
      "env": {
        "AGENTIC_ANALYTICS_ALLOWED_WORKSPACE_ROOTS": "[\"/path/to/your/data\"]"
      }
    }
  }
}
```

No host-specific adapter is required for correctness.

## 4. Reference workflow

Ask the coding agent, with a survey CSV in the authorized workspace:

> Inspect `survey.csv`. Report the share of respondents with positive adoption intent, break it down by organization type, create one chart, register the key findings as evidence, and validate the final claims. Do not use causal language.

A correct host-independent tool sequence will usually include:

1. `create_session`
2. `list_sources` / `inspect_source`
3. `query_data` or `execute_python`
4. `register_evidence`
5. `list_artifacts`
6. `validate_analysis`

The exact planning order may differ by host. Correctness is judged from resulting execution, evidence, artifact, and validation records—not from matching a prescribed hidden sequence.

## 5. Protocol conformance

A standalone conformance script is planned but not implemented. Until then, `tests/protocol` covers host-neutral tool schemas, source discovery and bounded queries, result completeness, evidence lineage, validation blockers and session/workspace isolation; managed execution and artifact registration are covered in `tests/scenarios` and `tests/security`:

```bash
uv run pytest tests/protocol tests/scenarios
```

The data-plane tests need Linux with Landlock, and the managed-execution tests need Docker and an `agentic-analytics-exec:test` image (see README, Development).

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

Server state lives under `.agentic-analytics/state/` (setting `AGENTIC_ANALYTICS_STATE_DIR`): one directory per session (`ses_.../`), holding a subdirectory of JSON records per record type, plus a `locks/` directory. Managed outputs are written to the workspace's `outputs/`.

The exact internal persistence layout is not part of the public MCP contract and may change behind repository interfaces.
