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

## Quickstart

Requires Python 3.12+ and, for sandboxed execution, Docker.

```bash
# Install
python -m pip install -e '.[dev]'

# Build the managed-execution sandbox image (used by strict-mode execute_python)
docker build -t agentic-analytics-exec:dev -f docker/Dockerfile.exec .

# Run the MCP server over stdio. By default the current directory is the authorized
# workspace, so start it from the folder that holds your CSV/Parquet data:
cd /path/to/your/data
agentic-analytics
```

Register it with your MCP client (e.g. Claude Code or Codex). The `env` allowlist
authorizes exactly which local directories the server may read:

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

For a quick local trial without Docker, use the development backend — permissive
sessions only, **not isolated, never for untrusted code**:

```bash
export AGENTIC_ANALYTICS_EXECUTION_BACKEND=subprocess_dev
```

Then ask your agent to inspect and analyze the data — see [How it works](#how-it-works) for the tool flow.

## Implemented capabilities

The runtime exposes a versioned MCP tool surface over a host-neutral core:

- **Sessions** — `create_session` authorizes a workspace root (optionally with analysis/causal design metadata) and reports host capabilities derived from the configured backend.
- **Data plane** — `list_sources` discovers CSV/Parquet sources (bounded), `inspect_source` registers and profiles them in a single scan, and `query_data` runs bounded, read-only DuckDB SQL over registered sources with enforced memory/time limits, source-fingerprint integrity checks, and audit records for successful and failed runs.
- **Managed execution** — `execute_python` runs code in an isolated, ephemeral Docker sandbox (network-disabled, read-only root filesystem, dropped capabilities, resource-capped) and registers generated files as immutable artifacts stored outside the writable workspace; `list_artifacts` / `get_artifact` expose metadata, and artifact bytes are retrievable through a registered MCP resource. A non-conformant subprocess backend is available for development (permissive sessions only).
- **Evidence ledger** — `register_evidence` / `list_evidence` record a source → execution → artifact → evidence provenance chain with validated links, lineage enforcement, and cycle detection.
- **Validation** — `validate_analysis` runs deterministic checks (evidence coverage, stale sources, duplicates, missingness, denominator consistency, unsupported causal language) and returns bounded, provenance-linked findings; incomplete coverage never reports a clean pass.
- **Large results** — query results that exceed the row or response-size budget spill to a Parquet artifact, with a bounded in-context preview derived from the same evaluation.

Persistence is append-only and session-scoped, with atomic record publication, canonical typed IDs, and UTC-normalized timestamps. Quality gates (ruff, mypy, pytest, and the sandbox image build) run in CI.

See `specs/001-agent-agnostic-analytical-runtime/` for the specification, plan, contracts, data model, and implementation task graph. The runtime is delivered as a stack of layered pull requests: runtime foundation → data plane → managed execution → evidence ledger → validation → large-data spill.

## How it works

You bring your own data — local CSV/Parquet files — and point an MCP-capable coding agent (Claude Code, Codex, …) at the runtime. The agent does the reasoning and decides which tools to call; the runtime provides a bounded, sandboxed environment and records provenance so every result is reproducible and independently checkable. Full datasets never flood the model's context.

A typical end-to-end flow over a `survey.csv` in your workspace (subsequent calls carry the `session_id` returned by the first):

1. **Authorize the data.** `create_session(workspace_root=".")` opens a session scoped to your workspace and returns `ses_…`. Only directories under the server's configured allowlist are accepted.
2. **Discover and inspect.** `list_sources()` finds `survey.csv`; `inspect_source(source="survey.csv")` registers it and returns schema, row count, null counts, duplicate-row count, and a small sample — plus a content fingerprint. The full file is never sent to the model.
3. **Query without loading everything.** `query_data(sql="SELECT region, avg(score) AS mean FROM source('src_…') GROUP BY region")` runs bounded, read-only DuckDB SQL. Small results return inline; oversized results spill to a Parquet artifact with a bounded preview.
4. **Run richer analysis in a sandbox.** `execute_python(code=...)` runs pandas / scikit-learn / matplotlib in an isolated, network-disabled Docker container over the workspace. Generated files (a chart, a cleaned dataset) are captured as immutable artifacts.
5. **Record evidence.** `register_evidence(classification="derived_fact", claim="Mean score in NCR is 4.2", source_ids=[...], execution_ids=[...])` links the claim to the exact source and execution that produced it.
6. **Validate the analysis.** `validate_analysis(claim_texts=[...])` runs deterministic checks — stale/changed sources, duplicate observations, high missingness, denominator sanity, unsupported causal language, and whether each material claim is backed by registered evidence — and returns findings to act on.
7. **Retrieve outputs.** `list_artifacts()` / `get_artifact(artifact_id="art_…")` return the generated tables, charts, and datasets with metadata and content hashes.

The agent chooses *which* steps to take and in what order; the runtime guarantees they run in a bounded sandbox, over authorized data, with a verifiable source → execution → artifact → evidence trail.

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

## Relationship to DeepAnalyze

[DeepAnalyze](https://arxiv.org/abs/2510.16872) puts the intelligence and the analytical environment inside a single trained model (DeepAnalyze-8B) that autonomously drives the data-science pipeline. Agentic Analytics deliberately separates those concerns: it is the portable, MCP-native analytical **environment and evidence contract** — data inspection, sandboxed query and execution, source → execution → artifact → evidence provenance, and deterministic validation — that *any* MCP-capable coding agent can drive.

The two are complementary rather than equivalent:

- **Planning and autonomy** live in the calling agent, not in this runtime. Agentic Analytics has no agent loop or planner of its own; it exposes tools and enforces contracts.
- **Rigor lives in the runtime.** Provenance and validation are deterministic and independent of model self-review — a trust layer DeepAnalyze's autonomous framing does not emphasize.
- **Scope.** The runtime covers inspection, query, managed execution (including modeling and visualization through code), artifacts, evidence, and validation. It does not attempt autonomous end-to-end pipelines or turnkey report generation; those remain the agent's responsibility.

In short: DeepAnalyze is an autonomous data scientist; Agentic Analytics is the shared, verifiable environment that such an agent — or any coding agent — can work in.

## Acknowledgements

This project is informed by research on agentic systems for autonomous data
science, in particular **DeepAnalyze: Agentic Large Language Models for
Autonomous Data Science** by Shaolei Zhang, Ju Fan, Meihao Fan, Guoliang Li,
and Xiaoyong Du (Renmin University of China and Tsinghua University).

- Paper: [arXiv:2510.16872](https://arxiv.org/abs/2510.16872)
- Code: <https://github.com/ruc-datalab/DeepAnalyze> (MIT-licensed)
- Project page: <https://ruc-deepanalyze.github.io/>

If you build on that work, please cite it:

```bibtex
@misc{deepanalyze,
      title={DeepAnalyze: Agentic Large Language Models for Autonomous Data Science},
      author={Shaolei Zhang and Ju Fan and Meihao Fan and Guoliang Li and Xiaoyong Du},
      year={2025},
      eprint={2510.16872},
      archivePrefix={arXiv},
      primaryClass={cs.AI},
      url={https://arxiv.org/abs/2510.16872},
}
```

## Development and AI assistance

This project is developed with AI coding assistants under human direction:

- Specification and initial implementation were produced with **OpenAI Codex**, which also performed automated code review.
- Review-finding remediation, security/robustness hardening, and documentation were done with **Anthropic Claude** (via Claude Code).

The maintainer directs the work and is responsible for all changes. AI contributions are attributed per-commit via `Co-Authored-By` trailers, and every change lands through pull requests gated by CI (ruff, mypy, tests, and the sandbox image build).
