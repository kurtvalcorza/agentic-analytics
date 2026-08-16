from pathlib import Path

from agentic_analytics.ids import EntityType
from agentic_analytics.models import AnalysisSession

from .base import JsonRecordRepository


class SessionRepository(JsonRecordRepository[AnalysisSession]):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "sessions", AnalysisSession, EntityType.SESSION)

    def list_all(self) -> list[AnalysisSession]:
        """Return every persisted session across the state store."""

        return [
            self.model_type.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.root.glob(f"*/{self.namespace}/*.json"))
        ]
