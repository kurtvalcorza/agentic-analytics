from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel

from agentic_analytics.ids import EntityType, is_canonical_id


class RecordAlreadyExists(RuntimeError):
    pass


class RecordNotFound(KeyError):
    pass


class SessionScopeError(PermissionError):
    pass


class JsonRecordRepository[RecordT: BaseModel]:
    """Append-only JSON record store scoped by session and record type.

    Each record is a create-once file. O_EXCL prevents accidental replacement and keeps the
    initial persistence semantics immutable without relying on fragile JSONL partial appends.
    """

    def __init__(self, root: Path, namespace: str, model_type: type[RecordT]) -> None:
        self.root = root.resolve(strict=False)
        self.namespace = namespace
        self.model_type = model_type

    def _session_dir(self, session_id: str) -> Path:
        if not is_canonical_id(session_id, EntityType.SESSION):
            raise ValueError("session_id must use the canonical ses_ ID format")
        return self.root / session_id / self.namespace

    def _path(self, session_id: str, record_id: str) -> Path:
        if not is_canonical_id(record_id):
            raise ValueError("record_id must use a canonical typed ID format")
        return self._session_dir(session_id) / f"{record_id}.json"

    def add(self, record: RecordT) -> RecordT:
        record_data = record.model_dump(mode="python")
        session_id = str(record_data.get("session_id") or record_data.get("id") or "")
        if not is_canonical_id(session_id, EntityType.SESSION):
            raise ValueError("record must expose a canonical session_id or be a session record")
        record_id = str(record_data.get("id") or "")
        target = self._path(session_id, record_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = record.model_dump_json(by_alias=True, indent=2).encode("utf-8") + b"\n"
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise RecordAlreadyExists(record_id) from exc
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return record

    def get(self, session_id: str, record_id: str) -> RecordT:
        target = self._path(session_id, record_id)
        if not target.exists():
            if self._record_exists_elsewhere(record_id):
                raise SessionScopeError(
                    f"record {record_id} does not belong to session {session_id}"
                )
            raise RecordNotFound(record_id)
        data = json.loads(target.read_text(encoding="utf-8"))
        return self.model_type.model_validate(data)

    def list(self, session_id: str) -> list[RecordT]:
        directory = self._session_dir(session_id)
        if not directory.exists():
            return []
        return [
            self.model_type.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.json"))
        ]

    def _record_exists_elsewhere(self, record_id: str) -> bool:
        if not is_canonical_id(record_id):
            return False
        return any(self.root.glob(f"*/{self.namespace}/{record_id}.json"))
