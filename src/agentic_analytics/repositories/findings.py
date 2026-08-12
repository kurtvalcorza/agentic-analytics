from pathlib import Path

from agentic_analytics.models import ValidationFinding

from .base import JsonRecordRepository


class FindingRepository(JsonRecordRepository[ValidationFinding]):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "findings", ValidationFinding)
