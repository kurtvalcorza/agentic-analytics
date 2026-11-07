"""SEC-01 / OPS-01: managed workloads cannot exhaust host disk or server memory."""

import os
import threading
import time
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics.execution_backends import guards
from agentic_analytics.execution_backends.guards import DiskLimits, WorkspaceDiskGuard
from agentic_analytics.models import AnalysisSession, ExecutionStatus, SessionMode
from agentic_analytics.runtime import Runtime
from agentic_analytics.settings import Settings


def _runtime(tmp_path: Path, **overrides: Any) -> tuple[Runtime, AnalysisSession, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.csv").write_text("x\n1\n", encoding="utf-8")
    settings: dict[str, Any] = {
        "state_dir": tmp_path / "state",
        "allowed_workspace_roots": [workspace],
        "execution_backend": "subprocess_dev",
        "max_execution_output_bytes": 256 * 1024,
        "max_execution_output_files": 50,
        "min_free_disk_bytes": 0,
    }
    settings.update(overrides)
    runtime = Runtime.create(Settings(**settings))
    session = runtime.sessions.add(
        AnalysisSession(workspace_root=str(workspace), mode=SessionMode.PERMISSIVE)
    )
    return runtime, session, workspace


_ONE_LARGE_FILE = """
import time
with open('big.bin', 'wb') as stream:
    for _ in range(4096):
        stream.write(b'x' * 65536)
        stream.flush()
        time.sleep(0.002)
"""

_MANY_SMALL_FILES = """
import time
for index in range(5000):
    with open(f'part-{index}.txt', 'w') as stream:
        stream.write('x')
    time.sleep(0.001)
"""


@pytest.mark.parametrize("code", [_ONE_LARGE_FILE, _MANY_SMALL_FILES], ids=["large", "many"])
def test_output_budget_terminates_workload_and_reclaims_disk(tmp_path: Path, code: str) -> None:
    runtime, session, workspace = _runtime(tmp_path)

    record = runtime.execution.execute_python(session, code, timeout_seconds=60)

    assert record.status is ExecutionStatus.FAILED
    assert record.error is not None
    assert record.error["type"] == "resource_limit_exceeded"
    assert record.error["resource"] == "disk"
    assert "output budget" in record.stderr_preview
    assert record.artifact_ids == []
    # Partial output created by the killed workload is removed; inputs are untouched.
    remaining = sorted(path.name for path in workspace.iterdir() if path.is_file())
    assert remaining == ["input.csv"]
    assert (workspace / "input.csv").read_text(encoding="utf-8") == "x\n1\n"
    # The failure is persisted as an auditable record.
    assert runtime.executions.get(session.id, record.id).status is ExecutionStatus.FAILED


def test_output_within_budget_is_registered(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)

    record = runtime.execution.execute_python(
        session, "open('small.txt', 'w').write('ok')", timeout_seconds=30
    )

    assert record.status is ExecutionStatus.SUCCEEDED
    assert len(record.artifact_ids) == 1
    assert (workspace / "small.txt").exists()


def test_free_space_reserve_terminates_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, session, _ = _runtime(tmp_path, min_free_disk_bytes=10 * 1024**3)

    class Usage:
        free = 1024

    monkeypatch.setattr(guards.shutil, "disk_usage", lambda _: Usage())
    started = time.monotonic()
    record = runtime.execution.execute_python(
        session, "import time\ntime.sleep(30)", timeout_seconds=60
    )

    assert time.monotonic() - started < 20
    assert record.status is ExecutionStatus.FAILED
    assert record.error is not None
    assert record.error["type"] == "resource_limit_exceeded"
    assert "reserve" in record.stderr_preview


def test_guard_ignores_preexisting_files_and_excluded_state(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = workspace / "state"
    state.mkdir(parents=True)
    (workspace / "existing.bin").write_bytes(b"x" * 100_000)
    limits = DiskLimits(max_output_bytes=50_000, max_output_files=5, min_free_bytes=0)
    guard = WorkspaceDiskGuard(workspace, limits, lambda _: None, excluded_roots=(state,))

    assert guard.check() is None
    (state / "archive.bin").write_bytes(b"x" * 200_000)
    assert guard.check() is None
    (workspace / "new.bin").write_bytes(b"x" * 100_000)
    reason = guard.check()
    assert reason is not None and "output budget" in reason


def test_dev_backend_bounds_captured_output(tmp_path: Path) -> None:
    runtime, session, _ = _runtime(tmp_path, max_output_chars=4096)

    record = runtime.execution.execute_python(
        session,
        "import sys\nfor _ in range(200):\n    sys.stdout.write('y' * 65536)\n",
        timeout_seconds=60,
    )

    assert record.status is ExecutionStatus.SUCCEEDED
    assert record.truncated is True
    assert len(record.stdout_preview) <= 4096


def test_drain_bounded_keeps_draining_past_cap() -> None:
    stream = StringIO("z" * 300_000)
    buffer: list[str] = []
    truncated = threading.Event()

    guards.drain_bounded(stream, buffer, 1000, truncated)

    assert "".join(buffer) == "z" * 1000
    assert truncated.is_set()
    assert stream.closed


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX specific")
def test_dev_backend_timeout_kills_descendants(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    marker = workspace / "child-alive.txt"
    code = f"""
import subprocess, sys
subprocess.Popen([sys.executable, '-c', '''
import time
from pathlib import Path
time.sleep(3)
Path({str(marker)!r}).write_text("alive")
'''])
import time
time.sleep(60)
"""
    record = runtime.execution.execute_python(session, code, timeout_seconds=1)

    assert record.status is ExecutionStatus.TIMED_OUT
    time.sleep(4)
    assert not marker.exists()
