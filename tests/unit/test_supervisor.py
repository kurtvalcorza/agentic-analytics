"""C9: the in-container supervisor is a real module, tested here without Docker."""

from __future__ import annotations

import ast
import ctypes
import inspect
import os
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

resource = pytest.importorskip("resource", reason="the supervisor runs on Linux")

from agentic_analytics.execution_backends import docker, output_quota, supervisor  # noqa: E402


def _event(wd: int, mask: int, name: bytes, pad: int = 0) -> bytes:
    raw = name + b"\0" * pad
    return struct.pack("iIII", wd, mask, 0, len(raw)) + raw


def test_supervisor_imports_only_the_standard_library() -> None:
    # Only supervisor.py is mounted into the container, so it cannot import the package.
    tree = ast.parse(output_quota.SUPERVISOR_SOURCE)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0
    }
    assert not any(isinstance(n, ast.ImportFrom) and n.level for n in ast.walk(tree))
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


def test_mounted_source_is_the_module_and_paths_match_the_server() -> None:
    assert Path(supervisor.__file__).read_text(encoding="utf-8") == output_quota.SUPERVISOR_SOURCE
    assert supervisor.OUTPUT_DIR == output_quota.CONTAINER_OUTPUT_DIR
    assert supervisor.VERDICT_PATH == output_quota.VERDICT_PATH
    assert f"dst={supervisor.REQUEST_PATH}" in inspect.getsource(docker)


def test_parse_events_splits_padded_names() -> None:
    data = _event(1, supervisor.IN_CREATE, b"a.csv", pad=11) + _event(2, supervisor.IN_MODIFY, b"")

    assert supervisor.parse_events(data) == [
        (1, supervisor.IN_CREATE, "a.csv"),
        (2, supervisor.IN_MODIFY, ""),
    ]


def test_parse_events_ignores_a_truncated_tail() -> None:
    assert supervisor.parse_events(_event(1, supervisor.IN_CREATE, b"x")[:10]) == []


def test_pin_capacity_follows_the_descriptor_limit() -> None:
    unlimited = resource.RLIM_INFINITY
    assert supervisor.nofile_target(1024, unlimited) == supervisor.PIN_LIMIT + 256
    assert supervisor.nofile_target(1024, 4096) == 4096
    assert supervisor.nofile_target(4096, 4096) is None
    assert supervisor.nofile_target(unlimited, unlimited) is None
    assert supervisor.pin_cap_for(unlimited) == supervisor.PIN_LIMIT
    assert supervisor.pin_cap_for(4096) == 4096 - supervisor.FD_RESERVE
    assert supervisor.pin_cap_for(10) == 0


def _watcher(root: Path, inotify_fd: int = -1) -> supervisor.OutputWatcher:
    libc = ctypes.CDLL(None, use_errno=True)
    watcher = supervisor.OutputWatcher(str(root), inotify_fd, libc.inotify_add_watch)
    watcher.pin_cap = 100
    return watcher


def test_watch_dir_pins_existing_files_recursively_and_skips_links(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "a.csv").write_text("a")
    (tmp_path / "nested" / "b.csv").write_text("b")
    (tmp_path / "link").symlink_to(tmp_path / "a.csv")
    watcher = _watcher(tmp_path)
    try:
        watcher.watch_dir(str(tmp_path))
        pinned = {os.readlink(f"/proc/self/fd/{fd}") for fd in watcher.pinned}
        assert pinned == {str(tmp_path / "a.csv"), str(tmp_path / "nested" / "b.csv")}
    finally:
        for fd in watcher.pinned:
            os.close(fd)


def test_track_stops_at_the_pin_cap(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"{index}.csv").write_text("x")
    watcher = _watcher(tmp_path)
    watcher.pin_cap = 2
    try:
        watcher.watch_dir(str(tmp_path))
        assert len(watcher.pinned) == 2
    finally:
        for fd in watcher.pinned:
            os.close(fd)


def test_run_pins_files_created_while_watching(tmp_path: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    ifd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    assert ifd >= 0
    watcher = _watcher(tmp_path, ifd)
    watcher.watch_dir(str(tmp_path))
    thread = threading.Thread(target=watcher.run, daemon=True)
    thread.start()
    try:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "late.csv").write_text("late")
        deadline = time.monotonic() + 5
        while len(watcher.pinned) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(watcher.pinned) == 1
        assert os.readlink(f"/proc/self/fd/{watcher.pinned[0]}").endswith("late.csv")
    finally:
        watcher.stop.set()
        thread.join(5)
        for fd in watcher.pinned:
            os.close(fd)
        os.close(ifd)
    assert watcher.low_free is not None


def test_exhausted_keeps_the_lowest_sample_and_fails_closed(tmp_path: Path) -> None:
    watcher = _watcher(tmp_path)
    assert watcher.exhausted(0)  # never sampled
    watcher.sample()
    assert watcher.low_free is not None
    assert not watcher.exhausted(0)
    assert watcher.exhausted(watcher.low_free)
    watcher.low_inodes = 0
    assert watcher.exhausted(0)


def test_others_lists_every_other_pid_including_zombies(tmp_path: Path) -> None:
    for name in ("1", "7", "42", "self", "sys"):
        (tmp_path / name).mkdir()

    assert sorted(supervisor.others(str(tmp_path), me=1)) == [7, 42]


def test_signed_verdict_round_trips_through_the_server_parser() -> None:
    key = "ab" * 32
    envelope = supervisor.sign_verdict(
        key.encode(),
        exit_code=0,
        timed_out=False,
        quota_exhausted=True,
        min_free_bytes=4096,
        others_remaining=False,
    )

    verdict = output_quota.parse_verdict(envelope.decode(), key)
    assert verdict == output_quota.Verdict(
        exit_code=0,
        timed_out=False,
        quota_exhausted=True,
        min_free_bytes=4096,
        others_remaining=False,
    )
    assert output_quota.parse_verdict(envelope.decode(), "cd" * 32) is None


def test_write_verdict_replaces_a_planted_symlink_without_following_it(tmp_path: Path) -> None:
    target = tmp_path / "victim"
    target.write_text("untouched")
    verdict = tmp_path / "verdict.json"
    verdict.symlink_to(target)
    stale = tmp_path / f"verdict.json.{os.getpid()}"
    os.mkfifo(stale)  # would block a plain open; it is unlinked first

    supervisor.write_verdict(b"{}", str(verdict))

    assert not verdict.is_symlink()
    assert verdict.read_bytes() == b"{}"
    assert target.read_text() == "untouched"
    assert not stale.exists()


def test_write_verdict_fails_when_the_staging_name_is_a_directory(tmp_path: Path) -> None:
    verdict = tmp_path / "verdict.json"
    (tmp_path / f"verdict.json.{os.getpid()}").mkdir()

    with pytest.raises(OSError):
        supervisor.write_verdict(b"{}", str(verdict))
    assert not verdict.exists()
