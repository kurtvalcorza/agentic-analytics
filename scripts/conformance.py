#!/usr/bin/env python3
"""Host-free protocol service conformance smoke test."""
import tempfile
from pathlib import Path

from agentic_analytics.execution_backends import SubprocessDevelopmentBackend
from agentic_analytics.runtime import AnalyticsRuntime


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp); (root / "survey.csv").write_text("group,positive\nA,true\nA,false\nB,true\n")
        app = AnalyticsRuntime(root / ".state", SubprocessDevelopmentBackend())
        session = app.create_session(root, "permissive")["session_id"]
        print("PASS tool discovery")
        source = app.inspect_source(session, "survey.csv")["source_id"]
        print("PASS source inspection")
        result = app.query_data(session, f"SELECT count(*) n FROM source('{source}')", 2)
        assert result["rows"] == [(3,)]
        print("PASS bounded query")
        execution = app.execute_python(session, "open('result.csv','w').write('x\\n1\\n')", [], 5)
        print("PASS managed execution (development backend)")
        assert execution["artifact_ids"]
        print("PASS artifact registration")
        evd = app.register_evidence(session_id=session, classification="derived_fact", claim="There are 3 rows.",
            material=True, source_ids=[source], execution_ids=[result["execution_id"]], artifact_ids=[], evidence_ids=[])
        assert evd["id"]
        print("PASS evidence lineage")
        assert app.validate_analysis(session, claim_texts=["Unsupported material claim."])["status"] == "blocked"
        print("PASS validation blocker fixture")
        other = app.create_session(root, "permissive")["session_id"]
        try: app.get_artifact(other, execution["artifact_ids"][0]); raise AssertionError("authorization failed")
        except KeyError: pass
        print("PASS cross-session authorization")


if __name__ == "__main__": main()

