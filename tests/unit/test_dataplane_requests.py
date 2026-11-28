"""C10: typed worker requests, strict decoding, and sandbox grants from declared paths."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentic_analytics.dataplane import DataPlaneClient
from agentic_analytics.dataplane.requests import (
    REQUEST_TYPES,
    EngineLimits,
    ProfileRequest,
    QueryRequest,
    RequestError,
    SourceView,
    TableStatsRequest,
    decode_request,
)
from agentic_analytics.settings import Settings

LIMITS = EngineLimits(timeout_seconds=5, memory_limit="256MB", preview_bytes=4096, cell_chars=64)
QUERY = QueryRequest(
    limits=LIMITS,
    sources=(
        SourceView(view="a", kind="csv", path="/data/a.csv"),
        SourceView(view="b", kind="parquet", path="/data/b.parquet"),
    ),
    sql='SELECT * FROM "a"',
    result_table="r",
    limit=10,
    spill_cap=100,
    spill_path="/state/archive/s/e/query-result.parquet",
    spill_byte_ceiling=1024,
    require_complete=True,
)
PROFILE = ProfileRequest(
    limits=LIMITS, path="/data/a.csv", kind="csv", sample_limit=5, max_profile_columns=10
)
STATS = TableStatsRequest(
    limits=LIMITS, path="/data/a.csv", kind="csv", distinct=True, duplicate_keys=("id",)
)


@pytest.mark.parametrize("request_", [QUERY, PROFILE, STATS], ids=lambda r: r.op)
def test_requests_round_trip_through_json(request_: Any) -> None:
    wire = json.loads(json.dumps(request_.to_json()))

    assert decode_request(request_.op, wire) == request_


def test_every_operation_has_a_request_type_and_an_engine_entry() -> None:
    from agentic_analytics.dataplane.engine import OPERATIONS

    assert set(OPERATIONS) == set(REQUEST_TYPES) == {"query", "profile", "table_stats"}


def test_declared_paths_cover_exactly_what_each_operation_touches() -> None:
    assert QUERY.read_paths() == ("/data/a.csv", "/data/b.parquet")
    assert QUERY.write_dirs() == ("/state/archive/s/e",)
    assert PROFILE.read_paths() == ("/data/a.csv",)
    assert PROFILE.write_dirs() == STATS.write_dirs() == ()


def test_sandbox_grants_come_only_from_the_declared_paths(tmp_path: Path) -> None:
    client = DataPlaneClient(Settings(state_dir=tmp_path / "state"))
    scratch = tmp_path / "scratch"

    query = client.sandbox_policy(QUERY, scratch)
    profile = client.sandbox_policy(PROFILE, scratch)

    assert query.read_files == QUERY.read_paths()
    assert query.write_dirs == (str(scratch), "/state/archive/s/e")
    assert profile.read_files == ("/data/a.csv",)
    assert profile.write_dirs == (str(scratch),)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"attack": "x"}, "unknown fields"),
        ({"sample_limit": True}, "sample_limit must be an integer"),
        ({"sample_limit": "5"}, "sample_limit must be an integer"),
        ({"path": "relative.csv"}, "absolute path"),
        ({"limits": {"timeout_seconds": 5}}, "limits is missing"),
        ({"limits": None}, "limits must be an object"),
    ],
)
def test_malformed_profile_requests_are_rejected(change: dict[str, Any], message: str) -> None:
    payload = {**PROFILE.to_json(), **change}

    with pytest.raises(RequestError, match=message):
        decode_request("profile", payload)


def test_missing_fields_unknown_ops_and_bad_lists_are_rejected() -> None:
    payload = PROFILE.to_json()
    del payload["kind"]
    with pytest.raises(RequestError, match="missing"):
        decode_request("profile", payload)
    with pytest.raises(RequestError, match="unknown operation"):
        decode_request("drop_table", PROFILE.to_json())
    with pytest.raises(RequestError, match="duplicate_keys"):
        decode_request("table_stats", {**STATS.to_json(), "duplicate_keys": [1]})
    with pytest.raises(RequestError, match="sources must be a list"):
        decode_request("query", {**QUERY.to_json(), "sources": {"path": "/x"}})
    with pytest.raises(RequestError, match="absolute path"):
        decode_request(
            "query", {**QUERY.to_json(), "sources": [{"view": "v", "kind": "csv", "path": "x"}]}
        )


def test_the_worker_rejects_a_malformed_request_before_dispatch(tmp_path: Path) -> None:
    envelope = {
        "op": "profile",
        "payload": {**PROFILE.to_json(), "path": "relative.csv"},
        "os_limits": {},
        "sandbox": {"mode": "auto", "write_dirs": [str(tmp_path)]},
        "parent": {},
    }
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "agentic_analytics.dataplane.worker"],
        input=json.dumps(envelope).encode(),
        capture_output=True,
        timeout=60,
        cwd=tmp_path,
    )

    response = json.loads(completed.stdout)
    assert completed.returncode == 2, completed.stderr
    assert response["error"]["category"] == "invalid_request"
    assert response["error"]["message"] == "data-plane worker rejected a malformed request"
