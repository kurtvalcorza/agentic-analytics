from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb

from agentic_analytics.models import AnalysisSession, DataSource, SourceKind
from agentic_analytics.repositories import SourceRepository
from agentic_analytics.settings import Settings

from .preview import cell_bound_expr, json_value
from .workspace import WorkspaceService


class SourceInspectionError(ValueError):
    pass


def fingerprint_file(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    stat = path.stat()
    return {"sha256": digest.hexdigest(), "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


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
            return "read_csv(?, strict_mode = true)"
        if kind is SourceKind.PARQUET:
            return "read_parquet(?)"
        raise ValueError(f"unsupported inspectable source kind: {kind}")

    def inspect(
        self, session: AnalysisSession, source_path: str, sample_rows: int = 20
    ) -> tuple[DataSource, dict[str, Any]]:
        path = self.workspace.resolve_file(session.workspace_root, source_path)
        if path.stat().st_size == 0:
            raise SourceInspectionError("source is empty")
        suffix = path.suffix.lower()
        if suffix not in {".csv", ".parquet"}:
            raise SourceInspectionError("only CSV and Parquet sources are supported")
        kind = SourceKind.CSV if suffix == ".csv" else SourceKind.PARQUET
        relative_path = self.workspace.relative_to_workspace(session.workspace_root, path)
        fingerprint = fingerprint_file(path)
        try:
            schema, profile = self._profile(path, kind, sample_rows)
        except duckdb.Error as exc:
            raise SourceInspectionError(f"source could not be inspected: {path.name}") from exc
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
            schema=schema,
            row_count=profile["row_count"],
            profile={
                "null_counts": profile["null_counts"],
                "duplicate_row_count": profile["duplicate_row_count"],
                "profile_truncated": profile["profile_truncated"],
            },
        )
        self.sources.add(source)
        return source, profile

    def _preview_schema(
        self, schema: list[dict[str, Any]], null_counts: dict[str, int]
    ) -> tuple[list[dict[str, Any]], dict[str, int], bool]:
        """Bound response metadata while leaving the canonical source schema intact."""
        shown: list[dict[str, Any]] = []
        shown_nulls: dict[str, int] = {}
        budget = self.settings.max_result_preview_bytes
        column_limit = min(512, budget // 64)
        for column in schema[:column_limit]:
            candidate = {**column, "type": column["type"][:256]}
            next_nulls = dict(shown_nulls)
            if column["name"] in null_counts:
                next_nulls[column["name"]] = null_counts[column["name"]]
            metadata = {"schema": [*shown, candidate], "null_counts": next_nulls}
            if len(json.dumps(metadata).encode("utf-8")) > budget:
                break
            shown.append(candidate)
            shown_nulls = next_nulls
        return shown, shown_nulls, shown != schema

    def _sample(
        self,
        connection: duckdb.DuckDBPyConnection,
        relation: str,
        path: Path,
        schema: list[dict[str, Any]],
        sample_limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        if not schema:
            return [], True
        budget = self.settings.max_result_preview_bytes
        cell_cap = min(self.settings.max_result_cell_chars, max(16, budget // len(schema) // 2))
        projection = ", ".join(
            cell_bound_expr(column["name"], column["type"], cell_cap) for column in schema
        )
        cursor = connection.execute(
            f"SELECT {projection} FROM {relation} LIMIT {sample_limit}", [str(path)]
        )
        sample: list[dict[str, Any]] = []
        truncated = False
        while (raw := cursor.fetchone()) is not None:
            row = {}
            for column, value in zip(schema, raw, strict=True):
                value = json_value(value)
                if isinstance(value, str) and len(value) > cell_cap:
                    value = value[:cell_cap]
                    truncated = True
                row[column["name"]] = value
            if len(json.dumps([*sample, row]).encode("utf-8")) > budget:
                truncated = True
                if sample:
                    break
                # The first row may need a further byte cap (UTF-8/JSON escaping and keys).
                while len(json.dumps([row]).encode("utf-8")) > budget:
                    key = max(
                        (key for key, value in row.items() if isinstance(value, str) and value),
                        key=lambda key: len(row[key]),
                        default=None,
                    )
                    if key is None:
                        return [], True
                    row[key] = row[key][: len(row[key]) // 2]
            sample.append(row)
        return sample, truncated

    def _profile(
        self, path: Path, kind: SourceKind, sample_rows: int
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        sample_limit = min(max(sample_rows, 1), self.settings.max_sample_rows)
        relation = self._relation(kind)
        connection = duckdb.connect(database=":memory:")
        try:
            connection.execute("SET memory_limit = ?", [self.settings.query_memory_limit])
            cursor = connection.execute(f"SELECT * FROM {relation} LIMIT 0", [str(path)])
            schema = [
                {"name": str(column[0]), "type": str(column[1]), "nullable": True}
                for column in (cursor.description or [])
            ]
            # Compute row count and every column's null count in a single scan of the source
            # instead of one aggregate query per column (previously up to max_profile_columns
            # full scans), which made wide files effectively un-inspectable.
            profiled = schema[: self.settings.max_profile_columns]
            select_parts = ["count(*) AS __row_count"]
            for index, item in enumerate(profiled):
                quoted = '"' + str(item["name"]).replace('"', '""') + '"'
                select_parts.append(f"count(*) FILTER (WHERE {quoted} IS NULL) AS __null_{index}")
            aggregate = connection.execute(
                f"SELECT {', '.join(select_parts)} FROM {relation}", [str(path)]
            ).fetchone()
            row_count = int(aggregate[0]) if aggregate else 0
            null_counts: dict[str, int] = {
                str(item["name"]): int(aggregate[index + 1]) if aggregate else 0
                for index, item in enumerate(profiled)
            }
            distinct = connection.execute(
                f"SELECT count(*) FROM (SELECT DISTINCT * FROM {relation})", [str(path)]
            ).fetchone()
            distinct_count = int(distinct[0] if distinct else 0)
            shown_schema, shown_nulls, metadata_truncated = self._preview_schema(
                schema, null_counts
            )
            # Use canonical types in SQL; the displayed type itself may have been clipped.
            sample_schema = schema[: len(shown_schema)]
            sample, cell_truncated = self._sample(
                connection, relation, path, sample_schema, sample_limit
            )
            return schema, {
                "schema": shown_schema,
                "row_count": row_count,
                "null_counts": shown_nulls,
                "duplicate_row_count": row_count - distinct_count,
                "sample": sample,
                "sample_truncated": (
                    row_count > len(sample) or cell_truncated or len(sample_schema) < len(schema)
                ),
                "profile_truncated": (
                    metadata_truncated or len(schema) > self.settings.max_profile_columns
                ),
            }
        finally:
            connection.close()
