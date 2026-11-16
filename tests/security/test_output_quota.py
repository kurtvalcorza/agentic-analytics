"""SEC-01 / #22: each managed run writes only to a quota-limited, per-run tmpfs outputs/."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics.execution_backends.output_quota import (
    OutputArchiveError,
    extract_outputs,
    parse_verdict,
)
from agentic_analytics.models import AnalysisSession, ExecutionStatus, SessionMode
from agentic_analytics.runtime import Runtime
from agentic_analytics.settings import Settings

docker = pytest.mark.skipif(os.name != "posix", reason="Docker integration runs on Linux CI")
QUOTA = 1024 * 1024


def _runtime(tmp_path: Path, **overrides: Any) -> tuple[Runtime, AnalysisSession, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.csv").write_text("x\n1\n", encoding="utf-8")
    settings: dict[str, Any] = {
        "state_dir": tmp_path / "state",
        "allowed_workspace_roots": [workspace],
        "execution_backend": "docker",
        "docker_image": "agentic-analytics-exec:test",
        "docker_output_quota_bytes": QUOTA,
        "min_free_disk_bytes": 0,
        "execution_timeout_seconds": 30,
        "max_execution_timeout_seconds": 30,
    }
    settings.update(overrides)
    runtime = Runtime.create(Settings(**settings))
    session = runtime.sessions.add(
        AnalysisSession(workspace_root=str(workspace), mode=SessionMode.STRICT)
    )
    return runtime, session, workspace


def _containers(session: AnalysisSession) -> list[str]:
    listed = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=agentic-analytics.session={session.id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return listed.stdout.split()


def _run(runtime: Runtime, session: AnalysisSession, code: str, timeout: int = 30) -> Any:
    record = runtime.execution.execute_python(session, code, timeout_seconds=timeout)
    # Checked before close_session's defensive sweep: the run itself must leave nothing behind,
    # and the tmpfs exists only inside its container.
    assert _containers(session) == []
    return record


# --- integration ------------------------------------------------------------------------------


@docker
def test_over_quota_write_fails_closed_and_discards_partial_outputs(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    code = """
import os
print('uid=' + str(os.getuid()), flush=True)
open('outputs/partial.txt', 'w').write('partial')
with open('outputs/fill.bin', 'wb') as stream:
    for _ in range(64):
        stream.write(b'x' * 65536)
"""
    record = _run(runtime, session, code)

    assert record.status is ExecutionStatus.FAILED
    assert record.error is not None and record.error["type"] == "resource_limit_exceeded"
    assert record.error["resource"] == "disk"
    assert "No space left on device" in record.stderr_preview or "quota" in record.stderr_preview
    assert "uid=0" not in record.stdout_preview
    assert record.artifact_ids == []
    assert list((workspace / "outputs").iterdir()) == []
    assert (workspace / "input.csv").read_text(encoding="utf-8") == "x\n1\n"


@docker
def test_quota_hit_fails_closed_even_when_the_script_catches_enospc_and_exits_zero(
    tmp_path: Path,
) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    code = """
import errno
open('outputs/partial.txt', 'w').write('partial')
try:
    with open('outputs/fill.bin', 'wb') as stream:
        for _ in range(64):
            stream.write(b'x' * 65536)
except OSError as exc:
    assert exc.errno == errno.ENOSPC
    print('caught ENOSPC', flush=True)
"""
    record = _run(runtime, session, code)

    assert "caught ENOSPC" in record.stdout_preview
    assert record.status is ExecutionStatus.FAILED
    assert record.error is not None and record.error["type"] == "resource_limit_exceeded"
    assert record.artifact_ids == []
    assert not (workspace / "outputs" / "partial.txt").exists()


@docker
def test_in_quota_run_succeeds_and_outputs_arrive_intact(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    payload = bytes(range(256)) * 1024  # 256 KiB, well inside the 1 MiB quota
    code = """
from pathlib import Path
Path('outputs/nested/deeper').mkdir(parents=True)
Path('outputs/nested/deeper/data.bin').write_bytes(bytes(range(256)) * 1024)
Path('outputs/table.csv').write_text('a,b\\n1,2\\n')
print('wrote', flush=True)
"""
    record = _run(runtime, session, code)

    assert record.status is ExecutionStatus.SUCCEEDED, record.stderr_preview
    assert (workspace / "outputs/nested/deeper/data.bin").read_bytes() == payload
    assert (workspace / "outputs/table.csv").read_text(encoding="utf-8") == "a,b\n1,2\n"
    artifacts = {
        runtime.artifacts.get(session.id, artifact_id).relative_path: artifact_id
        for artifact_id in record.artifact_ids
    }
    assert set(artifacts) == {"outputs/nested/deeper/data.bin", "outputs/table.csv"}
    data = runtime.artifacts.get(session.id, artifacts["outputs/nested/deeper/data.bin"])
    assert data.sha256 == hashlib.sha256(payload).hexdigest()
    assert record.runtime["output_quota_bytes"] == QUOTA


@docker
def test_writes_outside_outputs_are_refused(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    record = _run(runtime, session, "open('burst.bin', 'wb').write(b'x' * 4096)")

    assert record.status is ExecutionStatus.FAILED
    assert "Read-only file system" in record.stderr_preview
    assert not (workspace / "burst.bin").exists()


@docker
def test_symlink_in_outputs_fails_the_run_closed(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    code = """
import os
open('outputs/ok.txt', 'w').write('ok')
os.symlink('/etc/passwd', 'outputs/link')
"""
    record = _run(runtime, session, code)

    assert record.status is ExecutionStatus.FAILED
    assert record.error is not None and record.error["resource"] == "output_archive"
    assert record.artifact_ids == []
    assert not (workspace / "outputs" / "link").exists()
    assert not (workspace / "outputs" / "ok.txt").exists()


@docker
def test_failed_and_killed_runs_leave_no_container_or_mount(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    failed = _run(runtime, session, "open('outputs/a.txt', 'w').write('a')\nraise SystemExit(3)")
    assert failed.status is ExecutionStatus.FAILED and failed.error is not None
    assert failed.error["exit_code"] == 3

    killed = _run(
        runtime,
        session,
        "open('outputs/b.txt', 'w').write('b')\nimport time\ntime.sleep(60)",
        timeout=2,
    )
    assert killed.status is ExecutionStatus.TIMED_OUT
    assert not (workspace / "outputs" / "b.txt").exists()


@docker
def test_forged_verdict_from_managed_code_is_rejected(tmp_path: Path) -> None:
    runtime, session, _ = _runtime(tmp_path)
    forged = json.dumps(
        {
            "body": json.dumps({"exit_code": 0, "quota_exhausted": False, "min_free_bytes": 1}),
            "mac": "0" * 64,
        }
    )
    # A background process outlives the script and rewrites the verdict after the supervisor.
    code = f"""
import os, subprocess, sys
subprocess.Popen([sys.executable, '-c', '''
import os, time
os.setsid()
for _ in range(200):
    try:
        open("/tmp/.agentic-analytics-verdict.json", "w").write({forged!r})
    except OSError:
        pass
    time.sleep(0.01)
'''], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    open('outputs/fill.bin', 'wb').write(b'x' * (4 * 1024 * 1024))
except OSError:
    pass
"""
    record = _run(runtime, session, code)
    # The script exhausted its quota and exited 0; the forged "all clear" must not count.
    assert record.status is ExecutionStatus.FAILED
    assert record.artifact_ids == []


# --- archive extraction -----------------------------------------------------------------------


def _tar(*entries: tarfile.TarInfo, data: dict[str, bytes] | None = None) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for info in entries:
            content = (data or {}).get(info.name)
            if content is not None:
                info.size = len(content)
            archive.addfile(info, io.BytesIO(content) if content is not None else None)
    buffer.seek(0)
    return buffer


def _file(name: str) -> tarfile.TarInfo:
    return tarfile.TarInfo(name)


def _link(name: str, target: str, kind: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info


@pytest.mark.parametrize(
    "member",
    [
        _file("../escape.txt"),
        _file("nested/../../escape.txt"),
        _file("/abs/escape.txt"),
        _link("link", "/etc/passwd", tarfile.SYMTYPE),
        _link("link", "../../escape.txt", tarfile.SYMTYPE),
        _link("hard", "/etc/passwd", tarfile.LNKTYPE),
    ],
    ids=["dotdot", "nested-dotdot", "absolute", "symlink-abs", "symlink-rel", "hardlink"],
)
def test_unsafe_archive_members_are_refused_before_anything_is_written(
    tmp_path: Path, member: tarfile.TarInfo
) -> None:
    destination = tmp_path / "workspace" / "outputs"
    destination.mkdir(parents=True)
    archive = _tar(_file("first.txt"), member, data={"first.txt": b"1", member.name: b"x"})

    with pytest.raises(OutputArchiveError):
        extract_outputs(archive, destination, QUOTA)
    assert list(destination.iterdir()) == []
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "workspace" / "escape.txt").exists()


def test_archive_over_quota_is_refused(tmp_path: Path) -> None:
    destination = tmp_path / "outputs"
    destination.mkdir()
    archive = _tar(_file("a.bin"), _file("b.bin"), data={"a.bin": b"x" * 600, "b.bin": b"y" * 600})
    with pytest.raises(OutputArchiveError, match="quota"):
        extract_outputs(archive, destination, 1000)
    assert list(destination.iterdir()) == []


def test_existing_symlink_and_protected_source_are_never_written_through(tmp_path: Path) -> None:
    destination = tmp_path / "outputs"
    (destination / "real").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (destination / "dir-link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OutputArchiveError, match="symlink"):
        extract_outputs(
            _tar(_file("dir-link/x.txt"), data={"dir-link/x.txt": b"x"}), destination, QUOTA
        )
    assert list(outside.iterdir()) == []

    source = destination / "real" / "source.csv"
    source.write_text("original", encoding="utf-8")
    info = source.stat()
    with pytest.raises(OutputArchiveError, match="protected"):
        extract_outputs(
            _tar(_file("real/source.csv"), data={"real/source.csv": b"tampered"}),
            destination,
            QUOTA,
            protected={(info.st_dev, info.st_ino)},
        )
    assert source.read_text(encoding="utf-8") == "original"


def test_safe_archive_is_extracted_byte_for_byte(tmp_path: Path) -> None:
    destination = tmp_path / "outputs"
    destination.mkdir()
    directory = tarfile.TarInfo("d")
    directory.type = tarfile.DIRTYPE
    written = extract_outputs(
        _tar(directory, _file("d/f.bin"), data={"d/f.bin": b"\x00\x01payload"}),
        destination,
        QUOTA,
    )
    assert written == [destination.resolve() / "d" / "f.bin"]
    assert (destination / "d" / "f.bin").read_bytes() == b"\x00\x01payload"


def test_verdict_must_carry_the_run_key() -> None:
    body = json.dumps({"exit_code": 0, "quota_exhausted": False, "min_free_bytes": 5})
    good = json.dumps({"body": body, "mac": hmac.new(b"k", body.encode(), "sha256").hexdigest()})
    assert parse_verdict(good, "k") is not None
    assert parse_verdict(good, "other-key") is None
    assert parse_verdict("not json", "k") is None
