from pathlib import Path

from agentic_analytics.models import ExecutionRecord

from .base import JsonRecordRepository


class ExecutionRepository(JsonRecordRepository[ExecutionRecord]):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "executions", ExecutionRecord)
