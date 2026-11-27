"""Managed-execution supervisor: PID 1 inside the Docker exec container (SEC-01).

This file runs inside the container, not on the server. The Docker backend copies its source
next to the request script and bind-mounts it read-only at ``SUPERVISOR_PATH``; the container
then runs ``python <SUPERVISOR_PATH> <quota threshold> <timeout seconds>``. Only this one file
is mounted, so it must import nothing but the standard library. The protocol it implements
(per-run key on stdin, inotify watch and file pinning, ``kill(-1)`` sweep, HMAC verdict) is
described in :mod:`agentic_analytics.execution_backends.output_quota`.

The paths below must match ``CONTAINER_OUTPUT_DIR`` and ``VERDICT_PATH`` in ``output_quota``
and the request mount in the Docker backend; a unit test checks that they do.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import hmac
import json
import os
import resource
import select
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable

OUTPUT_DIR = "/workspace/outputs"
VERDICT_PATH = "/tmp/.agentic-analytics-verdict.json"
REQUEST_PATH = "/run/agentic-analytics/request.py"
PIN_LIMIT = 20000
FD_RESERVE = 64  # descriptors never used for pinning: /proc scans and the verdict need some
KILL_SECONDS = 10.0
SAMPLE_SECONDS = 0.005
CHILD_POLL_SECONDS = 0.02
PR_SET_DUMPABLE = 4
IN_MODIFY, IN_MOVED_TO, IN_CREATE = 0x2, 0x80, 0x100
_EVENT = struct.Struct("iIII")  # struct inotify_event without its trailing name


def parse_events(data: bytes) -> list[tuple[int, int, str]]:
    """Split a read from an inotify descriptor into ``(watch descriptor, mask, name)``."""

    events: list[tuple[int, int, str]] = []
    offset = 0
    while offset + _EVENT.size <= len(data):
        wd, mask, _cookie, size = _EVENT.unpack_from(data, offset)
        start = offset + _EVENT.size
        name = data[start : start + size].rstrip(b"\0").decode(errors="replace")
        offset = start + size
        events.append((wd, mask, name))
    return events


def nofile_target(soft: int, hard: int) -> int | None:
    """Soft ``RLIMIT_NOFILE`` to ask for so ``PIN_LIMIT`` files can be pinned, or None."""

    unlimited = resource.RLIM_INFINITY
    if soft == unlimited:
        return None
    want = PIN_LIMIT + 256 if hard == unlimited else min(hard, PIN_LIMIT + 256)
    return want if want > soft else None


def pin_cap_for(soft: int) -> int:
    """How many output files may be pinned under a soft descriptor limit of ``soft``."""

    if soft == resource.RLIM_INFINITY:
        return PIN_LIMIT
    return max(0, min(PIN_LIMIT, soft - FD_RESERVE))


def raise_pin_cap() -> int:
    """Raise the soft descriptor limit if allowed; return the resulting pin capacity.

    Called after the script has started, so the script keeps the original soft limit.
    """

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = nofile_target(soft, hard)
    if target is not None:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            soft = target
    return pin_cap_for(soft)


class OutputWatcher:
    """Tracks the lowest free space and inodes seen under ``root`` and pins new files.

    A pinned file keeps its pages allocated after the script deletes it, so deleting a file
    does not return quota during the run.
    """

    def __init__(
        self, root: str, inotify_fd: int, add_watch: Callable[[int, bytes, int], int]
    ) -> None:
        self.root = root
        self.inotify_fd = inotify_fd
        self._add_watch = add_watch
        self.low_free: int | None = None
        self.low_inodes: int | None = None
        self.pin_cap = 0
        self.pinned: list[int] = []
        self.watches: dict[int, str] = {}
        self.stop = threading.Event()

    def sample(self) -> None:
        st = os.statvfs(self.root)
        free = st.f_bavail * st.f_frsize
        self.low_free = free if self.low_free is None else min(self.low_free, free)
        if st.f_files:
            inodes = st.f_favail
            self.low_inodes = inodes if self.low_inodes is None else min(self.low_inodes, inodes)

    def track(self, path: str) -> None:
        try:
            mode = os.lstat(path).st_mode
        except OSError:
            return
        if stat.S_ISDIR(mode):
            self.watch_dir(path)
        elif stat.S_ISREG(mode) and len(self.pinned) < self.pin_cap:
            with contextlib.suppress(OSError):
                self.pinned.append(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC))

    def watch_dir(self, path: str) -> None:
        if self.inotify_fd >= 0:
            mask = IN_MODIFY | IN_CREATE | IN_MOVED_TO
            wd = int(self._add_watch(self.inotify_fd, path.encode(), mask))
            if wd >= 0:
                self.watches[wd] = path
        with contextlib.suppress(OSError):
            for entry in os.scandir(path):
                self.track(entry.path)

    def handle(self, data: bytes) -> None:
        for wd, mask, name in parse_events(data):
            parent = self.watches.get(wd)
            if parent and name and mask & (IN_CREATE | IN_MOVED_TO):
                self.track(os.path.join(parent, name))

    def run(self) -> None:
        """Sample on every inotify event, and every ``SAMPLE_SECONDS`` as a fallback."""

        while not self.stop.is_set():
            if self.inotify_fd >= 0:
                ready = select.select([self.inotify_fd], [], [], SAMPLE_SECONDS)[0]
            else:
                time.sleep(SAMPLE_SECONDS)
                ready = []
            self.sample()
            if not ready:
                continue
            try:
                data = os.read(self.inotify_fd, 65536)
            except BlockingIOError:
                continue
            self.handle(data)
            self.sample()

    def exhausted(self, threshold: int) -> bool:
        """True when free space ever fell to ``threshold`` or free inodes reached zero.

        A run that was never sampled counts as exhausted, so it fails closed.
        """

        return self.low_free is None or self.low_free <= threshold or self.low_inodes == 0


def others(proc: str = "/proc", me: int | None = None) -> list[int]:
    """Every other process listed in ``proc``.

    Zombies count as alive. A thread-group leader that called ``SYS_exit`` shows state Z
    while its other threads keep running, so state proves nothing; a real zombie disappears
    once it is reaped.
    """

    me = os.getpid() if me is None else me
    return [int(entry) for entry in os.listdir(proc) if entry.isdigit() and int(entry) != me]


def reap() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def kill_others() -> bool:  # pragma: no cover - only safe as PID 1 in the exec container
    """SIGKILL every other process; True once ``/proc`` lists nothing but this one.

    ``kill(-1)`` from PID 1 signals every process in the PID namespace in one call, so a
    fork cannot slip between a scan and a signal. Orphans are reparented to this process and
    reaped here.
    """

    end = time.monotonic() + KILL_SECONDS
    while True:
        with contextlib.suppress(OSError):
            os.kill(-1, signal.SIGKILL)
        reap()
        try:
            if not others():
                return True
        except OSError:
            pass
        if time.monotonic() >= end:
            return False
        time.sleep(0.005)


def sign_verdict(
    key: bytes,
    *,
    exit_code: int | None,
    timed_out: bool,
    quota_exhausted: bool,
    min_free_bytes: int | None,
    others_remaining: bool,
) -> bytes:
    """The verdict envelope the server checks with ``output_quota.parse_verdict``."""

    body = json.dumps(
        {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "quota_exhausted": quota_exhausted,
            "min_free_bytes": min_free_bytes,
            "others_remaining": others_remaining,
        },
        sort_keys=True,
    )
    mac = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    return json.dumps({"body": body, "mac": mac}).encode()


def write_verdict(payload: bytes, path: str = VERDICT_PATH) -> None:
    """Write ``payload`` to ``path`` atomically, refusing anything but a new regular file.

    ``/tmp`` is writable by the script, which may have left anything at these names. Every
    other process is dead by now, so after the unlink nothing can recreate the staging name;
    ``O_EXCL``, ``O_NONBLOCK`` and the ``fstat`` make a FIFO, link or directory fail at once.
    """

    staging = path + "." + str(os.getpid())
    with contextlib.suppress(FileNotFoundError):
        os.unlink(staging)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(staging, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("verdict staging file is not a regular file")
        os.write(fd, payload)
    finally:
        os.close(fd)
    os.replace(staging, path)  # atomic: the server never reads a half-written verdict


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - runs in the container
    args = sys.argv[1:] if argv is None else argv
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)  # /proc/1/{mem,fd,environ} become root-only
    key = sys.stdin.buffer.readline().strip()
    threshold = int(args[0])
    deadline = time.monotonic() + float(args[1])
    watcher = OutputWatcher(
        OUTPUT_DIR, int(libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)), libc.inotify_add_watch
    )
    stdin_closed = threading.Event()

    def watch_stdin() -> None:
        sys.stdin.buffer.read()
        stdin_closed.set()

    threading.Thread(target=watch_stdin, daemon=True).start()
    watcher.sample()
    watcher.watch_dir(OUTPUT_DIR)
    child = subprocess.Popen(
        [sys.executable, REQUEST_PATH], stdin=subprocess.DEVNULL, start_new_session=True
    )
    with contextlib.suppress(ValueError, OSError):
        watcher.pin_cap = raise_pin_cap()
    monitor = threading.Thread(target=watcher.run, daemon=True)
    monitor.start()
    timed_out = False
    while child.poll() is None:
        if time.monotonic() >= deadline or stdin_closed.is_set():
            timed_out = True
            break
        time.sleep(CHILD_POLL_SECONDS)
    clean = kill_others()  # every process except this one, before anything is judged
    code = child.poll()
    watcher.stop.set()
    monitor.join()
    watcher.sample()
    envelope = sign_verdict(
        key,
        exit_code=code,
        timed_out=timed_out,
        quota_exhausted=watcher.exhausted(threshold),
        min_free_bytes=watcher.low_free,
        others_remaining=not clean,
    )
    try:
        write_verdict(envelope)
    except OSError:
        os._exit(3)  # no verdict: the server fails the run closed once the container is gone
    stdin_closed.wait()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
