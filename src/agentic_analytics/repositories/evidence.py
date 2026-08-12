from pathlib import Path

from agentic_analytics.models import EvidenceItem

from .base import JsonRecordRepository


class EvidenceRepository(JsonRecordRepository[EvidenceItem]):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "evidence", EvidenceItem)
