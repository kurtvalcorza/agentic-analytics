import os
import sys
from pathlib import Path

import pytest

from agentic_analytics.dataplane.sandbox import landlock_abi

_NO_LANDLOCK = (
    "this Linux kernel has no Landlock, so every data-plane worker fails closed with "
    "sandbox_unavailable; set AGENTIC_ANALYTICS_DATA_PLANE_SANDBOX=off to run the tests that "
    "do not assert confinement"
)


def _confinement_unavailable() -> bool:
    return (
        sys.platform.startswith("linux")
        and landlock_abi() <= 0
        and os.environ.get("AGENTIC_ANALYTICS_DATA_PLANE_SANDBOX", "auto") != "off"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip with one explanation instead of failing every data-plane test (SEC-02)."""

    if _confinement_unavailable():
        marker = pytest.mark.skip(reason=_NO_LANDLOCK)
        for item in items:
            item.add_marker(marker)


def pytest_report_header(config: pytest.Config) -> str | None:
    return f"data-plane sandbox: {_NO_LANDLOCK}" if _confinement_unavailable() else None


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
