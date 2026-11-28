"""C2: the shared child-process helpers used by the data plane and the execution backends."""

from __future__ import annotations

import subprocess
import sys
import threading
from io import BytesIO, StringIO
from pathlib import Path

import _processes
import pytest

from agentic_analytics.procutil import CHUNK_SIZE, drain_bounded, kill_tree


@pytest.mark.parametrize("stream", [BytesIO(b"z" * 300_000), StringIO("z" * 300_000)])
def test_drain_keeps_the_first_limit_and_reads_to_eof(stream: BytesIO | StringIO) -> None:
    chunks: list = []
    truncated = threading.Event()

    drain_bounded(stream, chunks, 1000, truncated)

    assert stream.closed
    assert len(chunks[0]) == 1000 and len(chunks) == 1
    assert truncated.is_set()


def test_drain_at_exactly_the_limit_is_not_truncated() -> None:
    chunks: list[bytes] = []
    truncated = threading.Event()

    drain_bounded(BytesIO(b"y" * (CHUNK_SIZE + 10)), chunks, CHUNK_SIZE + 10, truncated)

    assert b"".join(chunks) == b"y" * (CHUNK_SIZE + 10)
    assert not truncated.is_set()


def test_drain_without_an_event_still_caps() -> None:
    chunks: list[str] = []

    drain_bounded(StringIO("abcdef"), chunks, 4)

    assert "".join(chunks) == "abcd"


@pytest.mark.skipif(sys.platform != "linux", reason="reads /proc for the grandchild")
def test_kill_tree_reaches_the_grandchild(tmp_path: Path) -> None:
    started = tmp_path / "grandchild.pid"
    code = f"""
import os, subprocess, sys, time
from pathlib import Path
grand = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path({str(started)!r}).with_name("tmp").write_text(str(grand.pid))
os.replace(Path({str(started)!r}).with_name("tmp"), {str(started)!r})
time.sleep(60)
"""
    child = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        assert _processes.wait_for(started.exists, 30), "grandchild never started"
        grandchild = int(started.read_text())
        assert _processes.alive(grandchild)

        kill_tree(child)  # leader not reaped yet, so its group id is still ours
        child.wait(timeout=10)

        ended = _processes.wait_for(lambda: not _processes.alive(grandchild), 10)
        assert ended, _processes.describe(grandchild)
    finally:
        if child.poll() is None:  # only while the group id is still ours
            kill_tree(child)
            child.wait(timeout=10)
