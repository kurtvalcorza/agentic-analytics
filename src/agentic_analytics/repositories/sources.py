from pathlib import Path

from agentic_analytics.models import DataSource

from .base import JsonRecordRepository


class SourceRepository(JsonRecordRepository[DataSource]):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "sources", DataSource)
