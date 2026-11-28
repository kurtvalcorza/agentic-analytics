"""SEC-01 / #22: each managed run writes only to a quota-limited, per-run tmpfs outputs/."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics.execution_backends import output_quota
from agentic_analytics.execution_backends.output_quota import (
    OutputArchiveError,
    _members,
    extract_outputs,
    member_parts,
    parse_verdict,
)
from agentic_analytics.models import AnalysisSession, ExecutionStatus, SessionMode
from agentic_analytics.runtime import Runtime
from agentic_analytics.settings import Settings

_posix = pytest.mark.skipif(os.name != "posix", reason="Docker integration runs on Linux CI")


def docker(test: Any) -> Any:
    """Mark a test that runs the real Docker backend (skipped without the image)."""

    return pytest.mark.docker(_posix(test))


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
    with pytest.raises(OutputArchiveError, match=r"link|escapes"):
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
    assert written == [destination.resolve() / "d", destination.resolve() / "d" / "f.bin"]
    assert (destination / "d" / "f.bin").read_bytes() == b"\x00\x01payload"


def test_verdict_must_carry_the_run_key() -> None:
    body = json.dumps(
        {
            "exit_code": 0,
            "timed_out": False,
            "quota_exhausted": False,
            "min_free_bytes": 5,
            "others_remaining": False,
        }
    )
    good = json.dumps({"body": body, "mac": hmac.new(b"k", body.encode(), "sha256").hexdigest()})
    assert parse_verdict(good, "k") is not None
    assert parse_verdict(good, "other-key") is None
    assert parse_verdict("not json", "k") is None


# --- review round 2 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "..\\..\\x.txt",
        "./..\\..\\ESCAPED.txt",
        "./..\\.agentic-analytics\\state\\planted.json",
        "a\\b.txt",
        "C:x.txt",
        "c:/x.txt",
        "//server/share/x.txt",
        "\\\\?\\C:\\x.txt",
        "a:b.txt",
        "CON",
        "nul.txt",
        "com1",
        "dir/LPT9.log",
        "conin$",
        "trailing.",
        "trailing ",
        "ctrl\x01.txt",
        "../x.txt",
        "/abs.txt",
    ],
)
def test_member_names_are_validated_under_windows_and_posix_rules(name: str) -> None:
    # Runs on every OS: the validator, not the host, decides what a Windows host would do.
    with pytest.raises(OutputArchiveError):
        member_parts(name)


def test_plain_names_are_accepted() -> None:
    assert member_parts("./a/b.c/report.final.csv") == ("a", "b.c", "report.final.csv")
    assert member_parts("console.txt") == ("console.txt",)


def test_windows_traversal_member_writes_nothing_outside_outputs(tmp_path: Path) -> None:
    destination = tmp_path / "workspace" / "outputs"
    destination.mkdir(parents=True)
    name = "./..\\..\\ESCAPED.txt"
    with pytest.raises(OutputArchiveError):
        extract_outputs(_tar(_file(name), data={name: b"x"}), destination, QUOTA)
    assert not list(tmp_path.rglob("*ESCAPED*"))


def test_member_count_is_capped_and_nothing_is_written(tmp_path: Path) -> None:
    destination = tmp_path / "outputs"
    destination.mkdir()
    names = [f"f{index}.txt" for index in range(5)]
    archive = _tar(*[_file(name) for name in names], data={name: b"x" for name in names})
    with pytest.raises(OutputArchiveError, match="more than 3 entries"):
        extract_outputs(archive, destination, QUOTA, max_members=3)
    assert list(destination.iterdir()) == []


def test_negative_header_size_is_refused() -> None:
    info = _file("bad.bin")
    info.size = -1
    with pytest.raises(OutputArchiveError, match="invalid size"):
        _members([info], QUOTA, 10)  # type: ignore[arg-type]


def test_file_directory_conflicts_are_refused_before_writing(tmp_path: Path) -> None:
    destination = tmp_path / "outputs"
    destination.mkdir()
    archive = _tar(_file("a"), _file("a/b"), data={"a": b"1", "a/b": b"2"})
    with pytest.raises(OutputArchiveError):
        extract_outputs(archive, destination, QUOTA)
    assert list(destination.iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="hardlinks to arbitrary files need POSIX")
def test_existing_hardlink_in_outputs_is_never_written_through(tmp_path: Path) -> None:
    destination = tmp_path / "outputs"
    destination.mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text("SERVER", encoding="utf-8")
    (destination / "linked.txt").hardlink_to(state_file)
    archive = _tar(_file("linked.txt"), data={"linked.txt": b"tampered"})
    with pytest.raises(OutputArchiveError, match="linked or protected"):
        extract_outputs(archive, destination, QUOTA)
    assert state_file.read_text(encoding="utf-8") == "SERVER"


def test_quota_must_leave_room_in_container_memory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="docker_memory"):
        Settings(state_dir=tmp_path, docker_memory="512m", docker_output_quota_bytes=512 * 1024**2)
    Settings(state_dir=tmp_path, docker_memory="1g", docker_output_quota_bytes=256 * 1024**2)
    # /dev/shm (64 MiB) is counted with /tmp (128 MiB): 448 MiB + 192 MiB = 640 MiB = 80% of 800m.
    with pytest.raises(ValueError, match="docker_memory"):
        Settings(state_dir=tmp_path, docker_memory="799m", docker_output_quota_bytes=448 * 1024**2)
    Settings(state_dir=tmp_path, docker_memory="800m", docker_output_quota_bytes=448 * 1024**2)


@pytest.mark.parametrize("memory", ["1.5g", "1536MiB", "1 g", "0.001t", "1610612736"])
def test_docker_memory_forms_docker_accepts_are_checked_against_the_quota(
    tmp_path: Path, memory: str
) -> None:
    Settings(state_dir=tmp_path, docker_memory=memory, docker_output_quota_bytes=256 * 1024**2)
    with pytest.raises(ValueError, match="docker_memory"):
        Settings(state_dir=tmp_path, docker_memory=memory, docker_output_quota_bytes=2 * 1024**3)


@pytest.mark.parametrize("memory", ["lots", "1x", "", "1.5.2g", "-1g"])
def test_unparsable_docker_memory_is_refused(tmp_path: Path, memory: str) -> None:
    with pytest.raises(ValueError, match="docker_memory"):
        Settings(state_dir=tmp_path, docker_memory=memory)


@docker
def test_setsid_survivor_is_killed_before_judgement_and_cannot_inject(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    # The reviewer's case: a double-forked setsid process outlives the script, waits for the
    # verdict, then tries to add a file and to write into the exec'd tar's stdout.
    code = """
import os, time
open('outputs/real.txt', 'w').write('real')
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        deadline = time.time() + 20
        while time.time() < deadline:
            if os.path.exists('/tmp/.agentic-analytics-verdict.json'):
                break
            time.sleep(0.005)
        open('outputs/injected.txt', 'w').write('injected')
        for pid in os.listdir('/proc'):
            try:
                if b'tar' in open('/proc/' + pid + '/cmdline', 'rb').read():
                    os.write(os.open('/proc/' + pid + '/fd/1', os.O_WRONLY), b'x' * 4096)
            except OSError:
                pass
        time.sleep(30)
    os._exit(0)
"""
    started = time.monotonic()
    record = _run(runtime, session, code, timeout=20)
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"run wedged for {elapsed:.1f}s"
    assert record.status is ExecutionStatus.SUCCEEDED, record.stderr_preview
    assert (workspace / "outputs" / "real.txt").read_text(encoding="utf-8") == "real"
    assert not (workspace / "outputs" / "injected.txt").exists()


@docker
def test_managed_code_cannot_read_the_verdict_key(tmp_path: Path) -> None:
    runtime, session, _ = _runtime(tmp_path)
    code = """
import os
for label, action in [
    ('environ', lambda: open('/proc/1/environ', 'rb').read()),
    ('mem', lambda: open('/proc/1/mem', 'rb').read(1)),
    ('fd0', lambda: open('/proc/1/fd/0', 'rb').read(1)),
    ('fdlist', lambda: os.listdir('/proc/1/fd')),
]:
    try:
        action()
        print(label + '=readable')
    except OSError:
        print(label + '=denied')
print('stdin=' + str(os.read(0, 100)))
"""
    record = _run(runtime, session, code)
    assert record.status is ExecutionStatus.SUCCEEDED, record.stderr_preview
    for label in ("environ", "mem", "fd0", "fdlist"):
        assert f"{label}=denied" in record.stdout_preview
    assert "stdin=b''" in record.stdout_preview  # the script's stdin is /dev/null


@docker
def test_fill_catch_unlink_and_exit_zero_still_fails_closed(tmp_path: Path) -> None:
    # The reviewer's case: deleting the partial file must not hide that the quota was hit.
    runtime, session, workspace = _runtime(tmp_path, docker_output_quota_bytes=16 * 1024**2)
    code = """
import os
try:
    with open('outputs/fill.bin', 'wb') as stream:
        while True:
            stream.write(b'x' * 65536)
except OSError:
    pass
os.unlink('outputs/fill.bin')
open('outputs/result.txt', 'w').write('small')
"""
    for _ in range(3):
        record = _run(runtime, session, code)
        assert record.status is ExecutionStatus.FAILED
        assert record.error is not None and record.error["resource"] == "disk"
        assert not (workspace / "outputs" / "result.txt").exists()


# Not tested: fill + catch ENOSPC + truncate + exit 0 between two samples. Truncation frees
# tmpfs pages even while the supervisor holds a descriptor, so detection is timing-dependent;
# the gap is documented in README ("Detecting that the quota was hit is not sound in every
# case") and the kernel quota still protects the host.


@docker
def test_member_cap_fails_closed_and_keeps_nothing(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path, max_execution_output_files=50)
    code = "for i in range(60):\n    open(f'outputs/f{i}.txt', 'w').write('x')"
    record = _run(runtime, session, code)
    assert record.status is ExecutionStatus.FAILED
    assert record.error is not None and record.error["resource"] == "output_archive"
    assert list((workspace / "outputs").iterdir()) == []


@docker
def test_outputs_are_removed_when_artifact_registration_fails(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    code = "for i in range(200):\n    open(f'outputs/f{i}.txt', 'w').write('x')"
    with pytest.raises(Exception):  # noqa: B017 - the service's artifact limit, any type
        runtime.execution.execute_python(session, code, timeout_seconds=30)
    assert _containers(session) == []
    assert list((workspace / "outputs").iterdir()) == []


# --- review round 3 ---------------------------------------------------------------------------


@docker
def test_zombie_leader_survivor_is_killed_before_judgement_and_cannot_inject(
    tmp_path: Path,
) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    # A thread-group leader that calls SYS_exit shows state Z in /proc while its other threads
    # keep running. A scan that treats Z as dead misses it, and the thread writes into outputs/
    # after the verdict.
    code = """
import ctypes, os, sys, threading, time
open('outputs/ok.txt', 'w').write('ok')
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        def late():
            time.sleep(0.15)
            open('outputs/injected.txt', 'w').write('written after the verdict')
            time.sleep(60)
        threading.Thread(target=late).start()
        ctypes.CDLL(None).syscall(60, 0)  # SYS_exit on x86-64: only the leader thread exits
        time.sleep(60)
    os._exit(0)
os.wait()
sys.exit(0)
"""
    started = time.monotonic()
    record = _run(runtime, session, code, timeout=20)
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"run wedged for {elapsed:.1f}s"
    assert record.status is ExecutionStatus.SUCCEEDED, record.stderr_preview
    assert sorted(path.name for path in (workspace / "outputs").iterdir()) == ["ok.txt"]


@docker
def test_fifo_at_the_verdict_staging_path_does_not_hold_the_run(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    code = """
import os
os.mkfifo('/tmp/.agentic-analytics-verdict.json.1')
open('outputs/ok.txt', 'w').write('ok')
"""
    started = time.monotonic()
    record = _run(runtime, session, code, timeout=6)
    assert time.monotonic() - started < 15
    assert record.status is ExecutionStatus.SUCCEEDED, record.stderr_preview
    assert (workspace / "outputs" / "ok.txt").read_text(encoding="utf-8") == "ok"


@docker
def test_unusable_verdict_staging_path_fails_the_run_at_once(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    code = """
import os
os.makedirs('/tmp/.agentic-analytics-verdict.json.1/occupied')
open('outputs/ok.txt', 'w').write('ok')
"""
    started = time.monotonic()
    record = _run(runtime, session, code, timeout=6)
    assert time.monotonic() - started < 15
    assert record.status is ExecutionStatus.FAILED
    assert "supervisor verdict" in (record.stderr_preview or "")
    assert list((workspace / "outputs").iterdir()) == []


@docker
def test_script_keeps_its_original_descriptor_limit(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    code = """
import resource
open('outputs/soft.txt', 'w').write(str(resource.getrlimit(resource.RLIMIT_NOFILE)[0]))
"""
    record = _run(runtime, session, code)
    assert record.status is ExecutionStatus.SUCCEEDED, record.stderr_preview
    default = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "agentic-analytics-exec:test",
            "python",
            "-c",
            "import resource; print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert (workspace / "outputs" / "soft.txt").read_text(encoding="utf-8") == default


@docker
def test_replaced_output_is_restored_when_artifact_registration_fails(tmp_path: Path) -> None:
    runtime, session, workspace = _runtime(tmp_path)
    (workspace / "outputs").mkdir()
    (workspace / "outputs" / "f0.txt").write_text("EARLIER RUN", encoding="utf-8")
    code = "for i in range(200):\n    open(f'outputs/f{i}.txt', 'w').write('x')"
    with pytest.raises(Exception):  # noqa: B017 - the service's artifact limit, any type
        runtime.execution.execute_python(session, code, timeout_seconds=30)
    assert _containers(session) == []
    assert [path.name for path in (workspace / "outputs").iterdir()] == ["f0.txt"]
    assert (workspace / "outputs" / "f0.txt").read_text(encoding="utf-8") == "EARLIER RUN"


def _earlier(tmp_path: Path) -> Path:
    destination = tmp_path / "outputs"
    destination.mkdir()
    (destination / "report.csv").write_bytes(b"EARLIER RUN, GOOD")
    return destination


def test_failed_move_restores_an_output_that_was_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _earlier(tmp_path)
    archive = _tar(
        _file("report.csv"),
        _file("b.txt"),
        _file("sub/c.txt"),
        data={"report.csv": b"FROM THE FAILED RUN", "b.txt": b"1", "sub/c.txt": b"2"},
    )
    real_replace = os.replace
    calls = 0

    def flaky(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:  # report.csv has been replaced; moving b.txt into place fails
            raise OSError(28, "No space left on device")
        real_replace(*args, **kwargs)

    monkeypatch.setattr(output_quota.os, "replace", flaky)
    with pytest.raises(OutputArchiveError, match="could not extract"):
        extract_outputs(archive, destination, QUOTA)
    assert calls >= 3  # the move that failed, then the restore
    assert [path.name for path in destination.iterdir()] == ["report.csv"]
    assert (destination / "report.csv").read_bytes() == b"EARLIER RUN, GOOD"


def test_failed_staging_leaves_earlier_outputs_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = _earlier(tmp_path)
    archive = _tar(
        _file("report.csv"),
        _file("b.txt"),
        data={"report.csv": b"FROM THE FAILED RUN", "b.txt": b"1"},
    )
    real_copy = output_quota.shutil.copyfileobj
    calls = 0

    def flaky(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError(28, "No space left on device")
        real_copy(*args, **kwargs)

    monkeypatch.setattr(output_quota.shutil, "copyfileobj", flaky)
    monkeypatch.setattr(
        output_quota.os, "replace", lambda *a, **k: pytest.fail("nothing may be moved in")
    )
    with pytest.raises(OutputArchiveError, match="could not extract"):
        extract_outputs(archive, destination, QUOTA)
    assert [path.name for path in destination.iterdir()] == ["report.csv"]
    assert (destination / "report.csv").read_bytes() == b"EARLIER RUN, GOOD"


def test_names_that_collide_on_case_insensitive_hosts_are_refused_everywhere(
    tmp_path: Path,
) -> None:
    destination = _earlier(tmp_path)
    cases = [
        ("report.csv", "a", "A/b"),  # the reviewer's write-phase trigger on Windows
        ("report.csv", "Result.csv", "result.csv"),
        ("report.csv", "Dir/x", "dir/y"),
        ("report.csv", "caf\u00e9.txt", "cafe\u0301.txt"),  # NFC and NFD spellings
    ]
    for names in cases:
        archive = _tar(*[_file(name) for name in names], data=dict.fromkeys(names, b"new"))
        with pytest.raises(OutputArchiveError, match="differ only by case"):
            extract_outputs(archive, destination, QUOTA)
        assert [path.name for path in destination.iterdir()] == ["report.csv"]
        assert (destination / "report.csv").read_bytes() == b"EARLIER RUN, GOOD"


def test_staging_name_is_reserved_and_no_staging_directory_is_left(tmp_path: Path) -> None:
    destination = _earlier(tmp_path)
    name = output_quota.STAGING_PREFIX + "x/f.txt"
    with pytest.raises(OutputArchiveError, match="reserved name"):
        extract_outputs(_tar(_file(name), data={name: b"x"}), destination, QUOTA)
    extract_outputs(_tar(_file("new.txt"), data={"new.txt": b"n"}), destination, QUOTA)
    assert sorted(path.name for path in destination.iterdir()) == ["new.txt", "report.csv"]


def test_remove_outputs_restores_what_a_successful_copy_out_replaced(tmp_path: Path) -> None:
    destination = _earlier(tmp_path)
    restore_dir = tmp_path / "state" / "outputs-restore"
    archive = _tar(
        _file("report.csv"),
        _file("sub/new.txt"),
        data={"report.csv": b"NEW", "sub/new.txt": b"n"},
    )
    created = extract_outputs(archive, destination, QUOTA, restore_dir=restore_dir)
    assert (destination / "report.csv").read_bytes() == b"NEW"
    assert created == [destination.resolve() / "sub", destination.resolve() / "sub" / "new.txt"]

    output_quota.remove_outputs([str(path) for path in created], restore_dir)
    assert [path.name for path in destination.iterdir()] == ["report.csv"]
    assert (destination / "report.csv").read_bytes() == b"EARLIER RUN, GOOD"
