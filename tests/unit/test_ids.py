import pytest

from agentic_analytics.ids import EntityType, is_canonical_id, new_id


def test_new_id_is_typed_opaque_and_unique() -> None:
    first = new_id(EntityType.SESSION)
    second = new_id(EntityType.SESSION)
    assert first != second
    assert is_canonical_id(first, EntityType.SESSION)
    assert not is_canonical_id(first, EntityType.SOURCE)


def test_unknown_entity_prefix_rejected() -> None:
    with pytest.raises(ValueError):
        new_id("unknown")
