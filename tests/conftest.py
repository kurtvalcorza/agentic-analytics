from pathlib import Path
import pytest
from agentic_analytics.execution_backends import SubprocessDevelopmentBackend
from agentic_analytics.runtime import AnalyticsRuntime

@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "survey.csv").write_text("group,value,denominator\nA,1,2\nA,1,2\nB,,1\n")
    return tmp_path

@pytest.fixture
def runtime(tmp_path: Path) -> AnalyticsRuntime:
    return AnalyticsRuntime(tmp_path / "state", SubprocessDevelopmentBackend())
