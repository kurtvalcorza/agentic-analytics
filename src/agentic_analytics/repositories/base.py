from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel

from agentic_analytics.ids import EntityType, is_canonical_id


class RecordAlreadyExists(RuntimeError):
    pass


class RecordNotFound(KeyError):
    pass


class SessionScopeError(PermissionError):
    pass


def require_session_scope(session_id: str, *records: BaseModel) -> None:
    """Reject records that do not belong to the requested analysis session."""

    if not is_canonical_id(session_id, EntityType.SESSION):
        raise ValueError("session_id must use the canonical ses_ ID format")
    for record in records:
        data = record.model_dump(mode="python")
        owner = str(data.get("session_id") or data.get("id") or "")
        if owner != session_id:
            raise SessionScopeError(
                f"record {data.get('id', '<unknown>')} does not belong to session {session_id}"
            )


class JsonRecordRepository[RecordT: BaseModel]:
    """JSON record store scoped by session and record type.

    Records are published atomically: the serialized payload is written and fsynced to a
    temporary file that is then linked (create) or renamed (replace) into place, so readers
    never observe a partially written file and a crash mid-write cannot leave a truncated
    record behind. ``add`` is create-once; ``update`` durably advances a record's mutable
    fields (for example a session status transition) without breaking that guarantee.
    """

    def __init__(
        self, root: Path, namespace: str, model_type: type[RecordT], entity_type: EntityType
    ) -> None:
        self.root = root.resolve(strict=False)
        self.namespace = namespace
        self.model_type = model_type
        self.entity_type = entity_type

    def _session_dir(self, session_id: str) -> Path:
        if not is_canonical_id(session_id, EntityType.SESSION):
            raise ValueError("session_id must use the canonical ses_ ID format")
        return self.root / session_id / self.namespace

    def _path(self, session_id: str, record_id: str) -> Path:
        if not is_canonical_id(record_id, self.entity_type):
            raise ValueError(
                f"record_id must use the canonical {self.entity_type.value}_ ID format"
            )
        return self._session_dir(session_id) / f"{record_id}.json"

    def _validated(self, record: RecordT) -> RecordT:
        """Re-run validation over the current model state.

        Pydantic assignment validation does not cover in-place collection mutations (for
        example ``item.source_ids.clear()``), so revalidate immediately before persisting to
        guarantee only records satisfying every invariant are written to disk.
        """

        return self.model_type.model_validate(record.model_dump(mode="python", by_alias=True))

    def _resolve_target(self, record: RecordT) -> tuple[Path, str]:
        record_data = record.model_dump(mode="python")
        session_id = str(record_data.get("session_id") or record_data.get("id") or "")
        if not is_canonical_id(session_id, EntityType.SESSION):
            raise ValueError("record must expose a canonical session_id or be a session record")
        record_id = str(record_data.get("id") or "")
        return self._path(session_id, record_id), record_id

    def _write_temp(self, directory: Path, record: RecordT) -> str:
        payload = record.model_dump_json(by_alias=True, indent=2).encode("utf-8") + b"\n"
        fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink(tmp_name)
            raise
        return tmp_name

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        except OSError:
            # Some filesystems disallow directory fsync; the record itself is already durable.
            pass
        finally:
            os.close(dir_fd)

    def add(self, record: RecordT) -> RecordT:
        record = self._validated(record)
        target, record_id = self._resolve_target(record)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_name = self._write_temp(target.parent, record)
        try:
            # os.link publishes the fully written payload atomically and fails if the target
            # already exists, preserving create-once semantics without a partial-write window.
            os.link(tmp_name, target)
        except FileExistsError as exc:
            raise RecordAlreadyExists(record_id) from exc
        finally:
            os.unlink(tmp_name)
        self._fsync_dir(target.parent)
        return record

    def update(self, record: RecordT) -> RecordT:
        """Atomically replace an existing record with an advanced version.

        Used to durably transition mutable metadata (for example a session's ``status`` and
        ``updated_at``). Raises :class:`RecordNotFound` when no record exists to update.
        """

        record = self._validated(record)
        target, record_id = self._resolve_target(record)
        if not target.exists():
            raise RecordNotFound(record_id)
        tmp_name = self._write_temp(target.parent, record)
        try:
            os.replace(tmp_name, target)
        except BaseException:
            os.unlink(tmp_name)
            raise
        self._fsync_dir(target.parent)
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
        if not is_canonical_id(record_id, self.entity_type):
            return False
        return any(self.root.glob(f"*/{self.namespace}/{record_id}.json"))
