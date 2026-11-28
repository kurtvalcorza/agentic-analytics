"""Supported host platforms (C14).

The server needs POSIX process control (process groups, ``fcntl`` locks, resource limits),
and its confined data plane needs Linux (Landlock and seccomp). Native Windows is not
supported; run the server in WSL2 or a Linux container instead. macOS runs, but without data
plane confinement (see the README).

The check runs when the package is imported, before any module that needs POSIX, so a Windows
host gets one clear error instead of an ``ImportError`` from deep inside the package.
"""

from __future__ import annotations

import sys

UNSUPPORTED_PLATFORMS = frozenset({"win32"})


class UnsupportedPlatformError(RuntimeError):
    """The server cannot run on this host."""


def check_supported_platform(platform: str = sys.platform) -> None:
    """Raise :class:`UnsupportedPlatformError` on a host the server does not support."""

    if platform in UNSUPPORTED_PLATFORMS:
        raise UnsupportedPlatformError(
            "agentic-analytics does not support native Windows: its data plane needs Linux "
            "(Landlock and seccomp) and its process control needs POSIX. Run it in WSL2 or a "
            "Linux container; see docs/testing.md."
        )
