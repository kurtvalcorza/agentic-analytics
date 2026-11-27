"""C6: data-plane error text that reaches clients never names a server path."""

from agentic_analytics.dataplane.client import DataPlaneError, _absolute_strings, redact_paths


def test_redact_names_files_and_hides_roots() -> None:
    message = (
        "IO Error: No files found that match the pattern "
        '"/srv/ws/sales/q1.csv" under /srv/state/tmp/dataplane/query-abc/duckdb'
    )

    public = redact_paths(
        message,
        files=["/srv/ws/sales/q1.csv"],
        roots=["/srv/ws", "/srv/state", "/srv/state/tmp/dataplane/query-abc"],
    )

    assert public == (
        'IO Error: No files found that match the pattern "q1.csv" under <path>/duckdb'
    )


def test_redact_matches_sql_quoted_paths() -> None:
    # DuckDB echoes SQL literals with single quotes doubled.
    message = "Binder Error: read_csv('/srv/ws/o''brien.csv', strict_mode = true)"

    public = redact_paths(message, files=["/srv/ws/o'brien.csv"], roots=["/srv/ws"])

    assert "/srv/ws" not in public
    assert "read_csv('o'brien.csv'" in public


def test_redact_leaves_messages_without_paths_alone() -> None:
    message = 'Binder Error: Referenced column "missing" not found in FROM clause!'

    assert redact_paths(message, files=["/srv/ws/a.csv"], roots=["/srv/ws", "/"]) == message


def test_absolute_strings_walks_nested_payloads() -> None:
    payload = {
        "sql": "SELECT 1",
        "sources": [{"view": "v", "kind": "csv", "path": "/srv/ws/a.csv"}],
        "spill_path": "/srv/state/artifacts/x/result.parquet",
        "limits": {"threads": 2, "relative": "not/absolute"},
    }

    assert sorted(_absolute_strings(payload)) == [
        "/srv/state/artifacts/x/result.parquet",
        "/srv/ws/a.csv",
    ]


def test_public_message_is_str_and_raw_text_is_only_detail() -> None:
    error = DataPlaneError("sql", "BinderException", "public text", detail="raw /srv/ws/a.csv")

    assert str(error) == "public text"
    assert error.detail == "raw /srv/ws/a.csv"
    assert "/srv" not in repr(error.args)
