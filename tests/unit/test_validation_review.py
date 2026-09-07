from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentic_analytics.models import (
    AnalysisSession,
    DataSource,
    EvidenceClassification,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionType,
    SourceKind,
)
from agentic_analytics.runtime import Runtime
from agentic_analytics.services.inspector import fingerprint_file
from agentic_analytics.services.workspace import WorkspaceAuthorizationError
from agentic_analytics.settings import Settings
from agentic_analytics.validators.core import CheckResult, ValidationContext


def _setup(tmp_path: Path) -> tuple[Runtime, AnalysisSession, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data = workspace / "data.csv"
    data.write_text("id,value\n1,2\n", encoding="utf-8")
    runtime = Runtime.create(Settings(
        state_dir=tmp_path / "state", allowed_workspace_roots=[workspace],
        execution_backend="subprocess_dev",
    ))
    session = runtime.sessions.add(AnalysisSession(workspace_root=str(workspace)))
    return runtime, session, data


def _revisions(
    runtime: Runtime, session: AnalysisSession, data: Path
) -> tuple[DataSource, DataSource]:
    now = datetime.now(UTC)
    # Force IDs to sort opposite registration time. Repository listing order must not
    # determine which version is current.
    old = runtime.sources.add(DataSource(
        id="src_" + "f" * 32, session_id=session.id, kind=SourceKind.CSV,
        display_name=data.name, relative_path=data.name, fingerprint=fingerprint_file(data),
        registered_at=now - timedelta(days=1),
    ))
    data.write_text("id,value\n1,3\n", encoding="utf-8")
    current = runtime.sources.add(DataSource(
        id="src_" + "0" * 32, session_id=session.id, kind=SourceKind.CSV,
        display_name=data.name, relative_path=data.name, fingerprint=fingerprint_file(data),
        registered_at=now,
    ))
    return old, current


def test_reinspection_supersedes_uncited_source_version(tmp_path: Path) -> None:
    runtime, session, data = _setup(tmp_path)
    old, current = _revisions(runtime, session, data)
    result = runtime.query.execute(session, f"SELECT * FROM source('{current.id}')")
    execution = runtime.executions.get(session.id, result["execution_id"])
    assert execution.status is ExecutionStatus.SUCCEEDED
    run, findings = runtime.validation.validate(session, checks=["stale_sources"])
    assert findings == []
    assert run.status.value == "validated"
    # Historical provenance is retained, even though it no longer blocks new work.
    assert runtime.sources.get(session.id, old.id).fingerprint != current.fingerprint


@pytest.mark.parametrize("link", ["direct", "upstream", "execution"])
def test_cited_historical_source_remains_stale(tmp_path: Path, link: str) -> None:
    runtime, session, data = _setup(tmp_path)
    old, current = _revisions(runtime, session, data)
    if link == "execution":
        execution = runtime.executions.add(ExecutionRecord(
            session_id=session.id, execution_type=ExecutionType.MANAGED_SQL,
            status=ExecutionStatus.SUCCEEDED, request={"sql": "SELECT 1"},
            source_ids=[old.id, current.id], completed_at=datetime.now(UTC),
        ))
        runtime.evidence_ledger.register(
            session.id, EvidenceClassification.DERIVED_FACT, "A result from both sources.",
            source_ids=[current.id], execution_ids=[execution.id],
        )
    else:
        upstream = runtime.evidence_ledger.register(
            session.id, EvidenceClassification.SOURCE_FACT, "The old input value was 2.",
            source_ids=[old.id], material=link == "direct",
        )
        if link == "upstream":
            runtime.evidence_ledger.register(
                session.id, EvidenceClassification.INTERPRETATION, "This supports the claim.",
                evidence_ids=[upstream.id],
            )
    run, findings = runtime.validation.validate(session, checks=["stale_sources"])
    assert run.status.value == "blocked"
    assert [finding.entity_refs for finding in findings] == [[{"type": "source", "id": old.id}]]


def test_malformed_source_preserves_findings_and_incomplete_coverage(tmp_path: Path) -> None:
    runtime, session, data = _setup(tmp_path)
    runtime.inspector.inspect(session, data.name)
    data.write_bytes(b"id,value\n1,\xff\xfe\n")
    run, findings = runtime.validation.validate(session, claim_texts=["An unsupported claim."])
    assert run.status.value == "blocked"
    assert {finding.code for finding in findings} >= {"STALE_SOURCE", "MISSING_MATERIAL_EVIDENCE"}
    incomplete = {item["check"] for item in run.checks_inconclusive}
    assert {"duplicates", "missingness"} <= incomplete
    assert "causal_language" in run.checks_run
    assert set(run.finding_ids) == {finding.id for finding in runtime.findings.list(session.id)}
    assert runtime.validation_runs.get(session.id, run.id) == run


@pytest.mark.parametrize("check,code", [("duplicates", "DUPLICATE_ROWS"),
                                       ("missingness", "HIGH_MISSINGNESS")])
def test_source_failure_preserves_other_sources_in_same_check(
    tmp_path: Path, check: str, code: str
) -> None:
    runtime, session, data = _setup(tmp_path)
    good_ids = set()
    for name, character in [("first.csv", "0"), ("bad.csv", "8"), ("last.csv", "f")]:
        path = data.parent / name
        path.write_bytes(b"id,value\n1,\xff\n" if name == "bad.csv" else b"id,value\n1,\n1,\n")
        source = runtime.sources.add(DataSource(
            id="src_" + character * 32, session_id=session.id, kind=SourceKind.CSV,
            display_name=name, relative_path=name, fingerprint=fingerprint_file(path),
        ))
        if name != "bad.csv":
            good_ids.add(source.id)
    run, findings = runtime.validation.validate(session, checks=[check])
    assert run.status.value == "warnings"
    assert [item["check"] for item in run.checks_inconclusive] == [check]
    assert {
        ref["id"] for finding in findings if finding.code == code for ref in finding.entity_refs
    } == good_ids


def test_validation_reauthorizes_persisted_workspace(tmp_path: Path) -> None:
    runtime, session, _ = _setup(tmp_path)
    runtime.workspace.allowed_roots = [tmp_path / "other"]
    with pytest.raises(WorkspaceAuthorizationError, match="outside authorized roots"):
        runtime.validation.validate(session)
    assert runtime.validation_runs.list(session.id) == []


def test_programming_errors_are_not_reported_as_data_inconclusive(tmp_path: Path) -> None:
    runtime, session, _ = _setup(tmp_path)

    class BrokenValidator:
        name = "broken"

        def check(self, context: ValidationContext) -> CheckResult:
            raise AttributeError("programming defect")

    runtime.validation.validators = (BrokenValidator(),)
    with pytest.raises(AttributeError, match="programming defect"):
        runtime.validation.validate(session)
