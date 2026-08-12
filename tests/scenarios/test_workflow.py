from pathlib import Path

def test_host_neutral_workflow(runtime, workspace: Path):
    session = runtime.create_session(workspace, "permissive")["session_id"]
    source = runtime.inspect_source(session, "survey.csv", sample_rows=2)
    result = runtime.query_data(session, f"SELECT count(*) n FROM source('{source['source_id']}')", 1)
    evidence = runtime.register_evidence(session_id=session, classification="derived_fact",
        claim="There are 3 observations.", material=True, source_ids=[source["source_id"]],
        execution_ids=[result["execution_id"]], artifact_ids=[], evidence_ids=[])
    assert evidence["classification"] == "derived_fact"
    assert runtime.validate_analysis(session, claim_texts=["There are 3 observations."])["status"] in {"warnings", "validated"}

def test_unlinked_and_causal_claims_are_blocked(runtime, workspace: Path):
    session = runtime.create_session(workspace, "permissive")["session_id"]
    result = runtime.validate_analysis(session, claim_texts=["Training caused adoption."])
    assert result["status"] == "blocked"
    assert {x["code"] for x in result["findings"]} == {"MISSING_EVIDENCE", "UNSUPPORTED_CAUSAL_CLAIM"}
