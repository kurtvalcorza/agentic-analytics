"""On-disk layout of server state and of the workspace-internal directory.

One place for these names: the data-plane sandbox's protected roots, the Docker mount
exclusions, workspace snapshots and the artifact archive must all agree on them, and a
layout change that missed one site would weaken a protection silently.
"""

from __future__ import annotations

from pathlib import Path

# Directory inside a workspace that holds the server's own files (default state, dev HOME,
# staging); never an analytical source and never snapshotted as an execution output.
INTERNAL_DIR_NAME = ".agentic-analytics"

# Subdirectories of ``state_dir``.
ARCHIVE_DIR_NAME = "artifacts"
SCRATCH_DIR_NAME = "tmp"
DATAPLANE_SCRATCH_DIR_NAME = "dataplane"


def archive_dir(state_dir: Path) -> Path:
    """Content-addressed artifact archive (query spills, copied execution outputs)."""

    return state_dir / ARCHIVE_DIR_NAME


def scratch_dir(state_dir: Path) -> Path:
    """Server-owned scratch space (managed-execution request scripts)."""

    return state_dir / SCRATCH_DIR_NAME


def dataplane_scratch_dir(state_dir: Path) -> Path:
    """Per-operation private scratch directories of data-plane workers."""

    return scratch_dir(state_dir) / DATAPLANE_SCRATCH_DIR_NAME
