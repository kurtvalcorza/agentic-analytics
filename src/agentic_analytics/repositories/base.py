"""Append-oriented, session-scoped JSON repositories."""

import json
import os
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class Repository(Generic[T]):
    def __init__(self, root: Path, filename: str, model: type[T]) -> None:
        self.root, self.filename, self.model = root, filename, model

    def _path(self, session_id: str) -> Path:
        path = self.root / "sessions" / session_id / self.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def add(self, item: T) -> T:
        session_id = getattr(item, "session_id", None) or getattr(item, "id")
        path = self._path(session_id)
        if any(old.id == item.id for old in self.list(session_id)):
            raise ValueError(f"immutable record already exists: {item.id}")
        line = item.model_dump_json(by_alias=True) + "\n"
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        return item

    def list(self, session_id: str) -> list[T]:
        path = self._path(session_id)
        if not path.exists():
            return []
        return [self.model.model_validate_json(line) for line in path.read_text().splitlines() if line]

    def get(self, session_id: str, entity_id: str) -> T:
        for item in self.list(session_id):
            if item.id == entity_id:
                return item
        raise KeyError(entity_id)


class SessionRepository:
    def __init__(self, root: Path, model: type[T]) -> None:
        self.root, self.model = root, model

    def add(self, item: T) -> T:
        directory = self.root / "sessions" / item.id
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "generated").mkdir()
        path = directory / "manifest.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(item.model_dump_json(indent=2))
        os.replace(temporary, path)
        return item

    def get(self, session_id: str) -> T:
        path = self.root / "sessions" / session_id / "manifest.json"
        if not path.is_file():
            raise KeyError(session_id)
        return self.model.model_validate_json(path.read_text())

