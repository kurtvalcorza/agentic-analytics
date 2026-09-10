import pytest

from agentic_analytics.services.query import QueryRejected, QueryService


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM query('SELECT * FROM duckdb_settings()')",
        "SELECT * FROM query_table('duckdb_settings')",
    ],
)
def test_query_policy_blocks_dynamic_sql_surfaces(sql: str) -> None:
    """Dynamic relation/query helpers must not tunnel around lexical policy checks."""
    with pytest.raises(QueryRejected, match="forbidden"):
        QueryService._validate_sql(sql)
