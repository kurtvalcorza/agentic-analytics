"""SEC-02 / #21: the data-plane worker is privilege-isolated and cannot outlive the server.

The confinement tests simulate a compromised worker (for example a DuckDB remote-code-execution
flaw): attacker code replaces a worker operation and runs *after* the real worker entry point
has set itself up, then tries to escape. DuckDB's own ``allowed_paths`` cannot help here, because
the attacker opens its own unrestricted connection and plain Python file handles.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import resource
import signal
import socket
import struct
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import _processes
import pytest

from agentic_analytics.dataplane import DataPlaneClient, DataPlaneError
from agentic_analytics.dataplane.sandbox import landlock_abi
from agentic_analytics.settings import Settings

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Landlock confinement is Linux-only"
)
posix_only = pytest.mark.skipif(os.name != "posix", reason="kills a POSIX parent process")
# Confinement assertions need the kernel feature; tests/conftest.py explains the skip once.
requires_confinement = pytest.mark.skipif(
    not sys.platform.startswith("linux") or landlock_abi() <= 0,
    reason="needs Linux Landlock; see the session notice from tests/conftest.py",
)

# Runs as the worker program: installs the attacker operation, then hands control to the real
# worker entry point, which applies limits and isolation before dispatching to the operation.
_COMPROMISED_WORKER = """
import ctypes, errno, fcntl, json, os, resource, runpy, signal, socket, sys
import agentic_analytics.dataplane.engine as engine
import agentic_analytics.dataplane.sandbox as sandbox

def attack(request):
    import duckdb
    targets = request["attack"]
    outcome = {}

    def attempt(name, action):
        try:
            action()
            outcome[name] = "allowed"
        except Exception as exc:
            outcome[name] = "denied:" + type(exc).__name__

    def write_outside():
        with open(targets["outside_file"], "w") as handle:
            handle.write("escaped")

    def overwrite_source():
        with open(targets["source"], "w") as handle:
            handle.write("tampered")

    def scratch_write():
        with open("scratch-ok.txt", "w") as handle:
            handle.write("ok")

    unrestricted = duckdb.connect()  # no allowed_paths: models a bypass of DuckDB's own sandbox
    attempt("python_write_outside", write_outside)
    attempt("python_overwrite_source", overwrite_source)
    attempt("python_read_secret", lambda: open(targets["secret"]).read())
    attempt(
        "duckdb_copy_to_elsewhere",
        lambda: unrestricted.execute(
            "COPY (SELECT 1 AS x) TO '" + targets["copy_target"] + "' (FORMAT CSV)"
        ),
    )
    attempt(
        "duckdb_read_disallowed",
        lambda: unrestricted.execute(
            "SELECT * FROM read_csv('" + targets["secret"] + "')"
        ).fetchall(),
    )
    attempt("python_read_source", lambda: open(targets["source"]).read())
    attempt("scratch_write", scratch_write)

    # --- escapes that do not go through the filesystem namespace Landlock covers ---------
    def unix_connect():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(targets["unix_socket"])  # stand-in for /var/run/docker.sock
            sock.sendall(b"GET /_ping HTTP/1.0\\r\\n\\r\\n")

    def udp_send():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(b"exfiltrated", ("127.0.0.1", int(targets["udp_port"])))

    def tcp_connect():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            sock.connect(("127.0.0.1", int(targets["tcp_port"])))

    def socketpair():
        left, right = socket.socketpair()
        left.close()
        right.close()

    def fork():
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)

    def clear_death_signal():
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
        if libc.prctl(1, 0, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            raise OSError(ctypes.get_errno(), "prctl")

    def symlink_plant():
        os.symlink(targets["secret"], "planted-link")
        open("planted-link").read()

    attempt("unix_connect", unix_connect)
    attempt("udp_send", udp_send)
    attempt("tcp_connect", tcp_connect)
    attempt("socketpair", socketpair)
    attempt("signal_server", lambda: os.kill(int(targets["server_pid"]), 0))
    attempt(
        "read_server_proc",
        lambda: open("/proc/" + targets["server_pid"] + "/environ", "rb").read(),
    )
    attempt("symlink_plant", symlink_plant)
    attempt("hardlink_secret", lambda: os.link(targets["secret"], "hardlinked"))
    attempt("fork", fork)
    attempt("clear_death_signal", clear_death_signal)

    # --- calls Landlock does not mediate (#26): record the errno of each ------------------
    numbers = sandbox._SECCOMP_ARCHES[os.uname().machine][1]
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    def raw(name, *args):
        values = [ctypes.c_long(arg) if isinstance(arg, int) else arg for arg in args]
        if libc.syscall(ctypes.c_long(numbers[name]), *values) < 0:
            raise OSError(ctypes.get_errno(), name)

    def call(name, action):
        try:
            action()
            outcome[name] = "allowed"
        except OSError as exc:
            outcome[name] = errno.errorcode.get(exc.errno, str(exc.errno))

    victim = int(targets["victim_pid"])
    metadata = targets["metadata_file"]
    null_fd = os.open("/dev/null", os.O_RDONLY)
    scratch_fd = os.open("scratch-ok.txt", os.O_RDONLY)
    absent_key = 0x26AA0026  # no IPC object has this key; without IPC_CREAT none is made
    call("sys_prlimit64", lambda: resource.prlimit(victim, resource.RLIMIT_NOFILE, (7, 7)))
    call("sys_sched_setaffinity", lambda: os.sched_setaffinity(victim, {0}))
    call(
        "sys_sched_setscheduler",
        lambda: os.sched_setscheduler(victim, os.SCHED_BATCH, os.sched_param(0)),
    )
    call("sys_sched_setparam", lambda: os.sched_setparam(victim, os.sched_param(0)))
    call("sys_sched_setattr", lambda: raw("sched_setattr", victim, 0, 0))
    call("sys_setpriority", lambda: os.setpriority(os.PRIO_PROCESS, victim, 5))
    call("sys_ioprio_set", lambda: raw("ioprio_set", 1, victim, 0))
    call("sys_fcntl_setown", lambda: fcntl.fcntl(null_fd, fcntl.F_SETOWN, victim))
    call("sys_fcntl_setsig", lambda: fcntl.fcntl(null_fd, fcntl.F_SETSIG, signal.SIGUSR1))
    call("sys_fcntl_setown_ex", lambda: raw("fcntl", null_fd, 15, 0))
    call("sys_chmod", lambda: os.chmod(metadata, 0o666))
    call("sys_fchmod", lambda: os.fchmod(scratch_fd, 0o600))
    call("sys_chown", lambda: os.chown(metadata, os.getuid(), os.getgid()))
    call("sys_utime", lambda: os.utime(metadata, (1, 1)))
    call("sys_setxattr", lambda: os.setxattr(metadata, "user.probe", b"1"))
    call("sys_removexattr", lambda: os.removexattr(metadata, "user.probe"))
    call("sys_add_key", lambda: raw("add_key", 0, 0, 0, 0, 0))
    call("sys_request_key", lambda: raw("request_key", 0, 0, 0, 0))
    call("sys_keyctl", lambda: raw("keyctl", 0, -3, 0))  # KEYCTL_GET_KEYRING_ID, session
    call("sys_shmget", lambda: raw("shmget", absent_key, 0, 0))
    call("sys_shmat", lambda: raw("shmat", -1, 0, 0))
    call("sys_msgget", lambda: raw("msgget", absent_key, 0))
    call("sys_semget", lambda: raw("semget", absent_key, 0, 0))
    call("sys_mq_open", lambda: raw("mq_open", b"absent-queue", 0))
    # The same calls aimed at the worker itself stay available.
    call("self_sched_setaffinity", lambda: os.sched_setaffinity(0, os.sched_getaffinity(0)))
    call(
        "self_setpriority",
        lambda: os.setpriority(os.PRIO_PROCESS, 0, os.getpriority(os.PRIO_PROCESS, 0)),
    )
    call("self_prlimit64", lambda: resource.prlimit(os.getpid(), resource.RLIMIT_CORE))
    call("self_fcntl_getfl", lambda: fcntl.fcntl(null_fd, fcntl.F_GETFL))

    # Last: a successful exec replaces this process, so the replacement reports for both.
    report = json.dumps({"result": {**outcome, "execve": "allowed"}})
    attempt(
        "execve",
        lambda: os.execv(
            sys.executable, [sys.executable, "-I", "-c", "print(" + repr(report) + ")"]
        ),
    )
    return outcome

engine.OPERATIONS["profile"] = attack
runpy.run_module("agentic_analytics.dataplane.worker", run_name="__main__")
"""


class _CompromisedWorker(DataPlaneClient):
    def worker_command(self) -> list[str]:
        return [sys.executable, "-I", "-c", textwrap.dedent(_COMPROMISED_WORKER)]


def _attack(tmp_path: Path, **settings: Any) -> tuple[dict[str, str], dict[str, Path]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "data.csv"
    source.write_text("value\n1\n", encoding="utf-8")
    secret = workspace / "other-session-secret.csv"
    secret.write_text("token\nhunter2\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    metadata_file = outside / "metadata.txt"
    metadata_file.write_text("unchanged", encoding="utf-8")
    metadata_file.chmod(0o600)
    with contextlib.ExitStack() as stack:
        # A sacrificial process of the same uid, for the calls that take a pid.
        victim = stack.enter_context(subprocess.Popen(["sleep", "120"]))
        stack.callback(victim.kill)
        victim_limit = resource.prlimit(victim.pid, resource.RLIMIT_NOFILE)
        # Test-owned listeners: a pathname Unix socket standing in for /var/run/docker.sock,
        # and loopback UDP and TCP endpoints. Each records whether anything reached it.
        unix_path = outside / "ctl.sock"
        unix_listener = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM))
        unix_listener.bind(str(unix_path))
        unix_listener.listen(1)
        unix_listener.setblocking(False)
        udp = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        udp.bind(("127.0.0.1", 0))
        udp.setblocking(False)
        tcp = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(1)
        tcp.setblocking(False)
        targets: dict[str, Any] = {
            "source": source,
            "secret": secret,
            "outside_file": outside / "escape.txt",
            "copy_target": outside / "copy.csv",
            "unix_socket": unix_path,
            "udp_port": udp.getsockname()[1],
            "tcp_port": tcp.getsockname()[1],
            "server_pid": os.getpid(),
            "victim_pid": victim.pid,
            "metadata_file": metadata_file,
        }
        client = _CompromisedWorker(Settings(state_dir=tmp_path / "state", **settings))
        payload = {
            "limits": client.limits(30),
            "path": str(source),
            "kind": "csv",
            "attack": {key: str(value) for key, value in targets.items()},
        }
        outcome = client.call("profile", payload, deadline_seconds=30)
        received = {"unix": False, "udp": False, "tcp": False}
        with contextlib.suppress(BlockingIOError):
            unix_listener.accept()[0].close()
            received["unix"] = True
        with contextlib.suppress(BlockingIOError):
            udp.recvfrom(64)
            received["udp"] = True
        with contextlib.suppress(BlockingIOError):
            tcp.accept()[0].close()
            received["tcp"] = True
        targets["received"] = received
        targets["victim_limit"] = (
            victim_limit,
            resource.prlimit(victim.pid, resource.RLIMIT_NOFILE),
        )
    return outcome, targets


@requires_confinement
def test_compromised_worker_cannot_write_or_read_outside_its_allowed_paths(
    tmp_path: Path,
) -> None:
    outcome, targets = _attack(tmp_path)

    assert outcome["python_write_outside"].startswith("denied:PermissionError")
    assert outcome["python_overwrite_source"].startswith("denied:PermissionError")
    assert outcome["python_read_secret"].startswith("denied:PermissionError")
    assert outcome["duckdb_copy_to_elsewhere"].startswith("denied:")
    assert outcome["duckdb_read_disallowed"].startswith("denied:")
    # The operation's own grants still work: its authorized source and its private scratch.
    assert outcome["python_read_source"] == "allowed"
    assert outcome["scratch_write"] == "allowed"
    assert not targets["outside_file"].exists()
    assert not targets["copy_target"].exists()
    assert targets["source"].read_text(encoding="utf-8") == "value\n1\n"


@requires_confinement
def test_compromised_worker_cannot_reach_sockets_processes_or_exec(tmp_path: Path) -> None:
    """Escapes outside Landlock's filesystem rules (review of PR #25, P1 and P2)."""

    outcome, targets = _attack(tmp_path)

    # seccomp: no socket of any address family can be created, so a pathname Unix socket
    # (docker.sock), UDP, and TCP are unreachable whatever the Landlock ABI.
    for name in ("unix_connect", "udp_send", "tcp_connect", "socketpair"):
        assert outcome[name].startswith("denied:PermissionError"), (name, outcome[name])
    assert targets["received"] == {"unix": False, "udp": False, "tcp": False}
    # seccomp: no new program, no child process, and the death signal stays armed.
    assert outcome["execve"].startswith("denied:PermissionError")
    assert outcome["fork"].startswith("denied:PermissionError")
    assert outcome["clear_death_signal"].startswith("denied:")
    # seccomp (any ABI) and Landlock scoping (ABI 6+): no signal to the server.
    assert outcome["signal_server"].startswith("denied:PermissionError")
    # Landlock: the server's /proc entry is outside the read allowlist.
    assert outcome["read_server_proc"].startswith("denied:PermissionError")
    # Landlock: scratch grants no symlink creation, and a hardlink cannot cross the boundary.
    assert outcome["symlink_plant"].startswith("denied:PermissionError")
    assert outcome["hardlink_secret"].startswith("denied:")


_FILTERED_CALLS = (
    "prlimit64", "sched_setaffinity", "sched_setscheduler", "sched_setparam", "sched_setattr",
    "setpriority", "ioprio_set", "fcntl_setown", "fcntl_setsig", "fcntl_setown_ex",
    "chmod", "fchmod", "chown", "utime", "setxattr", "removexattr",
    "add_key", "request_key", "keyctl", "shmget", "shmat", "msgget", "semget", "mq_open",
)  # fmt: skip


@requires_confinement
def test_compromised_worker_cannot_act_on_other_processes_metadata_keys_or_ipc(
    tmp_path: Path,
) -> None:
    """Calls Landlock does not mediate are refused by the seccomp filter (#26)."""

    outcome, targets = _attack(tmp_path)

    results = {name: outcome[f"sys_{name}"] for name in _FILTERED_CALLS}
    assert results == dict.fromkeys(_FILTERED_CALLS, "EPERM")
    # Nothing reached the sacrificial process or file.
    before, after = targets["victim_limit"]
    assert after == before
    status = targets["metadata_file"].stat()
    assert status.st_mode & 0o777 == 0o600
    assert status.st_mtime > 1
    # The worker can still make the same calls on itself.
    assert {name: value for name, value in outcome.items() if name.startswith("self_")} == {
        "self_sched_setaffinity": "allowed",
        "self_setpriority": "allowed",
        "self_prlimit64": "allowed",
        "self_fcntl_getfl": "allowed",
    }


@linux_only
def test_sandbox_off_is_an_explicit_opt_out(tmp_path: Path) -> None:
    outcome, targets = _attack(tmp_path, data_plane_sandbox="off")

    assert outcome["python_write_outside"] == "allowed"
    assert targets["outside_file"].exists()
    assert outcome["unix_connect"] == "allowed"
    # Control for the #26 test: without the filter these calls succeed.
    assert outcome["sys_prlimit64"] == "allowed"
    assert outcome["sys_chmod"] == "allowed"
    assert targets["victim_limit"][1] == (7, 7)
    assert targets["metadata_file"].stat().st_mode & 0o777 == 0o666


@requires_confinement
def test_read_root_covering_a_protected_root_fails_closed(tmp_path: Path) -> None:
    """A workspace or state directory under a runtime read root would be readable (P2)."""

    # The interpreter prefix is a runtime read root; authorizing a workspace there would let
    # the read allowlist cover it, so the worker must refuse to start rather than expose it.
    client = DataPlaneClient(
        Settings(state_dir=tmp_path / "state", allowed_workspace_roots=[Path(sys.prefix)])
    )
    policy = client.sandbox_policy({}, tmp_path)
    assert os.path.realpath(sys.prefix) in policy.protected_roots
    with pytest.raises(DataPlaneError) as caught:
        client.call("profile", {"limits": client.limits(10)}, deadline_seconds=10)
    assert caught.value.category == "sandbox_unavailable"
    message = str(caught.value)
    assert sys.prefix not in message and str(tmp_path) not in message


def test_protected_root_check_rejects_ancestors_and_equal_paths() -> None:
    from agentic_analytics.dataplane import sandbox as sandbox_module

    check = sandbox_module.check_protected_roots
    check(["/usr", "/srv/venv"], ["/srv/app/workspace", "/var/state"])  # disjoint: fine
    check(["/srv/app/workspace/.venv"], ["/srv/app/workspace"])  # a descendant only
    check(["/usr"], ["/usrdata"])  # a shared prefix is not an ancestor
    for read_root, protected in (("/usr", "/usr/src/app"), ("/srv/s", "/srv/s"), ("/", "/a")):
        with pytest.raises(sandbox_module.SandboxUnavailable):
            check([read_root], [protected])


def test_required_sandbox_fails_closed_without_landlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentic_analytics.dataplane import sandbox as sandbox_module

    monkeypatch.setattr(sandbox_module, "landlock_abi", lambda: 0)
    with pytest.raises(sandbox_module.SandboxUnavailable):
        sandbox_module.enter(sandbox_module.SandboxPolicy(mode="required"))
    assert sandbox_module.enter(sandbox_module.SandboxPolicy(mode="off")) == "off"
    if sys.platform.startswith("linux"):
        # On Linux "auto" promises confinement, so a kernel without Landlock fails closed too.
        with pytest.raises(sandbox_module.SandboxUnavailable):
            sandbox_module.enter(sandbox_module.SandboxPolicy(mode="auto"))
    else:
        assert sandbox_module.enter(sandbox_module.SandboxPolicy(mode="auto")) == "unavailable"


@linux_only
def test_worker_reports_unavailable_sandbox_as_a_classified_failure(tmp_path: Path) -> None:
    class _NoLandlock(DataPlaneClient):
        def worker_command(self) -> list[str]:
            script = (
                "import runpy\n"
                "import agentic_analytics.dataplane.sandbox as s\n"
                "s.landlock_abi = lambda: 0\n"
                "runpy.run_module('agentic_analytics.dataplane.worker', run_name='__main__')\n"
            )
            return [sys.executable, "-I", "-c", script]

    client = _NoLandlock(Settings(state_dir=tmp_path / "state"))
    with pytest.raises(DataPlaneError) as caught:
        client.call("profile", {"limits": client.limits(10)}, deadline_seconds=10)
    assert caught.value.category == "sandbox_unavailable"


# --- the worker dies with its parent ----------------------------------------------------------

_PARENT = """
import json, subprocess, sys
from pathlib import Path
from agentic_analytics.dataplane import client as client_module
from agentic_analytics.settings import Settings

state, pid_file, pdeathsig = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] == "1"
real_popen = subprocess.Popen

class Recording(real_popen):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        pid_file.write_text(str(self.pid))

client_module.subprocess.Popen = Recording
client = client_module.DataPlaneClient(Settings(state_dir=state))
client.parent_death_signal = pdeathsig
spill = state / "archive" / "spill.parquet"
spill.parent.mkdir(parents=True)
payload = {
    "limits": client.limits(600),
    "sources": [],
    "spill_path": str(spill),
    "result_table": "r",
    "limit": 1,
    "spill_cap": 1,
    # Long enough to outlive the test by minutes if nothing ends the worker.
    "sql": "SELECT count(*) FROM range(100000000000000) t(x) WHERE x % 7 = 3",
}
client.call("query", payload, deadline_seconds=600)
"""


def _worker_running(pid: int) -> bool:
    # The worker stays single-threaded until bind_to_parent and sandbox.enter have returned
    # (worker.main); its first extra thread is the parent watchdog or DuckDB's own. A second
    # thread therefore proves the binding is in place and the operation is under way.
    return _processes.thread_count(pid) >= 2


def _assert_worker_dies_with_parent(tmp_path: Path, *, pdeathsig: bool) -> None:
    pid_file = tmp_path / "worker.pid"
    parent = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(_PARENT),
            str(tmp_path / "state"),
            str(pid_file),
            "1" if pdeathsig else "0",
        ]
    )
    worker_pid = None
    try:
        deadline = time.monotonic() + 60
        while worker_pid is None and time.monotonic() < deadline:
            if pid_file.exists() and pid_file.read_text():
                worker_pid = int(pid_file.read_text())
            elif parent.poll() is not None:
                pytest.fail(f"parent exited early with {parent.returncode}")
            else:
                time.sleep(0.05)
        assert worker_pid is not None, "worker never started"
        if _processes.HAS_PROC:
            started = _processes.wait_for(lambda: _worker_running(worker_pid), 60)
            assert started, f"worker never reached its operation: {_processes.describe(worker_pid)}"
        else:  # pragma: no cover - POSIX without /proc (macOS)
            time.sleep(2.0)
        assert _processes.alive(worker_pid), "worker ended before the parent was killed"

        parent.send_signal(signal.SIGKILL)
        parent.wait(timeout=10)
        # Bounded, and still far under the 600 s operation deadline: the death signal is
        # immediate and the watchdog polls every 0.25 s, so this allows for a loaded host.
        ended = _processes.wait_for(lambda: not _processes.alive(worker_pid), 15)
        assert ended, f"worker outlived its killed parent: {_processes.describe(worker_pid)}"
    finally:
        with contextlib.suppress(ProcessLookupError):
            parent.kill()
        if worker_pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(worker_pid, signal.SIGKILL)


@posix_only
def test_killing_the_parent_ends_the_worker(tmp_path: Path) -> None:
    _assert_worker_dies_with_parent(tmp_path, pdeathsig=True)


@posix_only
def test_portable_watchdog_ends_an_orphaned_worker_without_pdeathsig(tmp_path: Path) -> None:
    _assert_worker_dies_with_parent(tmp_path, pdeathsig=False)


def test_policy_grants_only_the_operation_paths(tmp_path: Path) -> None:
    from agentic_analytics.dataplane import sandbox as sandbox_module

    client = DataPlaneClient(Settings(state_dir=tmp_path / "state"))
    spill = tmp_path / "archive" / "s" / "e" / "query-result.parquet"
    policy = client.sandbox_policy(
        {
            "sources": [{"path": "/data/a.csv"}, {"path": "/data/b.parquet"}],
            "spill_path": str(spill),
        },
        tmp_path / "scratch",
    )
    assert policy.read_files == ("/data/a.csv", "/data/b.parquet")
    assert policy.write_dirs == (str(tmp_path / "scratch"), str(spill.parent))
    assert json.loads(json.dumps(policy.to_json())) == policy.to_json()
    assert sandbox_module.SandboxPolicy.from_json(policy.to_json()) == policy


@requires_confinement
def test_symlink_swapped_in_for_an_authorized_source_fails_closed(tmp_path: Path) -> None:
    """The worker grants the source without following a final symlink (P3)."""

    secret = tmp_path / "secret.csv"
    secret.write_text("token\nhunter2\n", encoding="utf-8")
    swapped = tmp_path / "data.csv"
    swapped.symlink_to(secret)
    client = DataPlaneClient(Settings(state_dir=tmp_path / "state"))
    payload = {"limits": client.limits(10), "path": str(swapped), "kind": "csv"}
    with pytest.raises(DataPlaneError) as caught:
        client.call("profile", payload, deadline_seconds=10)
    assert caught.value.category == "sandbox_unavailable"
    assert "hunter2" not in str(caught.value)


@requires_confinement
def test_source_under_a_symlinked_parent_directory_fails_closed(tmp_path: Path) -> None:
    """No component of an authorized source path may be a symlink (#26)."""

    real = tmp_path / "real"
    real.mkdir()
    (real / "data.csv").write_text("value\n1\n", encoding="utf-8")
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    client = DataPlaneClient(Settings(state_dir=tmp_path / "state"))
    direct = {
        "limits": client.limits(10),
        "path": str(real / "data.csv"),
        "kind": "csv",
        "sample_limit": 5,
        "max_profile_columns": 10,
    }
    assert client.call("profile", direct, deadline_seconds=10)["profile"]
    through_link = {**direct, "path": str(tmp_path / "link" / "data.csv")}
    with pytest.raises(DataPlaneError) as caught:
        client.call("profile", through_link, deadline_seconds=10)
    assert caught.value.category == "sandbox_unavailable"


@linux_only
@pytest.mark.parametrize("openat2", [True, False])
def test_source_file_open_refuses_a_symlink_in_any_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openat2: bool
) -> None:
    """Both the openat2 path and the per-component fallback fail closed."""

    from agentic_analytics.dataplane import sandbox as sandbox_module

    def unsupported(root_fd: int, relative: str) -> int:
        raise OSError(errno.ENOSYS, "openat2")

    if not openat2:
        monkeypatch.setattr(sandbox_module, "_open_beneath_root", unsupported)
    real = tmp_path / "real"
    real.mkdir()
    (real / "data.csv").write_text("value\n1\n", encoding="utf-8")
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    (real / "final-link.csv").symlink_to(real / "data.csv")

    fd = sandbox_module._open_source_file(str(real / "data.csv"))
    assert fd is not None
    os.close(fd)
    assert sandbox_module._open_source_file(str(real / "missing.csv")) is None
    for swapped in (tmp_path / "link" / "data.csv", real / "final-link.csv"):
        with pytest.raises(sandbox_module.SandboxUnavailable):
            sandbox_module._open_source_file(str(swapped))


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_seccomp_program_is_well_formed_for_each_supported_architecture(machine: str) -> None:
    """Structural check; aarch64 is not executed in CI, so its table is checked here."""

    from agentic_analytics.dataplane import sandbox as sandbox_module

    program = sandbox_module.seccomp_program(machine, 4242)
    assert 0 < len(program) < 4096
    ret, jumps = 0x06, {0x15, 0x35, 0x45}
    for index, (code, jt, jf, _k) in enumerate(program):
        if code in jumps:  # every branch lands inside the program
            assert index + 1 + max(jt, jf) < len(program)
    # The architecture is checked first and a mismatch kills; the default is allow.
    audit_arch = sandbox_module._SECCOMP_ARCHES[machine][0]
    assert program[0] == (0x20, 0, 0, 4)
    assert program[1] == (0x15, 1, 0, audit_arch)
    assert program[2] == (ret, 0, 0, 0x80000000)
    assert program[-1] == (ret, 0, 0, 0x7FFF0000)
    numbers = sandbox_module._SECCOMP_ARCHES[machine][1]
    for name in sandbox_module._SECCOMP_DENY:
        assert name in numbers or name in sandbox_module._SECCOMP_X86_64_ONLY
    assert len(set(numbers.values())) == len(numbers)  # no copy-paste duplicates
    with pytest.raises(sandbox_module.SandboxUnavailable):
        sandbox_module.seccomp_program("riscv64", 4242)


def _run_filter(
    program: list[tuple[int, int, int, int]], arch: int, number: int, *args: int
) -> int:
    """Evaluate the classic-BPF subset the filter uses against one ``seccomp_data``."""

    padded = [arg & 0xFFFFFFFFFFFFFFFF for arg in (*args, 0, 0, 0, 0, 0, 0)[:6]]
    data = struct.pack("<iIQ6Q", number, arch, 0, *padded)
    accumulator = index = 0
    while True:
        code, jt, jf, k = program[index]
        index += 1
        if code == 0x06:  # BPF_RET
            return k
        if code == 0x20:  # BPF_LD | BPF_W | BPF_ABS
            accumulator = int.from_bytes(data[k : k + 4], "little")
            continue
        taken = {0x15: accumulator == k, 0x35: accumulator >= k, 0x45: bool(accumulator & k)}
        index += jt if taken[code] else jf


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_seccomp_program_decisions_for_each_supported_architecture(machine: str) -> None:
    """What the filter returns per call and argument, on both tables (#26)."""

    from agentic_analytics.dataplane import sandbox as sandbox_module

    pid = 4242
    program = sandbox_module.seccomp_program(machine, pid)
    arch, numbers = sandbox_module._SECCOMP_ARCHES[machine]
    allow, eperm = 0x7FFF0000, 0x00050000 | errno.EPERM

    def decide(name: str, *args: int) -> int:
        return _run_filter(program, arch, numbers[name], *args)

    for name in sandbox_module._SECCOMP_DENY:
        if name in numbers:
            assert decide(name) == eperm, name
    for name in (*sandbox_module._SECCOMP_SELF_ONLY, *sandbox_module._SECCOMP_SELF_OR_ZERO):
        assert decide(name, pid) == allow, name
        assert decide(name, 1) == eperm, name
        assert decide(name, -1) == eperm, name
    for name in sandbox_module._SECCOMP_SELF_ONLY:
        assert decide(name, 0) == eperm, name  # pid 0 is the whole process group
    for name in sandbox_module._SECCOMP_SELF_OR_ZERO:
        assert decide(name, 0) == allow, name  # pid 0 is the caller
    for name, which in sandbox_module._SECCOMP_WHICH_WHO:
        assert decide(name, which, 0) == allow, name
        assert decide(name, which, pid) == allow, name
        assert decide(name, which, 1) == eperm, name
        assert decide(name, which + 1, 0) == eperm, name  # a process group or a uid
    for command in (8, 10, 15):  # F_SETOWN, F_SETSIG, F_SETOWN_EX
        assert decide("fcntl", 3, command) == eperm, command
    for command in (1, 3, 4, 6, 9, 1030):  # GETFD, GETFL, SETFL, SETLK, GETOWN, DUPFD_CLOEXEC
        assert decide("fcntl", 3, command) == allow, command
    assert decide("clone", 0x00010000) == allow
    assert decide("clone", 0x11) == eperm
    assert decide("prctl", 1) == eperm
    assert decide("prctl", 38) == allow
    assert decide("clone3") == 0x00050000 | errno.ENOSYS
    assert _run_filter(program, arch, 9999) == allow  # the default
    assert _run_filter(program, 0x40000003, numbers["fcntl"]) == 0x80000000  # i386: killed
