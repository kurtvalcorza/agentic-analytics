import pytest
from pydantic import ValidationError
from agentic_analytics.ids import new_id
from agentic_analytics.models import EvidenceItem

def test_ids_are_typed_and_unique():
    assert new_id("ses").startswith("ses_")
    assert new_id("src") != new_id("src")
    with pytest.raises(ValueError): new_id("bad")

def test_derived_evidence_requires_complete_lineage():
    with pytest.raises(ValidationError):
        EvidenceItem(id="evd_test", session_id="ses_test", classification="derived_fact", claim="x")
