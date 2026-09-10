from __future__ import annotations

from pathlib import Path

from agentic_analytics.models import (
    AnalysisSession,
    DataSource,
    EvidenceItem,
    ValidationFinding,
    ValidationRun,
    ValidationRunStatus,
    ValidationScope,
    ValidationSeverity,
)
from agentic_analytics.repositories import (
    EvidenceRepository,
    ExecutionRepository,
    FindingRepository,
    SourceRepository,
    ValidationRunRepository,
)
from agentic_analytics.validators.core import DEFAULT_VALIDATORS, ValidationContext, Validator

from .workspace import WorkspaceService


class ValidationRequestError(ValueError):
    pass


class ValidationService:
    def __init__(
        self,
        evidence: EvidenceRepository,
        sources: SourceRepository,
        findings: FindingRepository,
        runs: ValidationRunRepository,
        validators: tuple[Validator, ...] = DEFAULT_VALIDATORS,
        *,
        executions: ExecutionRepository,
        workspace: WorkspaceService | None = None,
    ) -> None:
        self.evidence = evidence
        self.sources = sources
        self.findings = findings
        self.runs = runs
        self.validators = validators
        self.executions = executions
        self.workspace = workspace

    def _active_sources(self, session_id: str, evidence: list[EvidenceItem]) -> list[DataSource]:
        sources = self.sources.list(session_id)
        referenced = {source_id for item in evidence for source_id in item.source_ids}
        execution_ids = {execution_id for item in evidence for execution_id in item.execution_ids}
        for execution_id in execution_ids:
            referenced.update(self.executions.get(session_id, execution_id).source_ids)
        # Reinspection supersedes an unused version, but cited historical records remain
        # active, including sources used by executions behind upstream evidence items.
        # Repository order is by random ID and cannot identify the newest registration.
        latest: dict[tuple[str | None, str | None], DataSource] = {}
        for source in sources:
            location = (source.relative_path, source.uri)
            previous = latest.get(location)
            if previous is None or source.registered_at > previous.registered_at:
                latest[location] = source
        return [
            source for source in sources
            if source.id in referenced
            or source.registered_at == latest[(source.relative_path, source.uri)].registered_at
        ]

    def validate(
        self,
        session: AnalysisSession,
        *,
        claim_texts: list[str] | None = None,
        checks: list[str] | None = None,
        duplicate_keys: dict[str, list[str]] | None = None,
        scope: ValidationScope = ValidationScope.FINAL,
    ) -> tuple[ValidationRun, list[ValidationFinding]]:
        known = {validator.name for validator in self.validators}
        if checks is not None:
            unknown = sorted({check for check in checks if check not in known})
            if unknown:
                # Reject unknown check selectors so a typo cannot silently skip every check
                # and report success.
                raise ValidationRequestError(
                    f"unknown validation checks: {', '.join(unknown)}"
                )
            selected = set(checks)
        else:
            selected = known
        if not selected:
            # An explicit empty check set has zero coverage. Rejecting it prevents a
            # request that runs no validator at all from falling through to a clean
            # "validated" verdict.
            raise ValidationRequestError(
                "validation requires at least one check; an empty check set has zero coverage"
            )
        workspace_root = (
            self.workspace.authorize_workspace(session.workspace_root)
            if self.workspace is not None
            else Path(session.workspace_root).resolve(strict=True)
        )
        evidence = self.evidence.list(session.id)
        context = ValidationContext(
            session=session,
            evidence=evidence,
            sources=self._active_sources(session.id, evidence),
            workspace_root=workspace_root,
            claim_texts=claim_texts or [],
            duplicate_keys=duplicate_keys or {},
            workspace=self.workspace,
        )
        all_findings: list[ValidationFinding] = []
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
        elif checks_inconclusive:
            # Coverage gaps must not read as a clean pass: if any selected check could not
            # reach a verdict, the run is not a confident "validated".
            status = ValidationRunStatus.WARNINGS
        else:
            status = ValidationRunStatus.VALIDATED
        run = self.runs.add(
            ValidationRun(
                session_id=session.id,
                status=status,
                scope=scope,
                finding_ids=[finding.id for finding in persisted],
                checks_run=checks_run,
                checks_skipped=checks_skipped,
                checks_inconclusive=checks_inconclusive,
            )
        )
        return run, persisted
