"""C1: the confined data-plane worker loads no server-side service or tool code."""

import json
import subprocess
import sys


def test_dataplane_engine_imports_no_service_or_tool_modules() -> None:
    # A fresh interpreter, as the worker uses, so the check sees only what the engine pulls in.
    script = (
        "import json, sys\n"
        "import agentic_analytics.dataplane.engine\n"
        "import agentic_analytics.dataplane.worker\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith(("
        "'agentic_analytics.services', 'agentic_analytics.tools')))))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == []
