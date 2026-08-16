from __future__ import annotations

from pathlib import Path

from agentic_analytics.models import (
    AnalysisSession,
    ValidationRun,
    ValidationRunStatus,
    ValidationSeverity,
)
from agentic_analytics.repositories import (
    EvidenceRepository,
    FindingRepository,
    SourceRepository,
    ValidationRunRepository,
)
from agentic_analytics.validators.core import DEFAULT_VALIDATORS, ValidationContext, Validator


class ValidationService:
    def __init__(
        self,
        evidence: EvidenceRepository,
        sources: SourceRepository,
        findings: FindingRepository,
        runs: ValidationRunRepository,
        validators: tuple[Validator, ...] = DEFAULT_VALIDATORS,
    ) -> None:
        self.evidence = evidence
        self.sources = sources
        self.findings = findings
        self.runs = runs
        self.validators = validators

    def validate(
        self,
        session: AnalysisSession,
        *,
        claim_texts: list[str] | None = None,
        checks: list[str] | None = None,
    ) -> tuple[ValidationRun, list[object]]:
        selected = set(checks or [validator.name for validator in self.validators])
        context = ValidationContext(
            session=session,
            evidence=self.evidence.list(session.id),
            sources=self.sources.list(session.id),
            workspace_root=Path(session.workspace_root).resolve(strict=True),
            claim_texts=claim_texts or [],
        )
        all_findings = []
        checks_run: list[str] = []
        checks_skipped: list[str] = []
        checks_inconclusive: list[dict[str, str]] = []

        for validator in self.validators:
            if validator.name not in selected:
                checks_skipped.append(validator.name)
                continue
            result = validator.check(context)
            if result.outcome == "inconclusive":
                checks_inconclusive.append(
                    {"check": validator.name, "reason": result.reason or "inconclusive"}
                )
            else:
                checks_run.append(validator.name)
            all_findings.extend(result.findings)

        persisted = [self.findings.add(finding) for finding in all_findings]
        severities = {finding.severity for finding in persisted}
        if ValidationSeverity.BLOCKING in severities:
            status = ValidationRunStatus.BLOCKED
        elif severities & {ValidationSeverity.ERROR, ValidationSeverity.WARNING}:
            status = ValidationRunStatus.WARNINGS
        else:
            status = ValidationRunStatus.VALIDATED
        run = self.runs.add(
            ValidationRun(
                session_id=session.id,
                status=status,
                finding_ids=[finding.id for finding in persisted],
                checks_run=checks_run,
                checks_skipped=checks_skipped,
                checks_inconclusive=checks_inconclusive,
            )
        )
        return run, persisted
