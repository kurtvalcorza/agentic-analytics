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
