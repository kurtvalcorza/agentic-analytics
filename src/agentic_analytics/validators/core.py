from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeGuard

from agentic_analytics.dataplane import DataPlaneClient, DataPlaneError
from agentic_analytics.models import (
    AnalysisSession,
    DataSource,
    EvidenceItem,
    SourceKind,
    ValidationFinding,
    ValidationSeverity,
)
from agentic_analytics.services.inspector import fingerprint_file
from agentic_analytics.services.workspace import WorkspaceAuthorizationError, WorkspaceService

_SUPPORTED_TABULAR = {SourceKind.CSV, SourceKind.PARQUET}
# Source-format, filesystem, resource, timeout, and worker-crash failures make a data check
# incomplete (inconclusive). SQL programming errors are raised as caller errors instead.


@dataclass(slots=True)
class ValidationContext:
    session: AnalysisSession
    evidence: list[EvidenceItem]
    sources: list[DataSource]
    workspace_root: Path
    claim_texts: list[str] = field(default_factory=list)
    # Per-source duplicate keys supplied through the public validate_analysis call, keyed by
    # source relative_path or source id. Makes the key-based duplicate check reachable via MCP.
    duplicate_keys: dict[str, list[str]] = field(default_factory=dict)
    workspace: WorkspaceService | None = None
    # Isolated DuckDB worker (SEC-02); required by the checks that read source data.
    data_plane: DataPlaneClient | None = None

    def table_stats(self, source: DataSource, path: Path, **request: Any) -> dict[str, Any]:
        if self.data_plane is None:
            raise RuntimeError("data-reading validation checks require a data-plane client")
        timeout = self.data_plane.settings.data_plane_timeout_seconds
        try:
            return self.data_plane.call(
                "table_stats",
                {
                    "path": str(path),
                    "kind": source.kind.value,
                    "limits": self.data_plane.limits(timeout),
                    **request,
                },
                deadline_seconds=timeout,
            )
        except DataPlaneError as exc:
            if exc.category == "sql":
                # A malformed request (e.g. an unknown duplicate-key column) is a caller error.
                raise ValueError(f"validation query failed: {exc}") from exc
            raise

    def resolve_source_file(self, source: DataSource) -> Path | None:
        if self.workspace is not None and source.relative_path is not None:
            try:
                return self.workspace.resolve_file(self.workspace_root, source.relative_path)
            except WorkspaceAuthorizationError:
                return None
        return _resolve_source_file(self.workspace_root, source)

    def source_duplicate_keys(self, source: DataSource) -> list[str]:
        if source.relative_path and source.relative_path in self.duplicate_keys:
            return list(self.duplicate_keys[source.relative_path])
        if source.id in self.duplicate_keys:
            return list(self.duplicate_keys[source.id])
        meta = source.profile.get("validation", {})
        keys_raw = meta.get("duplicate_keys", []) if isinstance(meta, dict) else []
        return [str(key) for key in keys_raw] if isinstance(keys_raw, list) else []


@dataclass(slots=True)
class CheckResult:
    check: str
    findings: list[ValidationFinding] = field(default_factory=list)
    outcome: str = "run"
    reason: str | None = None


class Validator(Protocol):
    name: str

    def check(self, context: ValidationContext) -> CheckResult: ...


def _finding(
    context: ValidationContext,
    check: str,
    code: str,
    severity: ValidationSeverity,
    message: str,
    *,
    entity_refs: list[dict[str, str]] | None = None,
    details: dict[str, object] | None = None,
    remediation: str | None = None,
) -> ValidationFinding:
    return ValidationFinding(
        session_id=context.session.id,
        code=code,
        severity=severity,
        message=message,
        entity_refs=entity_refs or [],
        check=check,
        details=details or {},
        remediation=remediation,
    )


def _normalize_claim(value: str) -> str:
    return " ".join(value.casefold().split())


def _is_number(value: object) -> TypeGuard[int | float]:
    # bool is a subclass of int; a boolean is never a meaningful analytical quantity.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _resolve_source_file(workspace_root: Path, source: DataSource) -> Path | None:
    """Resolve a source to a real file strictly contained in the workspace, else None.

    Mirrors the workspace containment check so validators never follow a symlink out of the
    authorized workspace, and never abort the whole run when a registered file is missing.
    """

    if source.relative_path is None:
        return None
    requested = Path(source.relative_path)
    if requested.is_absolute() or ".." in requested.parts:
        return None
    try:
        resolved = (workspace_root / requested).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if resolved != workspace_root and workspace_root not in resolved.parents:
        return None
    if not resolved.is_file():
        return None
    return resolved


def _inconclusive(check: str, reason: str) -> CheckResult:
    return CheckResult(check, outcome="inconclusive", reason=reason)


def _source_read_failure(result: CheckResult, source: DataSource, error: Exception) -> None:
    result.outcome = "inconclusive"
    # Keep diagnostics bounded and avoid returning DuckDB's full source contents/host path.
    name = error.error_type if isinstance(error, DataPlaneError) else type(error).__name__
    reason = f"could not read source {source.id}: {name}"
    result.reason = f"{result.reason}; {reason}" if result.reason else reason


class EvidenceCoverageValidator:
    name = "evidence_coverage"

    def check(self, context: ValidationContext) -> CheckResult:
        if not context.claim_texts:
            return _inconclusive(self.name, "no final claim_texts supplied")
        material_claims = {
            _normalize_claim(item.claim) for item in context.evidence if item.material
        }
        findings: list[ValidationFinding] = []
        for claim in context.claim_texts:
            if _normalize_claim(claim) in material_claims:
                continue
            findings.append(
                _finding(
                    context,
                    self.name,
                    "MISSING_MATERIAL_EVIDENCE",
                    ValidationSeverity.BLOCKING,
                    f"Material claim has no registered evidence linkage: {claim}",
                    details={"claim": claim},
                    remediation=(
                        "Register a material evidence item for this claim or remove it "
                        "from the final claims."
                    ),
                )
            )
        return CheckResult(self.name, findings)


class StaleSourceValidator:
    name = "stale_sources"

    def check(self, context: ValidationContext) -> CheckResult:
        findings: list[ValidationFinding] = []
        result = CheckResult(self.name, findings)
        checked = 0
        for source in context.sources:
            if source.relative_path is None:
                continue
            checked += 1
            path = context.resolve_source_file(source)
            if path is None:
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "SOURCE_MISSING",
                        ValidationSeverity.BLOCKING,
                        f"Registered source is no longer present: {source.relative_path}",
                        entity_refs=[{"type": "source", "id": source.id}],
                        remediation=(
                            "Restore and re-inspect the source before final validation."
                        ),
                    )
                )
                continue
            try:
                current = fingerprint_file(path)
            except OSError as exc:
                _source_read_failure(result, source, exc)
                continue
            if current != source.fingerprint:
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "STALE_SOURCE",
                        ValidationSeverity.BLOCKING,
                        (
                            "Source fingerprint changed after registration: "
                            f"{source.display_name}"
                        ),
                        entity_refs=[{"type": "source", "id": source.id}],
                        details={
                            "registered": source.fingerprint,
                            "current": current,
                        },
                        remediation=(
                            "Re-inspect the changed source and rerun dependent analyses."
                        ),
                    )
                )
        if checked == 0:
            return _inconclusive(self.name, "no local file sources")
        return result


class DuplicateObservationValidator:
    name = "duplicates"

    def check(self, context: ValidationContext) -> CheckResult:
        findings: list[ValidationFinding] = []
        result = CheckResult(self.name, findings)
        supported = 0
        for source in context.sources:
            if source.relative_path is None or source.kind not in _SUPPORTED_TABULAR:
                continue
            path = context.resolve_source_file(source)
            if path is None:
                # Missing/escaped sources are reported by stale_sources; do not abort here.
                continue
            supported += 1
            keys = context.source_duplicate_keys(source)
            try:
                stats = context.table_stats(source, path, distinct=True, duplicate_keys=keys)
            except DataPlaneError as exc:
                _source_read_failure(result, source, exc)
                continue
            duplicate_rows = int(stats["total"]) - int(stats["distinct"])
            if duplicate_rows > 0:
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "DUPLICATE_ROWS",
                        ValidationSeverity.WARNING,
                        f"Source contains {duplicate_rows} duplicate whole-row observations.",
                        entity_refs=[{"type": "source", "id": source.id}],
                        details={"duplicate_rows": duplicate_rows, "mode": "whole_row"},
                        remediation=(
                            "Confirm whether duplicate rows are legitimate repeated observations."
                        ),
                    )
                )
            duplicate_keys = int(stats.get("duplicate_key_rows", 0))
            if keys and duplicate_keys > 0:
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "DUPLICATE_KEYS",
                        ValidationSeverity.ERROR,
                        f"Configured observation key is duplicated {duplicate_keys} times.",
                        entity_refs=[{"type": "source", "id": source.id}],
                        details={
                            "duplicate_rows": duplicate_keys,
                            "mode": "configured_key",
                            "keys": keys,
                        },
                        remediation=(
                            "Resolve duplicate primary observations or register the correct "
                            "analytical grain."
                        ),
                    )
                )
        if supported == 0:
            return _inconclusive(self.name, "no supported tabular sources")
        return result


class MissingnessValidator:
    name = "missingness"

    def check(self, context: ValidationContext) -> CheckResult:
        findings: list[ValidationFinding] = []
        result = CheckResult(self.name, findings)
        supported = 0
        for source in context.sources:
            if source.relative_path is None or source.kind not in _SUPPORTED_TABULAR:
                continue
            path = context.resolve_source_file(source)
            if path is None:
                continue
            supported += 1
            try:
                stats = context.table_stats(source, path, null_counts=True)
            except DataPlaneError as exc:
                _source_read_failure(result, source, exc)
                continue
            total = int(stats["total"])
            if total == 0:
                continue
            meta = source.profile.get("validation", {})
            if isinstance(meta, dict):
                warning_threshold = float(meta.get("missingness_warning_threshold", 0.10))
                error_threshold = float(meta.get("missingness_error_threshold", 0.30))
            else:
                warning_threshold = 0.10
                error_threshold = 0.30
            for column in stats["columns"]:
                missing = int(stats["null_counts"][column])
                rate = missing / total
                if rate < warning_threshold:
                    continue
                severity = (
                    ValidationSeverity.ERROR
                    if rate >= error_threshold
                    else ValidationSeverity.WARNING
                )
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "HIGH_MISSINGNESS",
                        severity,
                        f"Column {column!r} has {rate:.1%} missing values.",
                        entity_refs=[{"type": "source", "id": source.id}],
                        details={
                            "column": column,
                            "missing": missing,
                            "total": total,
                            "rate": rate,
                        },
                        remediation=(
                            "Assess the missing-data mechanism and document or revise the "
                            "handling strategy."
                        ),
                    )
                )
        if supported == 0:
            return _inconclusive(self.name, "no supported tabular sources")
        return result


class DenominatorConsistencyValidator:
    name = "denominator_consistency"

    def check(self, context: ValidationContext) -> CheckResult:
        findings: list[ValidationFinding] = []
        checked = 0
        for item in context.evidence:
            if not isinstance(item.value, dict) or "denominator" not in item.value:
                continue
            checked += 1
            denominator = item.value.get("denominator")
            numerator = item.value.get("numerator")
            expected = item.value.get("expected_denominator")
            if not _is_number(denominator) or denominator <= 0:
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "INVALID_DENOMINATOR",
                        ValidationSeverity.BLOCKING,
                        "Registered denominator must be a positive number.",
                        entity_refs=[{"type": "evidence", "id": item.id}],
                        remediation=(
                            "Register the denominator used for the reported statistic "
                            "explicitly."
                        ),
                    )
                )
                continue
            if _is_number(numerator) and numerator > denominator:
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "NUMERATOR_EXCEEDS_DENOMINATOR",
                        ValidationSeverity.BLOCKING,
                        "Registered numerator exceeds its denominator.",
                        entity_refs=[{"type": "evidence", "id": item.id}],
                        details={
                            "numerator": numerator,
                            "denominator": denominator,
                        },
                        remediation=(
                            "Correct the numerator/denominator metadata and recompute "
                            "the claim."
                        ),
                    )
                )
            if _is_number(expected) and expected != denominator:
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "DENOMINATOR_MISMATCH",
                        ValidationSeverity.ERROR,
                        (
                            "Reported denominator differs from the registered expected "
                            "denominator."
                        ),
                        entity_refs=[{"type": "evidence", "id": item.id}],
                        details={
                            "denominator": denominator,
                            "expected_denominator": expected,
                        },
                        remediation=(
                            "Reconcile the analysis denominator with the registered "
                            "eligible population."
                        ),
                    )
                )
        if checked == 0:
            return _inconclusive(self.name, "no denominator metadata registered")
        return CheckResult(self.name, findings)


_CAUSAL = re.compile(
    r"\b(caus(?:e|ed|es|ing|al|ally|ation)|led to|resulted in|responsible for|"
    r"treatment effect|causal effect)\b",
    re.IGNORECASE,
)
_SUPPORTED_CAUSAL_DESIGNS = {
    "randomized_experiment",
    "randomized_controlled_trial",
    "quasi_experimental",
}


class UnsupportedCausalLanguageValidator:
    name = "causal_language"

    def check(self, context: ValidationContext) -> CheckResult:
        design = str(context.session.metadata.get("analysis_design", "")).casefold()
        explicitly_supported = context.session.metadata.get("causal_design") is True
        declared_design = explicitly_supported or design in _SUPPORTED_CAUSAL_DESIGNS
        findings: list[ValidationFinding] = []
        # Track the originating evidence id so an evidence-derived finding can link back to it.
        if context.claim_texts:
            candidates: list[tuple[str, str | None]] = [
                (claim, None) for claim in context.claim_texts
            ]
        else:
            candidates = [
                (item.claim, item.id) for item in context.evidence if item.material
            ]
        for claim, evidence_id in candidates:
            if not _CAUSAL.search(claim):
                continue
            entity_refs = (
                [{"type": "evidence", "id": evidence_id}] if evidence_id is not None else []
            )
            if declared_design:
                # The design is session metadata supplied by the caller, not evidence the
                # runtime checked. Accept the wording but disclose that it rests on an assertion.
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "CAUSAL_DESIGN_ASSERTED",
                        ValidationSeverity.INFO,
                        (
                            "Causal language accepted on the caller's declared design, which "
                            f"the runtime has not verified: {claim}"
                        ),
                        entity_refs=entity_refs,
                        details={
                            "analysis_design": design or None,
                            "causal_design_declared": explicitly_supported,
                            "claim": claim,
                        },
                        remediation=(
                            "Document the identification strategy and supporting design "
                            "evidence alongside the claim."
                        ),
                    )
                )
                continue
            findings.append(
                _finding(
                    context,
                    self.name,
                    "UNSUPPORTED_CAUSAL_CLAIM",
                    ValidationSeverity.BLOCKING,
                    (
                        "Causal language is unsupported by the registered design: "
                        f"{claim}"
                    ),
                    entity_refs=entity_refs,
                    details={"analysis_design": design or None, "claim": claim},
                    remediation=(
                        "Use associational wording or register a supported causal design."
                    ),
                )
            )
        return CheckResult(self.name, findings)


DEFAULT_VALIDATORS: tuple[Validator, ...] = (
    EvidenceCoverageValidator(),
    StaleSourceValidator(),
    DuplicateObservationValidator(),
    MissingnessValidator(),
    DenominatorConsistencyValidator(),
    UnsupportedCausalLanguageValidator(),
)
