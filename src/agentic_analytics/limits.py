"""Default resource limits shared by Settings and the execution backends.

Settings is the configuration surface; the backends and DiskLimits default to the same
values so a backend built directly (tests, embedders) cannot silently drift from what the
server would configure. Settings' cross-field validation still applies only to Settings.
"""

from __future__ import annotations

MIB = 1024 * 1024

DOCKER_MEMORY = "1g"
DOCKER_CPUS = 1.0
DOCKER_PIDS_LIMIT = 128
# Size of each run's tmpfs at /workspace/outputs (SEC-01).
DOCKER_OUTPUT_QUOTA_BYTES = 256 * MIB

MAX_OUTPUT_CHARS = 65536

# Budget for bytes/files one execution may add to its writable workspace, plus a host
# free-space reserve.
MAX_EXECUTION_OUTPUT_BYTES = 512 * MIB
MAX_EXECUTION_OUTPUT_FILES = 10_000
MIN_FREE_DISK_BYTES = 256 * MIB
