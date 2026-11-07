"""SEC-02: DuckDB runs out of process; failures are contained and the server keeps working."""

import contextlib
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agentic_analytics.dataplane import DataPlaneClient, DataPlaneError
from agentic_analytics.models import AnalysisSession, ExecutionStatus, SessionMode
from agentic_analytics.runtime import Runtime
from agentic_analytics.services.inspector import SourceInspectionError
from agentic_analytics.services.query import QueryExecutionError
from agentic_analytics.settings import Settings


def _runtime(tmp_path: Path, **overrides: Any) -> tuple[Runtime, AnalysisSession, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data.csv").write_text("value\n1\n2\n3\n", encoding="utf-8")
    settings: dict[str, Any] = {
        "state_dir": tmp_path / "state",
        "allowed_workspace_roots": [workspace],
        "execution_backend": "subprocess_dev",
    }
    settings.update(overrides)
    runtime = Runtime.create(Settings(**settings))
    session = runtime.sessions.add(
        AnalysisSession(workspace_root=str(workspace), mode=SessionMode.PERMISSIVE)
    )
    return runtime, session, workspace


def _assert_server_still_works(runtime: Runtime, session: AnalysisSession) -> None:
    source, _ = runtime.inspector.inspect(session, "data.csv")
    result = runtime.query.execute(session, f"SELECT sum(value) AS s FROM source('{source.id}')")
    assert result["rows"] == [[6]]


class _FakeWorker(DataPlaneClient):
    """Replaces the worker program to simulate failures DuckDB cannot be made to produce."""

    kill_grace_seconds = 1.0
    script = ""

    def worker_command(self) -> list[str]:
        return [sys.executable, "-c", textwrap.dedent(self.script)]


def _fake(tmp_path: Path, script: str, **settings: Any) -> DataPlaneClient:
    client = type("Fake", (_FakeWorker,), {"script": script})(
        Settings(state_dir=tmp_path / "state", **settings)
    )
    return client


def _call(client: DataPlaneClient, deadline: float = 30) -> dict[str, Any]:
    return client.call("profile", {"limits": client.limits(deadline)}, deadline_seconds=deadline)


# --- containment of worker failures -----------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="signals are POSIX specific")
def test_native_crash_is_contained(tmp_path: Path) -> None:
    client = _fake(tmp_path, "import os, signal\nos.kill(os.getpid(), signal.SIGSEGV)\n")

    with pytest.raises(DataPlaneError) as caught:
        _call(client)

    assert caught.value.category == "crashed"
    assert "SIGSEGV" in str(caught.value)


def test_hung_worker_is_killed_at_its_deadline(tmp_path: Path) -> None:
    client = _fake(tmp_path, "import time\ntime.sleep(120)\n")
    started = time.monotonic()

    with pytest.raises(DataPlaneError) as caught:
        _call(client, deadline=1)

    assert caught.value.category == "timeout"
    assert time.monotonic() - started < 15


def test_flooding_worker_output_is_bounded(tmp_path: Path) -> None:
    client = _fake(
        tmp_path,
        "import sys\nfor _ in range(64):\n    sys.stdout.write('x' * 1024 * 1024)\n",
        data_plane_max_response_bytes=1024 * 1024,
    )

    with pytest.raises(DataPlaneError) as caught:
        _call(client)

    assert caught.value.category == "response_too_large"


def test_worker_environment_excludes_server_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTIC_ANALYTICS_TEST_API_KEY", "server-secret")
    client = _fake(
        tmp_path,
        "import json, os\nprint(json.dumps({'result': {'env': dict(os.environ)}}))\n",
    )

    env = _call(client)["env"]

    assert "AGENTIC_ANALYTICS_TEST_API_KEY" not in env
    assert "server-secret" not in json.dumps(env)


def test_worker_scratch_directory_is_removed(tmp_path: Path) -> None:
    client = _fake(tmp_path, "import json\nprint(json.dumps({'result': {}}))\n")
    _call(client)

    assert list(client.work_root.iterdir()) == []


# --- real DuckDB failures, end to end -----------------------------------------------------


def test_query_timeout_is_contained_and_audited(tmp_path: Path) -> None:
    runtime, session, _ = _runtime(tmp_path, query_timeout_seconds=1)
    source, _ = runtime.inspector.inspect(session, "data.csv")

    with pytest.raises(QueryExecutionError, match="time limit"):
        runtime.query.execute(
            session,
            "SELECT count(*) FROM source('" + source.id + "'), range(100000000000) AS r(i) "
            "WHERE i % 7 = 3",
        )

    [failed] = [
        record
        for record in runtime.executions.list(session.id)
        if record.status is ExecutionStatus.TIMED_OUT
    ]
    assert failed.error is not None and failed.error["category"] == "timeout"
    _assert_server_still_works(runtime, session)


@pytest.mark.skipif(os.name != "posix", reason="OS address-space limits are POSIX only")
def test_query_memory_exhaustion_is_contained(tmp_path: Path) -> None:
    # DuckDB's own limit is set above the OS limit so the worker's address-space cap is what
    # stops the allocation.
    runtime, session, _ = _runtime(
        tmp_path,
        data_plane_memory_bytes=512 * 1024**2,
        query_memory_limit="128MB",
        query_timeout_seconds=60,
    )
    source, _ = runtime.inspector.inspect(session, "data.csv")
    # Settings validation forbids this combination; force it after validation so the OS
    # address-space limit, not DuckDB's own memory_limit, is what stops the allocation.
    runtime.settings.query_memory_limit = "8GB"

    with pytest.raises(QueryExecutionError):
        runtime.query.execute(
            session,
            f"SELECT (SELECT list(i) FROM range(400000000) AS r(i)) FROM source('{source.id}')",
        )

    failed = [
        record
        for record in runtime.executions.list(session.id)
        if record.status is ExecutionStatus.FAILED
    ]
    assert failed and failed[0].error is not None
    assert failed[0].error["category"] == "out_of_memory"
    _assert_server_still_works(runtime, session)


@pytest.mark.parametrize(
    ("name", "content", "must_reject"),
    [
        ("broken.parquet", b"PAR1" + os.urandom(4096) + b"PAR1", True),
        # DuckDB may tolerate arbitrary bytes in a CSV; it must just not take the server down.
        ("binary.csv", b"a,b\n" + bytes(range(256)) * 64, False),
    ],
)
def test_malformed_source_is_contained(
    tmp_path: Path, name: str, content: bytes, must_reject: bool
) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    (workspace / name).write_bytes(content)

    if must_reject:
        with pytest.raises(SourceInspectionError):
            runtime.inspector.inspect(session, name)
    else:
        with contextlib.suppress(SourceInspectionError):
            runtime.inspector.inspect(session, name)

    _assert_server_still_works(runtime, session)


def test_large_source_profiles_under_worker_limits(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    rows = 200_000
    pq.write_table(
        pa.table({"id": range(rows), "group": [f"g{i % 50}" for i in range(rows)]}),
        workspace / "big.parquet",
    )

    source, profile = runtime.inspector.inspect(session, "big.parquet")

    assert profile["row_count"] == rows
    assert source.row_count == rows


# --- the server never loads DuckDB --------------------------------------------------------


def test_server_process_never_imports_duckdb(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data.csv").write_text("value\n1\n2\n", encoding="utf-8")
    script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        from agentic_analytics.models import AnalysisSession, SessionMode
        from agentic_analytics.runtime import Runtime
        from agentic_analytics.settings import Settings

        workspace = Path({str(workspace)!r})
        runtime = Runtime.create(Settings(
            state_dir=Path({str(tmp_path / "state")!r}),
            allowed_workspace_roots=[workspace],
            execution_backend="subprocess_dev",
        ))
        session = runtime.sessions.add(
            AnalysisSession(workspace_root=str(workspace), mode=SessionMode.PERMISSIVE)
        )
        source, _ = runtime.inspector.inspect(session, "data.csv")
        runtime.query.execute(session, f"SELECT * FROM source('{{source.id}}')")
        runtime.validation.validate(session)
        print("duckdb" in sys.modules)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "False"


# --- OS limits and DuckDB confinement inside the worker ------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="resource limits are POSIX only")
def test_worker_applies_os_resource_limits() -> None:
    script = textwrap.dedent(
        """
        import json, resource
        from agentic_analytics.dataplane.worker import _apply_limits
        _apply_limits({
            "address_space_bytes": 700 * 1024 ** 2,
            "cpu_seconds": 17,
            "max_file_bytes": 5 * 1024 ** 2,
            "max_open_files": 64,
        })
        print(json.dumps({
            name: resource.getrlimit(getattr(resource, name))[0]
            for name in ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NOFILE", "RLIMIT_CORE")
        }))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    limits = json.loads(completed.stdout)

    assert limits == {
        "RLIMIT_AS": 700 * 1024**2,
        "RLIMIT_CPU": 17,
        "RLIMIT_FSIZE": 5 * 1024**2,
        "RLIMIT_NOFILE": 64,
        "RLIMIT_CORE": 0,
    }


def test_engine_connections_cannot_read_unlisted_files(tmp_path: Path) -> None:
    import duckdb

    from agentic_analytics.dataplane import engine

    allowed = tmp_path / "allowed.csv"
    allowed.write_text("x\n1\n", encoding="utf-8")
    secret = tmp_path / "secret.csv"
    secret.write_text("token\nhunter2\n", encoding="utf-8")
    limits = engine.EngineLimits(
        timeout_seconds=30, memory_limit="256MB", preview_bytes=4096, cell_chars=64
    )
    connection = duckdb.connect()
    try:
        engine.secure_connection(connection, [str(allowed)], limits)
        assert connection.execute(f"SELECT * FROM read_csv('{allowed}')").fetchall() == [(1,)]
        with pytest.raises(duckdb.Error):
            connection.execute(f"SELECT * FROM read_csv('{secret}')").fetchall()
        with pytest.raises(duckdb.Error):
            connection.execute("SET enable_external_access = true")
    finally:
        connection.close()


def test_validation_survives_worker_failure(tmp_path: Path) -> None:
    runtime, session, _ = _runtime(tmp_path)
    runtime.inspector.inspect(session, "data.csv")
    crashing = _fake(tmp_path, "import os\nos._exit(9)\n")
    runtime.validation.data_plane = crashing

    run, _ = runtime.validation.validate(session, checks=["duplicates", "missingness"])

    # A worker failure makes the data checks inconclusive, never a clean pass or a crash.
    assert run.status.value == "warnings"
    assert {item["check"] for item in run.checks_inconclusive} == {"duplicates", "missingness"}
    assert all("WorkerCrashed" in item["reason"] for item in run.checks_inconclusive)


def test_unknown_duplicate_key_is_a_caller_error(tmp_path: Path) -> None:
    runtime, session, _ = _runtime(tmp_path)
    runtime.inspector.inspect(session, "data.csv")

    with pytest.raises(ValueError, match="unknown duplicate key column") as caught:
        runtime.validation.validate(
            session, checks=["duplicates"], duplicate_keys={"data.csv": ["no_such_column"]}
        )
    # DuckDB's binder error quotes the SQL, including the absolute source path; it must never
    # reach the caller.
    assert "no_such_column" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    assert "read_csv" not in str(caught.value)


def test_file_size_limit_is_its_own_category_without_paths(tmp_path: Path) -> None:
    # A sort far larger than DuckDB's memory limit must spill to a temp file, which hits the
    # worker's 1 MiB file-size cap.
    runtime, session, _ = _runtime(
        tmp_path,
        query_memory_limit="64MB",
        data_plane_max_file_bytes=1024**2,
        max_artifact_bytes=1024**2,
        max_total_artifact_bytes=1024**2,
        query_timeout_seconds=120,
    )
    source, _ = runtime.inspector.inspect(session, "data.csv")

    with pytest.raises(QueryExecutionError) as caught:
        runtime.query.execute(
            session,
            "SELECT count(*) FROM (SELECT i, repeat('x', 100) AS s FROM source('"
            + source.id
            + "'), range(5000000) AS r(i) ORDER BY s, i DESC)",
        )

    assert "disk space" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    [failed] = [
        record
        for record in runtime.executions.list(session.id)
        if record.status is ExecutionStatus.FAILED
    ]
    assert failed.error is not None and failed.error["category"] == "disk_limit"
    _assert_server_still_works(runtime, session)


def test_many_threads_fit_the_default_address_space(tmp_path: Path) -> None:
    runtime, session, _ = _runtime(tmp_path, data_plane_threads=64)

    _assert_server_still_works(runtime, session)


@pytest.mark.parametrize(
    "overrides",
    [
        {"query_memory_limit": "8GB"},
        {"data_plane_memory_bytes": 256 * 1024**2},
        {"max_artifact_bytes": 9 * 1024**3},
    ],
    ids=["duckdb-limit-exceeds-worker", "below-floor", "artifact-exceeds-file-cap"],
)
def test_settings_reject_worker_configurations_that_cannot_run(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        Settings(state_dir=tmp_path / "state", **overrides)


def test_stale_scratch_directories_are_swept(tmp_path: Path) -> None:
    client = _fake(tmp_path, "import json\nprint(json.dumps({'result': {}}))\n")
    client.work_root.mkdir(parents=True)
    stale = client.work_root / "query-stale"
    stale.mkdir()
    old = time.time() - 3 * 24 * 3600
    os.utime(stale, (old, old))
    fresh = client.work_root / "query-live"
    fresh.mkdir()

    _call(client)

    assert not stale.exists()
    # A recent directory may belong to another live server process sharing this state dir.
    assert fresh.exists()


# --- review follow-ups (PR 20) -------------------------------------------------------------


def test_worker_that_never_reads_stdin_cannot_block_past_deadline(tmp_path: Path) -> None:
    # The request is several times a pipe buffer (64 KiB on Linux) and the worker never reads
    # it, so a blocking write in the caller would hang forever.
    client = _fake(tmp_path, "import time\ntime.sleep(120)\n")
    payload = {"limits": client.limits(1), "padding": "x" * (8 * 1024 * 1024)}
    started = time.monotonic()

    with pytest.raises(DataPlaneError) as caught:
        client.call("profile", payload, deadline_seconds=1)

    assert caught.value.category == "timeout"
    assert time.monotonic() - started < 10


def test_aggregate_temp_spill_budget_is_enforced(tmp_path: Path) -> None:
    # Per-file cap is generous; only the aggregate temp budget can stop this spill.
    runtime, session, _ = _runtime(
        tmp_path,
        query_memory_limit="64MB",
        data_plane_max_temp_bytes=8 * 1024**2,
        query_timeout_seconds=120,
    )
    source, _ = runtime.inspector.inspect(session, "data.csv")

    with pytest.raises(QueryExecutionError) as caught:
        runtime.query.execute(
            session,
            "SELECT count(*) FROM (SELECT i, repeat('x', 100) AS s FROM source('"
            + source.id
            + "'), range(3000000) AS r(i) ORDER BY s, i DESC)",
        )

    assert "disk space" in str(caught.value)
    assert str(tmp_path) not in str(caught.value)
    [failed] = [
        record
        for record in runtime.executions.list(session.id)
        if record.status is ExecutionStatus.FAILED
    ]
    assert failed.error is not None and failed.error["category"] == "disk_limit"
    _assert_server_still_works(runtime, session)


def test_spill_write_failure_is_not_reported_as_source_read(tmp_path: Path) -> None:
    # Drive the worker directly so the spill's byte-ceiling watchdog is out of the way and the
    # write itself fails on the worker's 1 MiB file-size limit.
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data = workspace / "data.parquet"
    pq.write_table(
        pa.table({"s": [os.urandom(64).hex() for _ in range(200_000)]}), data
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    client = DataPlaneClient(
        Settings(
            state_dir=tmp_path / "state",
            data_plane_max_file_bytes=1024**2,
            max_artifact_bytes=1024**2,
            max_total_artifact_bytes=1024**2,
        )
    )

    with pytest.raises(DataPlaneError) as caught:
        client.call(
            "query",
            {
                "sources": [{"view": "v", "kind": "parquet", "path": str(data)}],
                "sql": 'SELECT * FROM "v"',
                "result_table": "t",
                "limit": 1,
                "spill_cap": 1_000_000,
                "spill_path": str(archive / "spill.parquet"),
                "spill_byte_ceiling": 10 * 1024**3,
                "limits": client.limits(120),
            },
            deadline_seconds=120,
        )

    assert caught.value.category == "disk_limit"
    assert str(tmp_path) not in str(caught.value)
