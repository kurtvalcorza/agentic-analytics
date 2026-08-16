import pytest
from pydantic import ValidationError

from agentic_analytics.ids import EntityType, new_id
from agentic_analytics.models import (
    AnalysisSession,
    DataSource,
    EvidenceClassification,
    EvidenceItem,
    SessionStatus,
    SourceKind,
)
from agentic_analytics.models.common import utc_now
from agentic_analytics.repositories import (
    EvidenceRepository,
    RecordAlreadyExists,
    RecordNotFound,
    SessionRepository,
    SessionScopeError,
    SourceRepository,
    require_session_scope,
)


def test_repository_is_create_once_and_session_scoped(state_root) -> None:
    repo = SourceRepository(state_root)
    session_a = new_id(EntityType.SESSION)
    session_b = new_id(EntityType.SESSION)
    source = DataSource(
        session_id=session_a,
        kind=SourceKind.CSV,
        display_name="data.csv",
        relative_path="data.csv",
        fingerprint={"sha256": "0" * 64},
    )

    repo.add(source)
    loaded = repo.get(session_a, source.id)
    assert loaded == source

    with pytest.raises(RecordAlreadyExists):
        repo.add(source)

    with pytest.raises(SessionScopeError):
        repo.get(session_b, source.id)


def test_repository_rejects_noncanonical_session_scope(state_root) -> None:
    repo = SourceRepository(state_root)
    source_id = new_id(EntityType.SOURCE)

    with pytest.raises(ValueError, match="canonical ses_"):
        repo.get("../outside", source_id)


def test_repository_rejects_wrong_entity_prefix(state_root) -> None:
    repo = SourceRepository(state_root)
    session_id = new_id(EntityType.SESSION)

    with pytest.raises(ValueError, match="canonical src_"):
        repo.get(session_id, new_id(EntityType.EVIDENCE))


def test_session_status_transition_is_durable(state_root) -> None:
    repo = SessionRepository(state_root)
    session = AnalysisSession(workspace_root="/tmp/workspace")
    repo.add(session)

    advanced = session.model_copy(
        update={"status": SessionStatus.COMPLETED, "updated_at": utc_now()}
    )
    repo.update(advanced)

    loaded = repo.get(session.id, session.id)
    assert loaded.status is SessionStatus.COMPLETED
    assert loaded.updated_at >= session.updated_at


def test_update_requires_existing_record(state_root) -> None:
    repo = SessionRepository(state_root)
    session = AnalysisSession(workspace_root="/tmp/workspace")

    with pytest.raises(RecordNotFound):
        repo.update(session)


def test_add_revalidates_mutated_record(state_root) -> None:
    repo = EvidenceRepository(state_root)
    item = EvidenceItem(
        session_id=new_id(EntityType.SESSION),
        classification=EvidenceClassification.SOURCE_FACT,
        claim="There are 10 rows.",
        source_ids=[new_id(EntityType.SOURCE)],
    )
    item.source_ids.clear()

    with pytest.raises(ValidationError):
        repo.add(item)


def test_reference_scope_rejects_record_owned_by_other_session() -> None:
    requested_session = new_id(EntityType.SESSION)
    source = DataSource(
        session_id=new_id(EntityType.SESSION),
        kind=SourceKind.CSV,
        display_name="other.csv",
        relative_path="other.csv",
        fingerprint={"sha256": "0" * 64},
    )

    with pytest.raises(SessionScopeError):
        require_session_scope(requested_session, source)
