from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agentic_analytics.ids import EntityType, is_canonical_id
from agentic_analytics.models import AnalysisSession, SessionStatus

from .base import JsonRecordRepository, SessionScopeError
from .locking import record_lock


class SessionRepository(JsonRecordRepository[AnalysisSession]):
    def __init__(self, root: Path) -> None:
        super().__init__(root, "sessions", AnalysisSession, EntityType.SESSION)

    @contextmanager
    def policy_lock(self) -> Iterator[None]:
        with record_lock(self.root / "locks" / "workspace-policy.lock"):
            yield

    @contextmanager
    def operation_lock(self, session_id: str) -> Iterator[None]:
        if not is_canonical_id(session_id, EntityType.SESSION):
            raise ValueError("session_id must use the canonical ses_ ID format")
        with record_lock(self.root / "locks" / f"{session_id}.lock"):
            yield

    @contextmanager
    def active(self, session_id: str) -> Iterator[AnalysisSession]:
        with self.operation_lock(session_id):
            session = self.get(session_id, session_id)
            if session.status is not SessionStatus.ACTIVE:
                raise SessionScopeError("session is closed; create a new analysis session")
            yield session

    def list_all(self) -> list[AnalysisSession]:
        """Return every persisted session across the state store."""

        return [
            self.model_type.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.root.glob(f"*/{self.namespace}/*.json"))
        ]
