from pathlib import Path

from agentic_analytics.models import ValidationRun

from .base import JsonRecordRepository


class ValidationRunRepository(JsonRecordRepository[ValidationRun]):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "validation-runs", ValidationRun)
