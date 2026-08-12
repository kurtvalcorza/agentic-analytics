from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agentic_analytics.ids import EntityType, new_id
from agentic_analytics.models import (
    AnalysisSession,
    Artifact,
    ArtifactKind,
    DataSource,
    EvidenceClassification,
    EvidenceItem,
    ExecutionRecord,
    ExecutionStatus,
    ExecutionType,
    SessionMode,
    SourceKind,
)


def test_session_normalizes_workspace(workspace) -> None:
    session = AnalysisSession(workspace_root=str(workspace), mode=SessionMode.STRICT)
    assert session.workspace_root == str(workspace.resolve())


def test_source_requires_location() -> None:
    with pytest.raises(ValidationError):
        DataSource(
            session_id=new_id(EntityType.SESSION),
            kind=SourceKind.CSV,
            display_name="data.csv",
            fingerprint={"sha256": "0" * 64},
        )


def test_terminal_execution_requires_completed_at() -> None:
    with pytest.raises(ValidationError):
        ExecutionRecord(
            session_id=new_id(EntityType.SESSION),
            execution_type=ExecutionType.MANAGED_PYTHON,
            status=ExecutionStatus.SUCCEEDED,
            request={"code": "print(1)"},
        )

    record = ExecutionRecord(
        session_id=new_id(EntityType.SESSION),
        execution_type=ExecutionType.MANAGED_PYTHON,
        status=ExecutionStatus.SUCCEEDED,
        request={"code": "print(1)"},
        completed_at=datetime.now(UTC),
    )
    assert record.status.terminal


def test_artifact_rejects_parent_traversal() -> None:
    with pytest.raises(ValidationError):
        Artifact(
            session_id=new_id(EntityType.SESSION),
            kind=ArtifactKind.FILE,
            display_name="escape",
            relative_path="../escape.txt",
            media_type="text/plain",
            size_bytes=1,
            sha256="0" * 64,
        )


def test_evidence_classification_requirements() -> None:
    session_id = new_id(EntityType.SESSION)
    source_id = new_id(EntityType.SOURCE)
    execution_id = new_id(EntityType.EXECUTION)

    source_fact = EvidenceItem(
        session_id=session_id,
        classification=EvidenceClassification.SOURCE_FACT,
        claim="There are 10 rows.",
        source_ids=[source_id],
    )
    assert source_fact.source_ids == [source_id]

    with pytest.raises(ValidationError):
        EvidenceItem(
            session_id=session_id,
            classification=EvidenceClassification.DERIVED_FACT,
            claim="Mean is 5.",
            source_ids=[source_id],
        )

    derived = EvidenceItem(
        session_id=session_id,
        classification=EvidenceClassification.DERIVED_FACT,
        claim="Mean is 5.",
        source_ids=[source_id],
        execution_ids=[execution_id],
    )
    interpretation = EvidenceItem(
        session_id=session_id,
        classification=EvidenceClassification.INTERPRETATION,
        claim="The distribution is centered near 5.",
        evidence_ids=[derived.id],
    )
    assert interpretation.evidence_ids == [derived.id]
