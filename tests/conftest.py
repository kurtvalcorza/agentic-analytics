import os
import shutil
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest

from agentic_analytics.dataplane.sandbox import landlock_abi

_NO_LANDLOCK = (
    "this Linux kernel has no Landlock, so every data-plane worker fails closed with "
    "sandbox_unavailable; set AGENTIC_ANALYTICS_DATA_PLANE_SANDBOX=off to run the tests that "
    "do not assert confinement"
)


DOCKER_TEST_IMAGE = "agentic-analytics-exec:test"
_BUILD_HINT = f"build it with: docker build -t {DOCKER_TEST_IMAGE} -f docker/Dockerfile.exec ."


@cache
def _docker_unavailable() -> str | None:
    """Why the ``docker`` tests cannot run here, or None when they can."""

    if shutil.which("docker") is None:
        return "docker is not on PATH"
    try:
        probe = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", DOCKER_TEST_IMAGE],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"docker did not answer: {exc}"
    if probe.returncode == 0:
        return None
    detail = (probe.stderr.strip().splitlines() or [""])[-1]
    if "no such image" in detail.lower():
        return f"the {DOCKER_TEST_IMAGE} image is missing; {_BUILD_HINT}"
    return f"docker image inspect failed ({detail or probe.returncode}); is the daemon running?"


def _confinement_unavailable() -> bool:
    return (
        sys.platform.startswith("linux")
        and landlock_abi() <= 0
        and os.environ.get("AGENTIC_ANALYTICS_DATA_PLANE_SANDBOX", "auto") != "off"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip with one explanation instead of failing many tests (SEC-02, DOC2).

    CI sets AGENTIC_ANALYTICS_TEST_REQUIRE_SANDBOX=1 so a runner without Landlock fails the
    run instead of passing it with every test skipped, and AGENTIC_ANALYTICS_TEST_REQUIRE_DOCKER=1
    so a missing exec image fails the run instead of skipping the ``docker`` tests.
    """

    if _confinement_unavailable():
        if os.environ.get("AGENTIC_ANALYTICS_TEST_REQUIRE_SANDBOX") == "1":
            raise pytest.UsageError(f"AGENTIC_ANALYTICS_TEST_REQUIRE_SANDBOX=1 but {_NO_LANDLOCK}")
        marker = pytest.mark.skip(reason=_NO_LANDLOCK)
        for item in items:
            item.add_marker(marker)
        return
    docker_items = [item for item in items if item.get_closest_marker("docker")]
    if not docker_items:
        return
    reason = _docker_unavailable()
    if reason is None:
        return
    if os.environ.get("AGENTIC_ANALYTICS_TEST_REQUIRE_DOCKER") == "1":
        raise pytest.UsageError(f"AGENTIC_ANALYTICS_TEST_REQUIRE_DOCKER=1 but {reason}")
    skip = pytest.mark.skip(reason=f"Docker test: {reason}")
    for item in docker_items:
        item.add_marker(skip)


def pytest_report_header(config: pytest.Config) -> list[str]:
    lines = []
    if _confinement_unavailable():
        lines.append(f"data-plane sandbox: {_NO_LANDLOCK}")
    return lines


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    root = tmp_path / "state"
    root.mkdir()
    return root
