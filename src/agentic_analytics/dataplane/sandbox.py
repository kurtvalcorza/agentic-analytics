"""Privilege isolation for the data-plane worker (SEC-02, issue #21).

OS-level controls are applied inside the worker before DuckDB is imported or any source byte
is read. None needs root, a setuid helper, or a container, so they hold wherever the server
itself runs. ``no_new_privs`` is set first, so a setuid binary cannot regain privileges.

* **Filesystem confinement (Linux, Landlock).** The worker restricts itself with Landlock
  (Linux 5.13+; ABI probed at run time). Afterwards it can read only the Python runtime and
  system libraries, the authorized source files of the current operation, and a few host-fact
  files, and create, write, or delete only regular files and directories inside its private
  scratch directory and the operation's own per-execution output directory. Nothing may be
  executed. Everything else, including the workspace, the server state directory, other
  sessions' archives, and the server's own ``/proc`` entries, is denied by the kernel,
  independently of DuckDB's ``allowed_paths``. Where the kernel supports it, TCP bind/connect
  (ABI 4+) and signals or abstract Unix sockets reaching processes outside the sandbox (ABI 6+)
  are denied too. A runtime read location that covers a protected root fails closed.
* **Syscall filter (Linux x86_64/aarch64, seccomp-bpf).** Landlock does not cover pathname Unix
  sockets (``docker.sock``), UDP, or other address families. The filter refuses every socket,
  ``execve``, process creation other than threads, ptrace-style access, io_uring, and signals to
  any pid but the worker's own, and locks ``PR_SET_PDEATHSIG``. See ``seccomp_program``.
* **Parent-death binding.** On Linux the worker asks the kernel for ``SIGKILL`` when its parent
  thread exits (``PR_SET_PDEATHSIG``). Where that is unavailable (macOS, Windows, or a failed
  ``prctl``), a watchdog thread polls the parent instead (POSIX: ``getppid`` changes once the
  worker is orphaned; Windows: a handle wait on the parent process), so an orphaned worker
  exits within about a quarter second.

Landlock and seccomp are not available on macOS or Windows (Windows uses the watchdog only).
``data_plane_sandbox`` selects the behaviour: ``auto`` (default) enforces both on Linux and
fails closed when the kernel lacks either, and runs without confinement elsewhere; ``required``
fails closed on every platform without them; ``off`` disables confinement explicitly.
Parent-death binding is always on.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import signal
import stat
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
# No EXECUTE anywhere: the worker never runs a program (Landlock EXECUTE gates execve, not the
# mmap of shared libraries), and seccomp denies execve as well.
_READ_RIGHTS = _FS_READ_FILE | _FS_READ_DIR
# Write directories get regular files and directories only: no device nodes, sockets, FIFOs,
# symlinks, cross-directory renames or links (REFER), or execution.
_WRITE_RIGHTS = (
    _FS_READ_FILE
    | _FS_READ_DIR
    | _FS_WRITE_FILE
    | _FS_TRUNCATE
    | _FS_MAKE_REG
    | _FS_MAKE_DIR
    | _FS_REMOVE_FILE
    | _FS_REMOVE_DIR
)

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
    # Directories no read or write grant may cover: the workspace roots, the server state
    # directory, and the archive root. enter() fails closed if a grant is equal to or an
    # ancestor of one, e.g. a workspace under /usr/src/app or state inside the venv prefix.
    protected_roots: tuple[str, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "read_files": list(self.read_files),
            "write_dirs": list(self.write_dirs),
            "read_dirs": list(self.read_dirs),
            "protected_roots": list(self.protected_roots),
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> SandboxPolicy:
        return cls(
            mode=str(value.get("mode", "auto")),
            read_files=tuple(str(item) for item in value.get("read_files", ())),
            write_dirs=tuple(str(item) for item in value.get("write_dirs", ())),
            read_dirs=tuple(str(item) for item in value.get("read_dirs", ())),
            protected_roots=tuple(str(item) for item in value.get("protected_roots", ())),
        )


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _prctl(option: int, arg2: int = 0, arg3: int = 0, arg4: int = 0, arg5: int = 0) -> int:
    """prctl(2) with every argument passed as a full-width unsigned long."""

    libc = _libc()
    libc.prctl.restype = ctypes.c_int
    libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
    return int(libc.prctl(option, arg2, arg3, arg4, arg5))


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
    """Resolve the runtime's own locations. Never used for source files (see below)."""

    seen: dict[str, None] = {}
    for path in paths:
        resolved = os.path.realpath(path)
        if os.path.exists(resolved):
            seen.setdefault(resolved, None)
    return list(seen)


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root if root.endswith("/") else root + "/")


def check_protected_roots(grants: Iterable[str], protected_roots: Iterable[str]) -> None:
    """Fail closed when a grant is equal to or an ancestor of a protected root.

    Grants must already be resolved. A grant *below* a protected root (the venv inside the
    workspace, the scratch directory inside the state directory) exposes only itself.
    """

    protected = [os.path.realpath(root) for root in protected_roots]
    for grant in grants:
        for root in protected:
            if _within(root, grant):
                raise SandboxUnavailable("a sandbox grant covers a protected directory")


def _open_source_file(path: str) -> int | None:
    """``O_PATH`` fd for an authorized source file, without following a final symlink.

    The server resolved and fingerprinted the path. A symlink or non-regular file in its place
    now means it was swapped after that check, so the worker refuses to start.
    """

    try:
        fd = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None  # nothing to grant; the operation itself reports the missing source
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise SandboxUnavailable("an authorized source file is not a regular file")
    return fd


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
    read_dirs = _existing([*runtime_read_roots(), *_SYSTEM_READ_DIRS, *policy.read_dirs])
    write_dirs = _existing(policy.write_dirs)
    check_protected_roots([*read_dirs, *write_dirs], policy.protected_roots)
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

        def allow_fd(fd: int, rights: int) -> None:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
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
                "add_rule",
            )

        def allow(path: str, rights: int) -> None:
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                allow_fd(fd, rights)
            finally:
                os.close(fd)

        for path in read_dirs:
            allow(path, _READ_RIGHTS)
        for path in _existing(_SYSTEM_READ_FILES):
            allow(path, _FS_READ_FILE)
        for path in policy.read_files:
            source_fd = _open_source_file(path)
            if source_fd is not None:
                try:
                    allow_fd(source_fd, _FS_READ_FILE)
                finally:
                    os.close(source_fd)
        for path in _existing(["/dev/null"]):
            allow(path, _FS_READ_FILE | _FS_WRITE_FILE)
        for path in _existing(["/dev/urandom"]):
            allow(path, _FS_READ_FILE)
        for path in write_dirs:
            allow(path, _WRITE_RIGHTS)
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


# --- seccomp-bpf --------------------------------------------------------------------------------
#
# Landlock confines the filesystem (plus TCP at ABI 4+ and signal/abstract-socket scoping at ABI
# 6+) but not pathname Unix sockets, UDP, or other address families, and it cannot stop a child
# process that does not inherit the death signal. The worker talks to the server only over its
# three inherited pipes and never runs a program, so a seccomp filter removes all of that
# outright, on every Landlock ABI.

_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_SECCOMP_RET_KILL_PROCESS = 0x80000000
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000
_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_JGE_K = 0x35
_BPF_JSET_K = 0x45
_BPF_RET_K = 0x06
_SECCOMP_DATA_NR = 0
_SECCOMP_DATA_ARCH = 4
_SECCOMP_DATA_ARG0_LOW = 16  # little-endian: the low 32 bits of args[0]
_X32_SYSCALL_BIT = 0x40000000
_CLONE_THREAD = 0x00010000

# machine -> (AUDIT_ARCH_*, syscall numbers). Any other Linux architecture fails closed.
_SECCOMP_ARCHES: dict[str, tuple[int, dict[str, int]]] = {
    "x86_64": (
        0xC000003E,
        {
            "socket": 41, "connect": 42, "accept": 43, "sendto": 44, "recvfrom": 45,
            "sendmsg": 46, "recvmsg": 47, "bind": 49, "listen": 50, "socketpair": 53,
            "clone": 56, "fork": 57, "vfork": 58, "execve": 59, "kill": 62, "ptrace": 101,
            "rt_sigqueueinfo": 129, "prctl": 157, "tkill": 200, "tgkill": 234,
            "unshare": 272, "accept4": 288, "rt_tgsigqueueinfo": 297, "perf_event_open": 298,
            "recvmmsg": 299, "sendmmsg": 307, "setns": 308, "process_vm_readv": 310,
            "process_vm_writev": 311, "bpf": 321, "execveat": 322, "userfaultfd": 323,
            "pidfd_send_signal": 424, "io_uring_setup": 425, "io_uring_enter": 426,
            "io_uring_register": 427, "pidfd_open": 434, "clone3": 435, "pidfd_getfd": 438,
        },
    ),
    "aarch64": (
        0xC00000B7,
        {
            "unshare": 97, "ptrace": 117, "kill": 129, "tkill": 130, "tgkill": 131,
            "rt_sigqueueinfo": 138, "prctl": 167, "socket": 198, "socketpair": 199,
            "bind": 200, "listen": 201, "accept": 202, "connect": 203, "sendto": 206,
            "recvfrom": 207, "sendmsg": 211, "recvmsg": 212, "clone": 220, "execve": 221,
            "rt_tgsigqueueinfo": 240, "perf_event_open": 241, "accept4": 242,
            "recvmmsg": 243, "setns": 268, "sendmmsg": 269, "process_vm_readv": 270,
            "process_vm_writev": 271, "bpf": 280, "execveat": 281, "userfaultfd": 282,
            "pidfd_send_signal": 424, "io_uring_setup": 425, "io_uring_enter": 426,
            "io_uring_register": 427, "pidfd_open": 434, "clone3": 435, "pidfd_getfd": 438,
        },
    ),
}  # fmt: skip
# Denied with EPERM. No socket of any address family can be created and the socket calls are
# refused as well, so nothing reaches the network, a pathname Unix socket (docker.sock), or
# another local service. io_uring is denied because it performs socket and file operations
# without entering these syscalls.
_SECCOMP_DENY = (
    "socket", "socketpair", "connect", "bind", "listen", "accept", "accept4", "sendto",
    "sendmsg", "sendmmsg", "recvfrom", "recvmsg", "recvmmsg",
    "execve", "execveat", "fork", "vfork",
    "ptrace", "process_vm_readv", "process_vm_writev", "tkill",
    "pidfd_open", "pidfd_getfd", "pidfd_send_signal",
    "io_uring_setup", "io_uring_enter", "io_uring_register",
    "unshare", "setns", "bpf", "perf_event_open", "userfaultfd",
)  # fmt: skip
# Allowed only when args[0] is the worker's own pid (glibc's raise() and abort() use tgkill).
_SECCOMP_SELF_ONLY = ("kill", "tgkill", "rt_sigqueueinfo", "rt_tgsigqueueinfo")


def _bpf(code: int, k: int, jt: int = 0, jf: int = 0) -> tuple[int, int, int, int]:
    return (code, jt, jf, k)


def seccomp_program(machine: str, pid: int) -> list[tuple[int, int, int, int]]:
    """The worker's seccomp-bpf program as ``(code, jt, jf, k)`` instructions."""

    if machine not in _SECCOMP_ARCHES:
        raise SandboxUnavailable("no seccomp syscall table for this architecture")
    audit_arch, numbers = _SECCOMP_ARCHES[machine]
    eperm = _SECCOMP_RET_ERRNO | errno.EPERM
    prog = [
        _bpf(_BPF_LD_W_ABS, _SECCOMP_DATA_ARCH),
        _bpf(_BPF_JEQ_K, audit_arch, jt=1),
        _bpf(_BPF_RET_K, _SECCOMP_RET_KILL_PROCESS),  # a foreign ABI: i386, arm32
        _bpf(_BPF_LD_W_ABS, _SECCOMP_DATA_NR),
    ]
    if machine == "x86_64":  # x32 shares AUDIT_ARCH_X86_64 and is marked by a number bit
        prog += [
            _bpf(_BPF_JGE_K, _X32_SYSCALL_BIT, jf=1),
            _bpf(_BPF_RET_K, _SECCOMP_RET_KILL_PROCESS),
        ]
    for name in _SECCOMP_DENY:
        if name in numbers:  # fork and vfork exist on x86_64 only
            prog += [_bpf(_BPF_JEQ_K, numbers[name], jf=1), _bpf(_BPF_RET_K, eperm)]
    # clone3 keeps its flags in memory a filter cannot read; ENOSYS makes glibc fall back to
    # clone(2), whose flags are checked below.
    prog += [
        _bpf(_BPF_JEQ_K, numbers["clone3"], jf=1),
        _bpf(_BPF_RET_K, _SECCOMP_RET_ERRNO | errno.ENOSYS),
    ]
    for name in _SECCOMP_SELF_ONLY:
        prog += [
            _bpf(_BPF_JEQ_K, numbers[name], jf=4),
            _bpf(_BPF_LD_W_ABS, _SECCOMP_DATA_ARG0_LOW),
            _bpf(_BPF_JEQ_K, pid & 0xFFFFFFFF, jf=1),
            _bpf(_BPF_RET_K, _SECCOMP_RET_ALLOW),
            _bpf(_BPF_RET_K, eperm),
        ]
    prog += [
        # clone(2) for threads only: a child process would not inherit the death signal.
        _bpf(_BPF_JEQ_K, numbers["clone"], jf=4),
        _bpf(_BPF_LD_W_ABS, _SECCOMP_DATA_ARG0_LOW),
        _bpf(_BPF_JSET_K, _CLONE_THREAD, jf=1),
        _bpf(_BPF_RET_K, _SECCOMP_RET_ALLOW),
        _bpf(_BPF_RET_K, eperm),
        # The death signal armed before the filter can no longer be changed or cleared.
        _bpf(_BPF_JEQ_K, numbers["prctl"], jf=4),
        _bpf(_BPF_LD_W_ABS, _SECCOMP_DATA_ARG0_LOW),
        _bpf(_BPF_JEQ_K, _PR_SET_PDEATHSIG, jf=1),
        _bpf(_BPF_RET_K, eperm),
        _bpf(_BPF_RET_K, _SECCOMP_RET_ALLOW),
        _bpf(_BPF_RET_K, _SECCOMP_RET_ALLOW),
    ]
    return prog


class _SockFilter(ctypes.Structure):
    _fields_ = (
        ("code", ctypes.c_uint16),
        ("jt", ctypes.c_uint8),
        ("jf", ctypes.c_uint8),
        ("k", ctypes.c_uint32),
    )


class _SockFprog(ctypes.Structure):
    _fields_ = (("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter)))


def _apply_seccomp() -> None:
    if sys.byteorder != "little":
        raise SandboxUnavailable("the seccomp filter supports little-endian hosts only")
    program = seccomp_program(os.uname().machine, os.getpid())
    filters = (_SockFilter * len(program))(*(_SockFilter(*insn) for insn in program))
    fprog = _SockFprog(len(program), ctypes.cast(filters, ctypes.POINTER(_SockFilter)))
    address = ctypes.cast(ctypes.pointer(fprog), ctypes.c_void_p).value or 0
    if _prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, address) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"seccomp filter install failed: {os.strerror(err)}")


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
    # no_new_privs first: Landlock and an unprivileged seccomp filter both require it, and it
    # keeps a setuid binary from regaining privileges.
    _check(_prctl(_PR_SET_NO_NEW_PRIVS, 1), "prctl(no_new_privs)")
    _apply_landlock(policy, abi)
    # Then seccomp, still before DuckDB is imported. Any failure fails the operation closed.
    _apply_seccomp()
    return f"landlock-abi-{abi}+seccomp"


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
                armed = _prctl(_PR_SET_PDEATHSIG, int(signal.SIGKILL)) == 0
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
