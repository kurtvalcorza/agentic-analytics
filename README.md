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
- **Validation** — `validate_analysis` runs deterministic checks (evidence coverage, stale sources, duplicates, missingness, denominator consistency, unsupported causal language) and returns bounded, provenance-linked findings. Only a run covering the full default baseline with no findings or inconclusive checks reports `validated`; a clean subset reports `partial`, and every response carries `coverage_complete` and an `assurance` statement.
- **Large results** — query results that exceed the row or response-size budget spill to a Parquet artifact, with a bounded in-context preview derived from the same evaluation.

Persistence is append-only and session-scoped, with atomic record publication, canonical typed IDs, and UTC-normalized timestamps. Quality gates (ruff, mypy, pytest, and the sandbox image build) run in CI.

See `specs/001-agent-agnostic-analytical-runtime/` for the specification, plan, contracts, data model, and implementation task graph. The runtime is delivered as a stack of layered pull requests: runtime foundation → data plane → managed execution → evidence ledger → validation → large-data spill.

### Not yet implemented

Two capabilities appear in the contracts (`specs/.../contracts/mcp-tools.md`) but are deliberately deferred to a later `0.1.x` and are **not** available yet:

- **External execution registration** (`register_external_execution`) — recording analyses run outside the managed sandbox, in permissive mode.
- **Challenge analysis** (`challenge_analysis`) — independent, adversarial re-checking of a completed analysis. Deterministic validation is available today via `validate_analysis`.

## How it works

You bring your own data — local CSV/Parquet files — and point an MCP-capable coding agent (Claude Code, Codex, …) at the runtime. The agent does the reasoning and decides which tools to call; the runtime provides a bounded, sandboxed environment and records provenance so results are auditable (see [Guarantees and limitations](#guarantees-and-limitations) for exactly what is and is not verified). Full datasets never flood the model's context.

A typical end-to-end flow over a `survey.csv` in your workspace (subsequent calls carry the `session_id` returned by the first):

1. **Authorize the data.** `create_session(workspace_root=".")` opens a session scoped to your workspace and returns `ses_…`. Only directories under the server's configured allowlist are accepted.
2. **Discover and inspect.** `list_sources()` finds `survey.csv`; `inspect_source(source="survey.csv")` registers it and returns schema, row count, null counts, duplicate-row count, and a small sample — plus a content fingerprint. The full file is never sent to the model.
3. **Query without loading everything.** `query_data(sql="SELECT region, avg(score) AS mean FROM source('src_…') GROUP BY region")` runs bounded, read-only DuckDB SQL. Small results return inline; oversized results spill to a Parquet artifact with a bounded preview. `truncated` describes the preview; check `result_complete` before treating the artifact as the full result (pass `require_complete=true` to fail instead of producing a capped artifact).
4. **Run richer analysis in a sandbox.** `execute_python(code=...)` runs pandas / scikit-learn / matplotlib in an isolated, network-disabled Docker container that reads the workspace and writes only to `outputs/` (a quota-limited per-run tmpfs). Generated files (a chart, a cleaned dataset) are captured as immutable artifacts.
5. **Record evidence.** `register_evidence(classification="derived_fact", claim="Mean score in NCR is 4.2", source_ids=[...], execution_ids=[...])` links the claim to the exact source and execution that produced it.
6. **Validate the analysis.** `validate_analysis(claim_texts=[...])` runs deterministic checks — stale/changed sources, duplicate observations, high missingness, denominator sanity, unsupported causal language, and whether each material claim is backed by registered evidence — and returns findings to act on.
7. **Retrieve outputs.** `list_artifacts()` / `get_artifact(artifact_id="art_…")` return the generated tables, charts, and datasets with metadata and content hashes.

The agent chooses *which* steps to take and in what order; the runtime runs them in a bounded sandbox, over authorized data, and records a source → execution → artifact → evidence trail whose assurance level is stated explicitly.

## Guarantees and limitations

The runtime separates **provenance bookkeeping** from **analytical truth**. What it does and does not establish:

| Area | Guaranteed | Not guaranteed |
| --- | --- | --- |
| Managed Python (Docker) | No network, read-only root FS, all capabilities dropped, `no-new-privileges`, explicit non-root UID (a root server is refused unless `AGENTIC_ANALYTICS_DOCKER_USER` names a non-root identity), memory/CPU/PID/time limits, server state masked, registered sources read-only. **The workspace is mounted read-only; the only writable output location is `outputs/`**, a fresh per-run tmpfs of `DOCKER_OUTPUT_QUOTA_BYTES` (default 256 MiB; settings take the `AGENTIC_ANALYTICS_` env prefix). The quota plus 192 MiB (128 MiB for `/tmp`, 64 MiB for `/dev/shm`) must stay below 80% of `DOCKER_MEMORY`, because tmpfs pages are charged to container memory; a `DOCKER_MEMORY` value that cannot be parsed is refused. The kernel enforces the quota, so a writer gets `ENOSPC` instead of filling the host disk. A supervisor (the container's PID 1, non-dumpable) runs the script with its own timeout. While the script runs, it watches `outputs/` with inotify and holds a descriptor on every file created there, so deleting a file does not free quota during the run. When the script ends, the supervisor sends `SIGKILL` to every other process in the container (`kill(-1)` from PID 1) and repeats until `/proc` lists no process but itself, before judging the run; if that takes more than 10 s the run fails. If free space ever fell to 64 KiB or 1% of the quota (whichever is larger), or inodes ran out, the run **fails closed** (`resource_limit_exceeded`, `resource: disk`) and its outputs are discarded, whatever the exit code. The verdict is HMAC-authenticated with a per-run key that reaches only the supervisor, on stdin. Outputs are then streamed out with a capped `docker exec tar` and validated as a whole before anything is written to `workspace/outputs/`. Only regular files and directories are accepted, at most `MAX_EXECUTION_OUTPUT_FILES` entries (10,000), with names that are safe under both POSIX and Windows rules and do not differ only by case or Unicode form. The files are staged in a private directory inside `outputs/` and renamed into place only after all of them have been written. Renames never follow a symlink, never write through a hardlink, and never replace a registered source; on any error the outputs already moved are removed and the earlier files they replaced are restored (`resource: output_archive`). If artifact registration fails, the run's outputs are removed and the files they replaced are restored in the same way. Restoring is best effort: a host I/O error during the restore itself can leave a replaced file in its new state. The tmpfs lives only inside the container, so it is gone after success, failure, timeout, or kill. | Managed scripts **must write under `outputs/`**; any other workspace write fails with `EROFS`. `outputs/` starts empty on every run: files from earlier runs (and a registered source stored under `outputs/`) are hidden during the run and are never overwritten when protected; read them by their artifact or move inputs elsewhere. A timed-out run keeps no partial outputs. The 256 MiB default is **inferred from test and quickstart outputs** (a CSV and a PNG, a few MB), not measured on real workloads. **Detecting that the quota was hit is not sound in every case**: a script that fills the quota, catches `ENOSPC`, *truncates* the file (which frees pages even while a descriptor is held) and exits 0 between two samples can be reported as succeeded. The host is still protected, because the kernel limit holds. Copy-out adds up to the quota in host disk use and in a temporary spool file per run, plus a temporary copy of each earlier output it replaces. Other host-specific Docker hardening (rootless Docker, user namespaces, seccomp/AppArmor profiles) is the deployment's responsibility. Docker Desktop on macOS and Windows provides the same tmpfs inside its Linux VM. The non-conformant `subprocess_dev` backend has no tmpfs; it keeps the polling output budget (`MAX_EXECUTION_OUTPUT_BYTES` / `_FILES`) and is limited to permissive sessions. |
| SQL / inspection / validation (DuckDB) | Every DuckDB operation runs in a **separate, short-lived worker process**; the MCP server never loads DuckDB. The worker gets OS limits (address space `DATA_PLANE_MEMORY_BYTES`, CPU seconds, largest writable file `DATA_PLANE_MAX_FILE_BYTES`, an aggregate DuckDB temp-spill budget `DATA_PLANE_MAX_TEMP_BYTES` (DuckDB's own default is 90% of free disk), open files, no core dumps), a scrubbed environment (no server secrets), a private scratch directory, bounded output (`DATA_PLANE_MAX_RESPONSE_BYTES`), and a hard wall-clock kill. A crash, OOM, hang, or malformed source fails that one operation (audited as `failed`/`timed_out` with an error `category`; messages that could name server paths are replaced with fixed text) while the server keeps running. Inside the worker: read-only SQL over registered sources only, file access confined to the listed files (`allowed_paths`), external access, extension loading, and configuration changes disabled, DuckDB memory and time limits, and source fingerprints re-checked before each query. **Privilege isolation (Linux, x86_64 and aarch64):** before DuckDB loads, the worker sets `no_new_privs` and confines itself with two kernel controls that need no root. *Landlock* (kernel 5.13+): it may read only the Python runtime, system libraries, a few host-fact files under `/proc/self`, `/sys` and `/etc`, and the operation's authorized source files (opened without following a symlink in any path component; a symlink swapped in for the file or for one of its parent directories, or a non-regular file, fails closed), and may create, write, or delete only regular files and directories in its private scratch directory and the query's own per-execution output directory (no symlinks, device nodes, sockets, FIFOs, or execution anywhere). The server's `/proc` entries, the workspace, server state, and other sessions' archives are outside the allowlist. *seccomp-bpf*: no socket of any address family can be created and the socket calls are refused, so pathname Unix sockets (for example `/var/run/docker.sock`), UDP, TCP, and every other family are unreachable; `execve`, `fork`/`vfork` and non-thread `clone`, `ptrace`, `process_vm_readv`/`writev`, io_uring, `pidfd_*`, `unshare`/`setns`, `bpf`, `perf_event_open` and `userfaultfd` are denied; `kill`, `tkill` and `tgkill` may target only the worker itself; changing `PR_SET_PDEATHSIG` is refused; `prlimit64`, `sched_setaffinity`, `sched_setscheduler`, `sched_setparam`, `sched_setattr`, `setpriority` and `ioprio_set` work only on the worker itself, and `fcntl(F_SETOWN/F_SETSIG/F_SETOWN_EX)` is refused, so the worker cannot change another process's limits, scheduling or priority or have a signal delivered to it; changing a file's mode, owner, times or extended attributes is refused for every file, the worker's own scratch files included (the `chmod`, `chown`, `utime`/`utimes`/`utimensat`/`futimesat` and set/remove-xattr families and `file_setattr`); kernel keyrings (`add_key`, `request_key`, `keyctl`), SysV IPC (`shm*`, `msg*`, `sem*`) and POSIX message queues (`mq_open`, `mq_unlink`) are refused; a foreign syscall ABI (i386, x32, arm32) kills the worker. If any part cannot be installed, the operation fails closed. Startup also fails closed if a runtime read location is equal to or an ancestor of a workspace root, the state directory, or the archive root (for example a workspace under `/usr/src/app`). *DuckDB settings* add a second, independent layer (listed above). A compromised worker therefore cannot read file contents in, or create, write, rename or delete files in, the workspace, server state, or other sessions' archives, even through its own unrestricted DuckDB connection, and cannot reach the Docker API or the network. **Lifetime:** the worker is bound to the server (`PR_SET_PDEATHSIG` on Linux, which a compromised worker cannot clear and cannot evade with a child process; a parent-polling watchdog where the death signal is unavailable), so it exits when the server is killed. | The worker still runs as the **server's OS user**; the boundary is the Landlock domain plus the seccomp filter, not a separate uid. It can still read the runtime read roots (`/usr`, `/lib*`, the interpreter prefix and `sys.path`), so do not keep data there; startup refuses only the overlaps it can see (workspace roots, state, archives). On kernels with **Landlock ABI < 4** (for example Linux 5.15) TCP is blocked by seccomp only, and on **ABI < 6** (for example 6.8) signal and abstract-socket scoping comes from seccomp only: `kill`-family signals to other pids and the `fcntl` signal-owner commands are still refused, but these layers are no longer doubled. The worker can still `stat` any path (existence, size, times). **The confinement assumes a non-root server:** the filter is a default-allow deny-list and does not deny the calls the kernel gates on capabilities (`mount`, `pivot_root`, `open_by_handle_at`, `init_module`, `kexec_load` and so on), so a server running as root with capabilities is much less confined. Denying those calls, and replacing the deny-list with an allow-list built from a traced DuckDB run, are still open ([#26](https://github.com/kurtvalcorza/agentic-analytics/issues/26)). Already-open descriptors are not revoked; the worker inherits only its three pipes. `PR_SET_PDEATHSIG` fires when the server *thread* that spawned the worker exits; the client keeps that thread blocked on the worker until it is reaped. The watchdog (macOS, Windows, or a failed `prctl`) polls every 0.25 s and runs inside the worker, so on those platforms a compromised worker could stop it. Other Linux architectures fail closed under `auto`. `DATA_PLANE_SANDBOX` = `auto` (default) enforces Landlock and seccomp on Linux and **fails closed** (`category: sandbox_unavailable`, with a fixed message) when the kernel lacks either; `required` fails closed on every host without them; `off` is an explicit opt-out. **macOS and Windows have no Landlock**: there the worker runs without filesystem confinement under `auto` (DuckDB's own `allowed_paths` and disabled external access still apply) and only the parent-death watchdog holds; use `required` to refuse such hosts. For hostile multi-tenant data on those platforms, run the server itself in a Linux container. The temp budget is DuckDB's own accounting per operation, not a filesystem quota; concurrent operations each get their own budget, so a hard host guarantee needs a quota-limited scratch volume. OS resource limits need a POSIX host; on Windows the worker still isolates crashes and hangs. Each operation pays ~0.1–0.3 s of process start-up. |
| Source lineage | Every execution and evidence item carries an assurance label. `managed_sql` executions are `enforced`: DuckDB can only reach the declared sources. Evidence is `source_lineage: "enforced"` only when every execution behind it is enforced. | Managed Python `source_ids` are **`declared`**: fingerprinted, but the program can read any workspace file, so declared lineage is not proof of data usage. Direct `source_fact` items are also caller-declared. |
| Artifacts | The recorded SHA-256 and size are computed from exactly the bytes written to a private temporary archive file, which is fsynced and atomically renamed. A source file whose metadata (size, mtime, ctime, inode) changed after the execution snapshot or during the copy is refused, not attested. Archive files are `0600` and server state `0700`. | Archived bytes are what the workspace held at registration; they are not proof the code computed them correctly. |
| Query results | `result_complete`, `spill_truncated`, `artifact_row_count`, and `max_spill_rows` distinguish preview truncation from an incomplete spilled artifact. | A capped artifact is an arbitrary row subset unless the query has a total `ORDER BY`. |
| Validation | Deterministic structural checks with explicit coverage. | Claim **values** are not compared with execution outputs (a claim of 4.2 backed by an execution that printed 3.1 is not detected), and statistical validity and causal identification are not verified. `causal_design` / `analysis_design` are caller declarations; causal wording accepted on that basis produces an informational `CAUSAL_DESIGN_ASSERTED` finding. |

**Prompt-injection boundary.** Sample rows, query previews, stdout, and every other value derived from a dataset are **untrusted data**. Text in a CSV cell or a script's output must never be treated as instructions or as authority for tool calls; the MCP host and agent orchestration are responsible for keeping tool results in the data channel.

**Deployment assumptions.** The development backend (`subprocess_dev`) is not a security boundary and is limited to permissive sessions. Do not place privileged control sockets (for example `docker.sock`) or host control filesystems inside an authorized workspace. List responses (`list_artifacts`, `list_evidence`) are paginated with `limit` / `offset` / `next_offset`, and source discovery stops after `MAX_DISCOVERY_SCAN_ENTRIES` filesystem entries (`scan_truncated`).

The remaining hardening work is tracked in the security-hardening RFC (issue #10).

## Development

Requires Python 3.12+.

CI installs exactly the reviewed `uv.lock` (`uv sync --frozen`), pins GitHub Actions and the sandbox base image by digest, and runs a dependency vulnerability scan plus SBOM generation. To reproduce locally:

```bash
uv sync --frozen --extra dev
uv run ruff check .
uv run mypy src
uv run pytest
```

Without uv, `python -m pip install -e '.[dev]'` installs from the version ranges in `pyproject.toml` instead of the lockfile.

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

## License

Released under the MIT License. See [LICENSE](LICENSE).
