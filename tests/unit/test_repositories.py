import pytest

from agentic_analytics.ids import EntityType, new_id
from agentic_analytics.models import DataSource, SourceKind
from agentic_analytics.repositories import (
    RecordAlreadyExists,
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
