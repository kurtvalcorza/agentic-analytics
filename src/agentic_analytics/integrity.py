"""Content hashing for source fingerprints and artifact attestation.

One implementation for both, so sources and artifacts carry the same guarantee: the digest,
the size and the mtime all describe the single open file that was read, not a later stat of a
path that may have been replaced or appended to in between.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> tuple[str, int, os.stat_result]:
    """Hash ``path`` in one pass; return the digest, the bytes hashed, and fstat of that file."""

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
        info = os.fstat(stream.fileno())
    return digest.hexdigest(), size, info


def fingerprint_file(path: Path) -> dict[str, Any]:
    """Source fingerprint: SHA-256, byte count and mtime of exactly the bytes hashed."""

    sha256, size, info = sha256_file(path)
    return {"sha256": sha256, "size_bytes": size, "mtime_ns": info.st_mtime_ns}
