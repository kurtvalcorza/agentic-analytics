"""C5: every consumer of the supported source formats derives from one registry."""

import pytest

from agentic_analytics.dataplane import engine
from agentic_analytics.models import READABLE_SOURCE_KINDS, READABLE_SOURCE_SUFFIXES, SourceKind
from agentic_analytics.services.workspace import WorkspaceService
from agentic_analytics.validators import core as validators


def test_discovery_and_validators_follow_the_registry() -> None:
    assert frozenset(READABLE_SOURCE_SUFFIXES) == WorkspaceService.SUPPORTED_SUFFIXES
    assert validators._SUPPORTED_TABULAR == READABLE_SOURCE_KINDS
    assert frozenset(READABLE_SOURCE_SUFFIXES.values()) == READABLE_SOURCE_KINDS


@pytest.mark.parametrize("kind", sorted(READABLE_SOURCE_KINDS))
def test_engine_has_a_reader_for_every_readable_kind(kind: SourceKind) -> None:
    # A format added to the registry without a reader would only fail at query time.
    assert "'/data/x'" in engine.reader_sql(kind.value, "/data/x")


def test_engine_refuses_kinds_outside_the_registry() -> None:
    for kind in set(SourceKind) - READABLE_SOURCE_KINDS:
        with pytest.raises(ValueError):
            engine.reader_sql(kind.value, "/data/x")
