import pytest

from agentic_analytics.ids import EntityType, new_id
from agentic_analytics.models import EvidenceClassification, EvidenceItem, ensure_acyclic_evidence


def test_evidence_dag_accepts_acyclic_graph() -> None:
    session_id = new_id(EntityType.SESSION)
    source_id = new_id(EntityType.SOURCE)
    root = EvidenceItem(
        session_id=session_id,
        classification=EvidenceClassification.SOURCE_FACT,
        claim="Observed fact",
        source_ids=[source_id],
    )
    interpretation = EvidenceItem(
        session_id=session_id,
        classification=EvidenceClassification.INTERPRETATION,
        claim="Interpretation",
        evidence_ids=[root.id],
    )
    ensure_acyclic_evidence([root, interpretation])


def test_evidence_dag_rejects_cycle() -> None:
    session_id = new_id(EntityType.SESSION)
    first_id = new_id(EntityType.EVIDENCE)
    second_id = new_id(EntityType.EVIDENCE)
    first = EvidenceItem(
        id=first_id,
        session_id=session_id,
        classification=EvidenceClassification.INTERPRETATION,
        claim="First",
        evidence_ids=[second_id],
    )
    second = EvidenceItem(
        id=second_id,
        session_id=session_id,
        classification=EvidenceClassification.INTERPRETATION,
        claim="Second",
        evidence_ids=[first_id],
    )
    with pytest.raises(ValueError, match="cycle"):
        ensure_acyclic_evidence([first, second])
