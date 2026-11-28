"""Child-process helpers shared by the data-plane client and the execution backends (C2).

Every child the server starts runs in its own session (``start_new_session=True``), so its
process group id is its own pid, and the whole tree it spawns can be killed in one call.

**When a group may be signalled.** A process group exists while any member is alive, and its
id cannot be reused until then. Signal a group only while its leader has not been reaped, or
while some member is known to be alive (for example, a descendant still holds the child's
pipes open). Once the group is empty its id may be recycled by an unrelated process.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
from typing import IO, Any

CHUNK_SIZE = 65536


def drain_bounded[T: (str, bytes)](
    stream: IO[T],
    chunks: list[T],
    limit: int,
    truncated: threading.Event | None = None,
) -> None:
    """Read ``stream`` to EOF, keeping at most ``limit`` bytes or characters in ``chunks``.

    Past the limit it keeps reading and discarding, so the child never blocks on a full pipe,
    and sets ``truncated``. The stream is closed at EOF. Join ``chunks`` to get the text.
    """

    total = 0
    while chunk := stream.read(CHUNK_SIZE):
        room = limit - total
        if len(chunk) > room and truncated is not None:
            truncated.set()
        if room > 0:
            kept = chunk[:room]
            chunks.append(kept)
            total += len(kept)
    stream.close()


def kill_group(pgid: int) -> None:
    """SIGKILL every process in group ``pgid``; a group that is already gone is ignored."""

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def kill_tree(proc: subprocess.Popen[Any]) -> None:
    """SIGKILL ``proc`` and its whole process group (``proc`` must lead its own session).

    See the module docstring for when this is safe to call.
    """

    kill_group(proc.pid)
    with contextlib.suppress(OSError):
        proc.kill()
