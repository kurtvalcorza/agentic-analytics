import re
from pathlib import Path
from typing import Any

import duckdb

from .inspector import relation_sql

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|copy|attach|detach|install|load|call|pragma)\b", re.I)


def query(sql: str, sources: dict[str, Path], max_rows: int = 200) -> dict[str, Any]:
    if not 1 <= max_rows <= 1000 or FORBIDDEN.search(sql) or not re.match(r"^\s*(select|with)\b", sql, re.I):
        raise ValueError("query must be read-only and max_rows must be between 1 and 1000")
    rendered = sql
    for source_id, path in sources.items():
        rendered = rendered.replace(f"source('{source_id}')", relation_sql(path))
    if re.search(r"source\s*\(", rendered, re.I):
        raise PermissionError("query references an unauthorized source")
    db = duckdb.connect(":memory:")
    result = db.execute(f"SELECT * FROM ({rendered}) AS bounded_result LIMIT {max_rows + 1}")
    columns = [x[0] for x in result.description]
    rows = result.fetchall()
    return {"columns": columns, "rows": rows[:max_rows], "row_count_returned": min(len(rows), max_rows), "truncated": len(rows) > max_rows}

