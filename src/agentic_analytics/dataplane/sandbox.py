"""Privilege isolation for the data-plane worker (SEC-02, issue #21).

Two OS-level controls are applied inside the worker before DuckDB is imported or any source
byte is read. Both need no root, no setuid helper, and no container, so they hold wherever the
server itself runs:

* **Filesystem confinement (Linux, Landlock).** The worker restricts itself with Landlock
  (Linux 5.13+; ABI probed at run time). Afterwards it can read and execute only the Python
  runtime and system libraries, read only the authorized source files of the current operation,
  and create, write, or delete only inside its private scratch directory and the operation's own
  per-execution output directory. Everything else, including the workspace, the server state
  directory, other sessions' archives, and the server's own ``/proc`` entries, is denied by the
  kernel, independently of DuckDB's ``allowed_paths``. Where the kernel supports it, TCP
  bind/connect (ABI 4+) and signals or abstract Unix sockets reaching processes outside the
  sandbox (ABI 6+) are denied too. ``no_new_privs`` is set, so a setuid binary cannot regain
  privileges.
* **Parent-death binding.** On Linux the worker asks the kernel for ``SIGKILL`` when its parent
  exits (``PR_SET_PDEATHSIG``). Where that is unavailable (macOS, Windows, or a failed
  ``prctl``), a watchdog thread polls the parent instead (POSIX: ``getppid`` changes once the
  worker is orphaned; Windows: a handle wait on the parent process), so an orphaned worker
  exits within about a quarter second.

Neither control is available on macOS or Windows (no Landlock; Windows uses the watchdog only).
``data_plane_sandbox`` selects the behaviour: ``auto`` (default) enforces Landlock on Linux and
fails closed when the kernel lacks it, and runs without filesystem confinement elsewhere;
``required`` fails closed on every platform without Landlock; ``off`` disables confinement
explicitly. Parent-death binding is always on.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import sys
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# --- Landlock ABI constants (include/uapi/linux/landlock.h) -------------------------------------

# The Landlock system calls were added after the per-architecture tables were unified, so these
# numbers are the same on every Linux architecture.
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_PDEATHSIG = 1
_PR_SET_NO_NEW_PRIVS = 38

_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_CHAR = 1 << 6
_FS_MAKE_DIR = 1 << 7
_FS_MAKE_REG = 1 << 8
_FS_MAKE_SOCK = 1 << 9
_FS_MAKE_FIFO = 1 << 10
_FS_MAKE_BLOCK = 1 << 11
_FS_MAKE_SYM = 1 << 12
_FS_REFER = 1 << 13  # ABI 2
_FS_TRUNCATE = 1 << 14  # ABI 3
_FS_IOCTL_DEV = 1 << 15  # ABI 5
_NET_BIND_TCP = 1 << 0  # ABI 4
_NET_CONNECT_TCP = 1 << 1  # ABI 4
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0  # ABI 6
_SCOPE_SIGNAL = 1 << 1  # ABI 6

_FS_ABI1 = (1 << 13) - 1
_FILE_RIGHTS = _FS_EXECUTE | _FS_WRITE_FILE | _FS_READ_FILE | _FS_TRUNCATE | _FS_IOCTL_DEV
_READ_RIGHTS = _FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR

# Read-only system locations for shared libraries loaded lazily by the interpreter and DuckDB.
# The rest of /etc and /proc, /home, /tmp, and the workspace are deliberately absent.
_SYSTEM_READ_ROOTS = ("/usr", "/lib", "/lib64", "/lib32", "/libx32")
# Host facts DuckDB reads at start-up (CPU count, memory, cgroup limits). The worker's own
# /proc entry is granted by resolving /proc/self inside the worker, never the server's.
_SYSTEM_READ_DIRS = ("/proc/self", "/sys/fs/cgroup", "/sys/devices/system/cpu")
_SYSTEM_READ_FILES = (
    "/etc/ld.so.cache",
    "/etc/localtime",
    "/proc/stat",
    "/proc/meminfo",
    "/proc/sys/vm/overcommit_memory",
)

_PARENT_POLL_SECONDS = 0.25
_ORPHAN_EXIT_CODE = 137


class SandboxUnavailable(RuntimeError):
    """Filesystem confinement was required but the host cannot provide it."""


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """What one worker operation may touch. Built by the server; applied by the worker."""

    mode: str = "auto"
    read_files: tuple[str, ...] = ()
    write_dirs: tuple[str, ...] = ()
    read_dirs: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "read_files": list(self.read_files),
            "write_dirs": list(self.write_dirs),
            "read_dirs": list(self.read_dirs),
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> SandboxPolicy:
        return cls(
            mode=str(value.get("mode", "auto")),
            read_files=tuple(str(item) for item in value.get("read_files", ())),
            write_dirs=tuple(str(item) for item in value.get("write_dirs", ())),
            read_dirs=tuple(str(item) for item in value.get("read_dirs", ())),
        )


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def landlock_abi() -> int:
    """Return the kernel's Landlock ABI version, or 0 when Landlock is unavailable."""

    if not sys.platform.startswith("linux"):
        return 0
    try:
        libc = _libc()
        libc.syscall.restype = ctypes.c_long
        version = libc.syscall(
            ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET),
            None,
            ctypes.c_size_t(0),
            ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
        )
    except (OSError, AttributeError):  # pragma: no cover - exotic libc
        return 0
    return int(version) if version > 0 else 0


def runtime_read_roots() -> list[str]:
    """Directories the interpreter needs to keep importing modules and loading libraries."""

    roots: list[str] = [*_SYSTEM_READ_ROOTS, sys.prefix, sys.base_prefix, sys.exec_prefix]
    roots += [entry for entry in sys.path if entry]
    return roots


class _RulesetAttr(ctypes.Structure):
    _fields_ = (
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    )


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))


def _check(result: int, what: str) -> int:
    if result < 0:
        err = ctypes.get_errno()
        raise OSError(err, f"landlock {what} failed: {os.strerror(err)}")
    return result


def _handled_fs(abi: int) -> int:
    rights = _FS_ABI1
    if abi >= 2:
        rights |= _FS_REFER
    if abi >= 3:
        rights |= _FS_TRUNCATE
    if abi >= 5:
        rights |= _FS_IOCTL_DEV
    return rights


def _existing(paths: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for path in paths:
        resolved = os.path.realpath(path)
        if os.path.exists(resolved):
            seen.setdefault(resolved, None)
    return list(seen)


def _apply_landlock(policy: SandboxPolicy, abi: int) -> None:
    libc = _libc()
    libc.syscall.restype = ctypes.c_long
    handled_fs = _handled_fs(abi)
    attr = _RulesetAttr(handled_fs, 0, 0)
    size = 8
    if abi >= 4:
        attr.handled_access_net = _NET_BIND_TCP | _NET_CONNECT_TCP
        size = 16
    if abi >= 6:
        attr.scoped = _SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL
        size = 24
    ruleset = _check(
        libc.syscall(
            ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET),
            ctypes.byref(attr),
            ctypes.c_size_t(size),
            ctypes.c_uint32(0),
        ),
        "create_ruleset",
    )
    try:

        def allow(path: str, rights: int) -> None:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                if not os.path.isdir(path):
                    rights &= _FILE_RIGHTS
                rule = _PathBeneathAttr(rights & handled_fs, fd)
                _check(
                    libc.syscall(
                        ctypes.c_long(_SYS_LANDLOCK_ADD_RULE),
                        ctypes.c_int(ruleset),
                        ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
                        ctypes.byref(rule),
                        ctypes.c_uint32(0),
                    ),
                    f"add_rule({path})",
                )
            finally:
                os.close(fd)

        for path in _existing([*runtime_read_roots(), *_SYSTEM_READ_DIRS, *policy.read_dirs]):
            allow(path, _READ_RIGHTS)
        for path in _existing([*_SYSTEM_READ_FILES, *policy.read_files]):
            allow(path, _FS_READ_FILE)
        for path in _existing(["/dev/null"]):
            allow(path, _FS_READ_FILE | _FS_WRITE_FILE)
        for path in _existing(["/dev/urandom"]):
            allow(path, _FS_READ_FILE)
        for path in _existing(policy.write_dirs):
            allow(path, handled_fs)
        _check(libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "prctl(no_new_privs)")
        _check(
            libc.syscall(
                ctypes.c_long(_SYS_LANDLOCK_RESTRICT_SELF),
                ctypes.c_int(ruleset),
                ctypes.c_uint32(0),
            ),
            "restrict_self",
        )
    finally:
        os.close(ruleset)


def enter(policy: SandboxPolicy) -> str:
    """Confine the calling process; return the isolation level actually applied.

    Must run before any other thread exists: Landlock restricts the calling thread and every
    thread or process created after it, but not threads that already exist.
    """

    if policy.mode == "off":
        return "off"
    abi = landlock_abi()
    if abi <= 0:
        if policy.mode == "required" or sys.platform.startswith("linux"):
            raise SandboxUnavailable(
                "data-plane filesystem confinement needs Linux Landlock (kernel 5.13+ with "
                "the landlock LSM enabled), which this host does not provide; set "
                "AGENTIC_ANALYTICS_DATA_PLANE_SANDBOX=off to run without it"
            )
        return "unavailable"
    if threading.active_count() != 1:
        raise SandboxUnavailable("sandbox must be entered before any worker thread starts")
    _apply_landlock(policy, abi)
    return f"landlock-abi-{abi}"


# --- parent-death binding -----------------------------------------------------------------------


def _orphaned() -> None:
    os._exit(_ORPHAN_EXIT_CODE)


def _watch_posix(parent_pid: int) -> None:
    while True:
        if os.getppid() != parent_pid:
            _orphaned()
        time.sleep(_PARENT_POLL_SECONDS)


def _watch_windows(parent_pid: int) -> None:  # pragma: no cover - exercised on Windows only
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    synchronize = 0x00100000
    handle = kernel32.OpenProcess(synchronize, False, parent_pid)
    if not handle:
        _orphaned()
    infinite = 0xFFFFFFFF
    kernel32.WaitForSingleObject(handle, infinite)
    _orphaned()


def bind_to_parent(parent_pid: int | None, *, pdeathsig: bool = True) -> bool:
    """Make this process exit when ``parent_pid`` exits (see the module docstring).

    Returns True when the kernel death signal is armed, so no watchdog thread is needed.
    """

    if not parent_pid or parent_pid <= 0:
        return False
    armed = False
    if os.name == "posix":
        if pdeathsig and sys.platform.startswith("linux"):
            with contextlib.suppress(OSError, AttributeError):
                armed = _libc().prctl(_PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0) == 0
        # The parent may already have died before the death signal was armed.
        if os.getppid() != parent_pid:
            _orphaned()
    return armed


def start_parent_watchdog(parent_pid: int | None) -> None:
    """Start the portable fallback watchdog. Call after :func:`enter`, so it runs confined."""

    if not parent_pid or parent_pid <= 0:
        return
    target = _watch_windows if os.name == "nt" else _watch_posix
    # A small stack: the worker's address-space limit is budgeted for DuckDB, not for this.
    previous = threading.stack_size(256 * 1024)
    try:
        threading.Thread(
            target=target, args=(parent_pid,), name="parent-watchdog", daemon=True
        ).start()
    finally:
        threading.stack_size(previous)
