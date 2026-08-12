from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import duckdb

from agentic_analytics.models import AnalysisSession, DataSource, SourceKind
from agentic_analytics.repositories import SourceRepository
from agentic_analytics.settings import Settings

from .workspace import WorkspaceService


def fingerprint_file(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {"sha256": digest.hexdigest(), "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class InspectorService:
    def __init__(
        self, sources: SourceRepository, workspace: WorkspaceService, settings: Settings
    ) -> None:
        self.sources = sources
        self.workspace = workspace
        self.settings = settings

    @staticmethod
    def _relation(kind: SourceKind) -> str:
        if kind is SourceKind.CSV:
            return "read_csv_auto(?)"
        if kind is SourceKind.PARQUET:
            return "read_parquet(?)"
        raise ValueError(f"unsupported inspectable source kind: {kind}")

    def inspect(
        self, session: AnalysisSession, source_path: str, sample_rows: int = 20
    ) -> tuple[DataSource, dict[str, Any]]:
        path = self.workspace.resolve_file(session.workspace_root, source_path)
        suffix = path.suffix.lower()
        if suffix not in {".csv", ".parquet"}:
            raise ValueError("only CSV and Parquet sources are supported")
        kind = SourceKind.CSV if suffix == ".csv" else SourceKind.PARQUET
        relative_path = self.workspace.relative_to_workspace(session.workspace_root, path)
        fingerprint = fingerprint_file(path)
        profile = self._profile(path, kind, sample_rows)
        existing = next(
            (
                item
                for item in self.sources.list(session.id)
                if item.relative_path == relative_path and item.fingerprint == fingerprint
            ),
            None,
        )
        if existing is not None:
            return existing, profile
        source = DataSource(
            session_id=session.id,
            kind=kind,
            display_name=path.name,
            relative_path=relative_path,
            fingerprint=fingerprint,
            schema=profile["schema"],
            row_count=profile["row_count"],
            profile={
                "null_counts": profile["null_counts"],
                "duplicate_row_count": profile["duplicate_row_count"],
                "profile_truncated": profile["profile_truncated"],
            },
        )
        self.sources.add(source)
        return source, profile

    def _profile(self, path: Path, kind: SourceKind, sample_rows: int) -> dict[str, Any]:
        sample_limit = min(max(sample_rows, 1), self.settings.max_sample_rows)
        relation = self._relation(kind)
        connection = duckdb.connect(database=":memory:")
        try:
            cursor = connection.execute(f"SELECT * FROM {relation} LIMIT 0", [str(path)])
            schema = [
                {"name": str(column[0]), "type": str(column[1]), "nullable": True}
                for column in (cursor.description or [])
            ]
            row = connection.execute(f"SELECT count(*) FROM {relation}", [str(path)]).fetchone()
            row_count = int(row[0] if row else 0)
            null_counts: dict[str, int] = {}
            for item in schema[: self.settings.max_profile_columns]:
                name = str(item["name"])
                quoted = '"' + name.replace('"', '""') + '"'
                value = connection.execute(
                    f"SELECT count(*) FILTER (WHERE {quoted} IS NULL) FROM {relation}",
                    [str(path)],
                ).fetchone()
                null_counts[name] = int(value[0] if value else 0)
            distinct = connection.execute(
                f"SELECT count(*) FROM (SELECT DISTINCT * FROM {relation})", [str(path)]
            ).fetchone()
            distinct_count = int(distinct[0] if distinct else 0)
            sample_cursor = connection.execute(
                f"SELECT * FROM {relation} LIMIT {sample_limit + 1}", [str(path)]
            )
            rows = sample_cursor.fetchall()
            sample_truncated = len(rows) > sample_limit
            sample = [
                {schema[index]["name"]: _json_value(value) for index, value in enumerate(row)}
                for row in rows[:sample_limit]
            ]
            return {
                "schema": schema,
                "row_count": row_count,
                "null_counts": null_counts,
                "duplicate_row_count": row_count - distinct_count,
                "sample": sample,
                "sample_truncated": sample_truncated,
                "profile_truncated": len(schema) > self.settings.max_profile_columns,
            }
        finally:
            connection.close()
