from pathlib import Path

import pytest

from agentic_analytics.models import (
    AnalysisSession,
    DataSource,
    EvidenceClassification,
    SourceKind,
)
from agentic_analytics.runtime import Runtime
from agentic_analytics.services.validation import ValidationRequestError
from agentic_analytics.settings import Settings


def _runtime(tmp_path: Path, workspace: Path) -> Runtime:
    return Runtime.create(
        Settings(
            state_dir=tmp_path / "state",
            allowed_workspace_roots=[workspace],
            execution_backend="subprocess_dev",
        )
    )


def _session(runtime: Runtime, workspace: Path, **metadata: object) -> AnalysisSession:
    session = AnalysisSession(workspace_root=str(workspace), metadata=metadata)
    runtime.sessions.add(session)
    return session


def test_unknown_check_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime = _runtime(tmp_path, workspace)
    session = _session(runtime, workspace)

    with pytest.raises(ValidationRequestError, match="unknown validation checks"):
        runtime.validation.validate(session, checks=["typo"])


def test_inconclusive_only_run_is_not_validated(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime = _runtime(tmp_path, workspace)
    session = _session(runtime, workspace)

    run, findings = runtime.validation.validate(session)
    assert findings == []
    # No claims and no sources: every check is inconclusive, so this must not read as a pass.
    assert run.status.value == "warnings"
    assert run.checks_inconclusive


def test_boolean_denominator_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime = _runtime(tmp_path, workspace)
    session = _session(runtime, workspace)
    source = runtime.sources.add(
        DataSource(
            session_id=session.id,
            kind=SourceKind.CSV,
            display_name="d.csv",
            relative_path="d.csv",
            fingerprint={"sha256": "a" * 64},
        )
    )
    runtime.evidence_ledger.register(
        session.id,
        EvidenceClassification.SOURCE_FACT,
        "Adoption rate was reported.",
        source_ids=[source.id],
        value={"denominator": True, "numerator": 1},
    )

    _, findings = runtime.validation.validate(session, checks=["denominator_consistency"])
    assert any(finding.code == "INVALID_DENOMINATOR" for finding in findings)


def test_registered_causal_design_allows_causal_claims(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime = _runtime(tmp_path, workspace)
    session = _session(runtime, workspace, causal_design=True)

    _, findings = runtime.validation.validate(
        session,
        claim_texts=["Training caused higher adoption."],
        checks=["causal_language"],
    )
    assert all(finding.code != "UNSUPPORTED_CAUSAL_CLAIM" for finding in findings)


def test_causal_gerund_is_detected(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime = _runtime(tmp_path, workspace)
    session = _session(runtime, workspace, analysis_design="observational")

    _, findings = runtime.validation.validate(
        session,
        claim_texts=["Training is causing higher adoption."],
        checks=["causal_language"],
    )
    assert any(finding.code == "UNSUPPORTED_CAUSAL_CLAIM" for finding in findings)


def test_evidence_derived_causal_finding_links_evidence(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime = _runtime(tmp_path, workspace)
    session = _session(runtime, workspace, analysis_design="observational")
    source = runtime.sources.add(
        DataSource(
            session_id=session.id,
            kind=SourceKind.CSV,
            display_name="d.csv",
            relative_path="d.csv",
            fingerprint={"sha256": "a" * 64},
        )
    )
    evidence = runtime.evidence_ledger.register(
        session.id,
        EvidenceClassification.SOURCE_FACT,
        "Training caused higher adoption.",
        source_ids=[source.id],
    )

    _, findings = runtime.validation.validate(session, checks=["causal_language"])
    causal = [f for f in findings if f.code == "UNSUPPORTED_CAUSAL_CLAIM"]
    assert causal
    assert {"type": "evidence", "id": evidence.id} in causal[0].entity_refs


def test_missing_source_blocks_without_aborting(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    data = workspace / "data.csv"
    data.write_text("id,value\n1,2\n", encoding="utf-8")
    runtime = _runtime(tmp_path, workspace)
    session = _session(runtime, workspace)
    runtime.inspector.inspect(session, "data.csv")
    data.unlink()

    run, findings = runtime.validation.validate(session)
    assert run.status.value == "blocked"
    assert any(finding.code == "SOURCE_MISSING" for finding in findings)


def test_duplicate_keys_are_reachable_via_request(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "data.csv").write_text("id,value\n1,a\n1,b\n2,c\n", encoding="utf-8")
    runtime = _runtime(tmp_path, workspace)
    session = _session(runtime, workspace)
    runtime.inspector.inspect(session, "data.csv")

    _, findings = runtime.validation.validate(
        session,
        checks=["duplicates"],
        duplicate_keys={"data.csv": ["id"]},
    )
    assert any(finding.code == "DUPLICATE_KEYS" for finding in findings)
