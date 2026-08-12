import hashlib
from pathlib import Path
from typing import Any

import duckdb


def fingerprint(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    stat = path.stat()
    return {"sha256": digest.hexdigest(), "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def relation_sql(path: Path) -> str:
    escaped = str(path).replace("'", "''")
    if path.suffix.lower() == ".csv":
        return f"read_csv_auto('{escaped}', sample_size=-1)"
    if path.suffix.lower() == ".parquet":
        return f"read_parquet('{escaped}')"
    raise ValueError("only CSV and Parquet sources are supported")


def inspect(path: Path, sample_rows: int = 20) -> dict[str, Any]:
    if not 0 <= sample_rows <= 100:
        raise ValueError("sample_rows must be between 0 and 100")
    db = duckdb.connect(":memory:")
    rel = relation_sql(path)
    description = db.execute(f"DESCRIBE SELECT * FROM {rel}").fetchall()
    columns = [row[0] for row in description]
    row_count = db.execute(f"SELECT count(*) FROM {rel}").fetchone()[0]
    sample = db.execute(f"SELECT * FROM {rel} LIMIT ?", [sample_rows]).fetchall()
    null_expr = ", ".join(f'count(*)-count("{c.replace(chr(34), chr(34)*2)}")' for c in columns)
    nulls = db.execute(f"SELECT {null_expr} FROM {rel}").fetchone() if columns else []
    duplicates = db.execute(f"SELECT count(*)-count(*) FILTER (WHERE n=1) FROM (SELECT count(*) n FROM {rel} GROUP BY ALL)").fetchone()[0] if columns else 0
    return {
        "fingerprint": fingerprint(path),
        "schema": [{"name": x[0], "type": x[1], "nullable": x[2] == "YES"} for x in description],
        "row_count": row_count,
        "profile": {"null_counts": dict(zip(columns, nulls, strict=True)), "duplicate_row_count": duplicates},
        "sample": [dict(zip(columns, row, strict=True)) for row in sample],
        "sample_truncated": row_count > sample_rows,
    }

