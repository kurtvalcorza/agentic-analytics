from pathlib import Path

from agentic_analytics.models import AnalysisSession

from .base import JsonRecordRepository


class SessionRepository(JsonRecordRepository[AnalysisSession]):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "sessions", AnalysisSession)
