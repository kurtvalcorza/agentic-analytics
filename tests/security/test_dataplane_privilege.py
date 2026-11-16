"""SEC-02 / #21: the data-plane worker is privilege-isolated and cannot outlive the server.

The confinement tests simulate a compromised worker (for example a DuckDB remote-code-execution
flaw): attacker code replaces a worker operation and runs *after* the real worker entry point
has set itself up, then tries to escape. DuckDB's own ``allowed_paths`` cannot help here, because
the attacker opens its own unrestricted connection and plain Python file handles.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics.dataplane import DataPlaneClient, DataPlaneError
from agentic_analytics.settings import Settings

linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Landlock confinement is Linux-only"
)
posix_only = pytest.mark.skipif(os.name != "posix", reason="kills a POSIX parent process")

# Runs as the worker program: installs the attacker operation, then hands control to the real
# worker entry point, which applies limits and isolation before dispatching to the operation.
_COMPROMISED_WORKER = """
import runpy
import agentic_analytics.dataplane.engine as engine

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
    targets = {
        "source": source,
        "secret": secret,
        "outside_file": outside / "escape.txt",
        "copy_target": outside / "copy.csv",
    }
    client = _CompromisedWorker(Settings(state_dir=tmp_path / "state", **settings))
    payload = {
        "limits": client.limits(30),
        "path": str(source),
        "kind": "csv",
        "attack": {key: str(value) for key, value in targets.items()},
    }
    return client.call("profile", payload, deadline_seconds=30), targets


@linux_only
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


@linux_only
def test_sandbox_off_is_an_explicit_opt_out(tmp_path: Path) -> None:
    outcome, targets = _attack(tmp_path, data_plane_sandbox="off")

    assert outcome["python_write_outside"] == "allowed"
    assert targets["outside_file"].exists()


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


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    with contextlib.suppress(OSError):
        if stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":  # zombie awaiting its reaper
            return False
    return True


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
        time.sleep(2.0)  # let the worker finish start-up and start the query
        assert _alive(worker_pid), "worker ended before the parent was killed"

        parent.send_signal(signal.SIGKILL)
        parent.wait(timeout=10)
        ended_by = time.monotonic() + 5.0  # bounded: well under the 600 s operation deadline
        while _alive(worker_pid) and time.monotonic() < ended_by:
            time.sleep(0.05)
        assert not _alive(worker_pid), "worker outlived its killed parent"
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
