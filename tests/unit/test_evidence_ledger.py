from datetime import UTC, datetime
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
from agentic_analytics.repositories import (
    ArtifactRepository,
    EvidenceRepository,
    ExecutionRepository,
    SessionScopeError,
    SourceRepository,
)
from agentic_analytics.services.evidence_ledger import (
    EvidenceLedger,
    EvidenceRegistrationError,
)


def _ledger(tmp_path: Path) -> tuple[
    EvidenceLedger, SourceRepository, ExecutionRepository
]:
    sources = SourceRepository(tmp_path)
    executions = ExecutionRepository(tmp_path)
    ledger = EvidenceLedger(
        EvidenceRepository(tmp_path),
        sources,
        executions,
        ArtifactRepository(tmp_path),
    )
    return ledger, sources, executions


def _source(session_id: str) -> DataSource:
    return DataSource(
        session_id=session_id,
        kind=SourceKind.CSV,
        display_name="data.csv",
        relative_path="data.csv",
        fingerprint={"sha256": "a" * 64, "size": 10},
    )


def _execution(session_id: str, source_id: str, status: ExecutionStatus) -> ExecutionRecord:
    return ExecutionRecord(
        session_id=session_id,
        execution_type=ExecutionType.MANAGED_SQL,
        status=status,
        request={"sql": "select 1"},
        source_ids=[source_id],
        source_fingerprints={source_id: {"sha256": "a" * 64}},
        completed_at=datetime.now(UTC),
    )


def test_derived_fact_requires_successful_execution(tmp_path: Path) -> None:
    ledger, sources, executions = _ledger(tmp_path)
    session = AnalysisSession(workspace_root=str(tmp_path))
    source = sources.add(_source(session.id))
    failed = executions.add(_execution(session.id, source.id, ExecutionStatus.FAILED))

    with pytest.raises(EvidenceRegistrationError):
        ledger.register(
            session.id,
            EvidenceClassification.DERIVED_FACT,
            "The observed mean was 4.2.",
            source_ids=[source.id],
            execution_ids=[failed.id],
        )


def test_interpretation_requires_same_session_upstream_evidence(tmp_path: Path) -> None:
    ledger, sources, executions = _ledger(tmp_path)
    session_a = AnalysisSession(workspace_root=str(tmp_path))
    session_b = AnalysisSession(workspace_root=str(tmp_path))
    source = sources.add(_source(session_a.id))
    execution = executions.add(
        _execution(session_a.id, source.id, ExecutionStatus.SUCCEEDED)
    )
    fact = ledger.register(
        session_a.id,
        EvidenceClassification.DERIVED_FACT,
        "The observed mean was 4.2.",
        source_ids=[source.id],
        execution_ids=[execution.id],
    )
    interpretation = ledger.register(
        session_a.id,
        EvidenceClassification.INTERPRETATION,
        "The sample suggests moderate uptake.",
        evidence_ids=[fact.id],
    )
    assert interpretation.evidence_ids == [fact.id]

    with pytest.raises(SessionScopeError):
        ledger.register(
            session_b.id,
            EvidenceClassification.INTERPRETATION,
            "Cross-session evidence must fail.",
            evidence_ids=[fact.id],
        )


def test_derived_fact_source_must_be_used_by_execution(tmp_path: Path) -> None:
    ledger, sources, executions = _ledger(tmp_path)
    session = AnalysisSession(workspace_root=str(tmp_path))
    used = sources.add(_source(session.id))
    other = sources.add(_source(session.id))
    execution = executions.add(_execution(session.id, used.id, ExecutionStatus.SUCCEEDED))

    # Citing `other` alongside an execution that only used `used` breaks the lineage.
    with pytest.raises(EvidenceRegistrationError, match="unlinked sources"):
        ledger.register(
            session.id,
            EvidenceClassification.DERIVED_FACT,
            "The observed mean was 4.2.",
            source_ids=[used.id, other.id],
            execution_ids=[execution.id],
        )


def test_interpretation_rejects_recommendation_input(tmp_path: Path) -> None:
    ledger, sources, executions = _ledger(tmp_path)
    session = AnalysisSession(workspace_root=str(tmp_path))
    source = sources.add(_source(session.id))
    execution = executions.add(_execution(session.id, source.id, ExecutionStatus.SUCCEEDED))
    fact = ledger.register(
        session.id,
        EvidenceClassification.DERIVED_FACT,
        "The observed mean was 4.2.",
        source_ids=[source.id],
        execution_ids=[execution.id],
    )
    recommendation = ledger.register(
        session.id,
        EvidenceClassification.RECOMMENDATION,
        "Increase the sample size.",
        evidence_ids=[fact.id],
    )

    with pytest.raises(EvidenceRegistrationError, match="recommendations rejected"):
        ledger.register(
            session.id,
            EvidenceClassification.INTERPRETATION,
            "This builds on a recommendation.",
            evidence_ids=[recommendation.id],
        )
