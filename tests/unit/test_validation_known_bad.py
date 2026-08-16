from pathlib import Path

from agentic_analytics.models import (
    AnalysisSession,
    DataSource,
    EvidenceClassification,
    SessionMode,
    SourceKind,
)
from agentic_analytics.runtime import Runtime
from agentic_analytics.services.inspector import fingerprint_file
from agentic_analytics.settings import Settings


def test_known_bad_fixture_triggers_all_v1_validators(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data = workspace / "data.csv"
    data.write_text("id,value\n1,\n1,2\n2,\n", encoding="utf-8")
    runtime = Runtime.create(
        Settings(
            state_dir=tmp_path / "state",
            allowed_workspace_roots=[workspace],
            execution_backend="subprocess_dev",
        )
    )
    session = AnalysisSession(
        workspace_root=str(workspace),
        mode=SessionMode.PERMISSIVE,
        metadata={"analysis_design": "observational"},
    )
    runtime.sessions.add(session)
    source = runtime.sources.add(
        DataSource(
            session_id=session.id,
            kind=SourceKind.CSV,
            display_name="data.csv",
            relative_path="data.csv",
            fingerprint=fingerprint_file(data),
            profile={
                "validation": {
                    "duplicate_keys": ["id"],
                    "missingness_warning_threshold": 0.10,
                    "missingness_error_threshold": 0.30,
                }
            },
        )
    )
    runtime.evidence_ledger.register(
        session.id,
        EvidenceClassification.SOURCE_FACT,
        "Treatment caused improvement.",
        source_ids=[source.id],
        value={"numerator": 5, "denominator": 4},
    )

    data.write_text("id,value\n1,\n1,2\n2,\n3,\n", encoding="utf-8")
    run, findings = runtime.validation.validate(
        session,
        claim_texts=["Treatment caused improvement.", "Unlinked final claim."],
    )

    assert run.status.value == "blocked"
    codes = {finding.code for finding in findings}
    assert {
        "MISSING_MATERIAL_EVIDENCE",
        "STALE_SOURCE",
        "DUPLICATE_KEYS",
        "HIGH_MISSINGNESS",
        "NUMERATOR_EXCEEDS_DENOMINATOR",
        "UNSUPPORTED_CAUSAL_CLAIM",
    } <= codes
