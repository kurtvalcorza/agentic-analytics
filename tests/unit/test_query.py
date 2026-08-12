from pathlib import Path
import pytest
from agentic_analytics.services.query import query

def test_query_is_bounded_and_read_only(workspace: Path):
    result = query("SELECT * FROM source('src_test')", {"src_test": workspace / "survey.csv"}, 2)
    assert result["truncated"] and len(result["rows"]) == 2
    with pytest.raises(ValueError): query("DELETE FROM source('src_test')", {"src_test": workspace / "survey.csv"})
