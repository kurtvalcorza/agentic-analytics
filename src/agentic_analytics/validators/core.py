from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import duckdb

from agentic_analytics.models import (
    AnalysisSession,
    DataSource,
    EvidenceItem,
    SourceKind,
    ValidationFinding,
    ValidationSeverity,
)
from agentic_analytics.services.inspector import fingerprint_file


@dataclass(slots=True)
class ValidationContext:
    session: AnalysisSession
    evidence: list[EvidenceItem]
    sources: list[DataSource]
    workspace_root: Path
    claim_texts: list[str] = field(default_factory=list)


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


class EvidenceCoverageValidator:
    name = "evidence_coverage"

    def check(self, context: ValidationContext) -> CheckResult:
        if not context.claim_texts:
            return CheckResult(self.name, outcome="inconclusive", reason="no final claim_texts supplied")
        material_claims = {
            _normalize_claim(item.claim): item for item in context.evidence if item.material
        }
        findings: list[ValidationFinding] = []
        for claim in context.claim_texts:
            if _normalize_claim(claim) not in material_claims:
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "MISSING_MATERIAL_EVIDENCE",
                        ValidationSeverity.BLOCKING,
                        f"Material claim has no registered evidence linkage: {claim}",
                        details={"claim": claim},
                        remediation="Register a material evidence item for this claim or remove it from the final claims.",
                    )
                )
        return CheckResult(self.name, findings)


class StaleSourceValidator:
    name = "stale_sources"

    def check(self, context: ValidationContext) -> CheckResult:
        findings: list[ValidationFinding] = []
        checked = 0
        for source in context.sources:
            if source.relative_path is None:
                continue
            checked += 1
            path = (context.workspace_root / source.relative_path).resolve(strict=False)
            if not path.exists():
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "SOURCE_MISSING",
                        ValidationSeverity.BLOCKING,
                        f"Registered source is no longer present: {source.relative_path}",
                        entity_refs=[{"type": "source", "id": source.id}],
                        remediation="Restore and re-inspect the source before final validation.",
                    )
                )
                continue
            current = fingerprint_file(path)
            if current != source.fingerprint:
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "STALE_SOURCE",
                        ValidationSeverity.BLOCKING,
                        f"Source fingerprint changed after registration: {source.display_name}",
                        entity_refs=[{"type": "source", "id": source.id}],
                        details={"registered": source.fingerprint, "current": current},
                        remediation="Re-inspect the changed source and rerun dependent analyses.",
                    )
                )
        if checked == 0:
            return CheckResult(self.name, outcome="inconclusive", reason="no local file sources")
        return CheckResult(self.name, findings)


def _reader_sql(source: DataSource, path: Path) -> str:
    literal = "'" + str(path).replace("'", "''") + "'"
    if source.kind is SourceKind.CSV:
        return f"read_csv({literal}, strict_mode = true)"
    if source.kind is SourceKind.PARQUET:
        return f"read_parquet({literal})"
    raise ValueError(f"unsupported source kind for validation: {source.kind}")


class DuplicateObservationValidator:
    name = "duplicates"

    def check(self, context: ValidationContext) -> CheckResult:
        findings: list[ValidationFinding] = []
        supported = 0
        for source in context.sources:
            if source.relative_path is None or source.kind not in {SourceKind.CSV, SourceKind.PARQUET}:
                continue
            supported += 1
            path = (context.workspace_root / source.relative_path).resolve(strict=True)
            reader = _reader_sql(source, path)
            connection = duckdb.connect(database=":memory:")
            try:
                total = int(connection.execute(f"SELECT count(*) FROM {reader}").fetchone()[0])
                distinct = int(
                    connection.execute(
                        f"SELECT count(*) FROM (SELECT DISTINCT * FROM {reader})"
                    ).fetchone()[0]
                )
                duplicate_rows = total - distinct
                validation_meta = source.profile.get("validation", {})
                keys = validation_meta.get("duplicate_keys", []) if isinstance(validation_meta, dict) else []
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
                            remediation="Confirm whether duplicate rows are legitimate repeated observations.",
                        )
                    )
                if keys:
                    quoted = ", ".join('"' + str(key).replace('"', '""') + '"' for key in keys)
                    duplicate_keys = int(
                        connection.execute(
                            f"SELECT coalesce(sum(n - 1), 0) FROM ("
                            f"SELECT count(*) AS n FROM {reader} GROUP BY {quoted} HAVING count(*) > 1)"
                        ).fetchone()[0]
                    )
                    if duplicate_keys > 0:
                        findings.append(
                            _finding(
                                context,
                                self.name,
                                "DUPLICATE_KEYS",
                                ValidationSeverity.ERROR,
                                f"Configured observation key is duplicated {duplicate_keys} times.",
                                entity_refs=[{"type": "source", "id": source.id}],
                                details={"duplicate_rows": duplicate_keys, "mode": "configured_key", "keys": keys},
                                remediation="Resolve duplicate primary observations or register the correct analytical grain.",
                            )
                        )
            finally:
                connection.close()
        if supported == 0:
            return CheckResult(self.name, outcome="inconclusive", reason="no supported tabular sources")
        return CheckResult(self.name, findings)


class MissingnessValidator:
    name = "missingness"

    def check(self, context: ValidationContext) -> CheckResult:
        findings: list[ValidationFinding] = []
        supported = 0
        for source in context.sources:
            if source.relative_path is None or source.kind not in {SourceKind.CSV, SourceKind.PARQUET}:
                continue
            supported += 1
            path = (context.workspace_root / source.relative_path).resolve(strict=True)
            reader = _reader_sql(source, path)
            connection = duckdb.connect(database=":memory:")
            try:
                cursor = connection.execute(f"SELECT * FROM {reader} LIMIT 0")
                columns = [str(item[0]) for item in cursor.description or []]
                total = int(connection.execute(f"SELECT count(*) FROM {reader}").fetchone()[0])
                if total == 0:
                    continue
                meta = source.profile.get("validation", {})
                warning_threshold = float(meta.get("missingness_warning_threshold", 0.10)) if isinstance(meta, dict) else 0.10
                error_threshold = float(meta.get("missingness_error_threshold", 0.30)) if isinstance(meta, dict) else 0.30
                for column in columns:
                    quoted = '"' + column.replace('"', '""') + '"'
                    missing = int(
                        connection.execute(
                            f"SELECT count(*) FROM {reader} WHERE {quoted} IS NULL"
                        ).fetchone()[0]
                    )
                    rate = missing / total
                    if rate >= warning_threshold:
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
                                details={"column": column, "missing": missing, "total": total, "rate": rate},
                                remediation="Assess missing-data mechanism and document or revise the handling strategy.",
                            )
                        )
            finally:
                connection.close()
        if supported == 0:
            return CheckResult(self.name, outcome="inconclusive", reason="no supported tabular sources")
        return CheckResult(self.name, findings)


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
            if not isinstance(denominator, (int, float)) or denominator <= 0:
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "INVALID_DENOMINATOR",
                        ValidationSeverity.BLOCKING,
                        "Registered denominator must be a positive number.",
                        entity_refs=[{"type": "evidence", "id": item.id}],
                        remediation="Register the denominator used for the reported statistic explicitly.",
                    )
                )
                continue
            if isinstance(numerator, (int, float)) and numerator > denominator:
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "NUMERATOR_EXCEEDS_DENOMINATOR",
                        ValidationSeverity.BLOCKING,
                        "Registered numerator exceeds its denominator.",
                        entity_refs=[{"type": "evidence", "id": item.id}],
                        details={"numerator": numerator, "denominator": denominator},
                        remediation="Correct the numerator/denominator metadata and recompute the claim.",
                    )
                )
            if isinstance(expected, (int, float)) and expected != denominator:
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "DENOMINATOR_MISMATCH",
                        ValidationSeverity.ERROR,
                        "Reported denominator differs from the registered expected denominator.",
                        entity_refs=[{"type": "evidence", "id": item.id}],
                        details={"denominator": denominator, "expected_denominator": expected},
                        remediation="Reconcile the analysis denominator with the registered eligible population.",
                    )
                )
        if checked == 0:
            return CheckResult(self.name, outcome="inconclusive", reason="no denominator metadata registered")
        return CheckResult(self.name, findings)


_CAUSAL = re.compile(
    r"\b(caus(?:e|ed|es|al|ally)|led to|resulted in|responsible for|treatment effect|causal effect)\b",
    re.IGNORECASE,
)
_SUPPORTED_CAUSAL_DESIGNS = {"randomized_experiment", "randomized_controlled_trial", "quasi_experimental"}


class UnsupportedCausalLanguageValidator:
    name = "causal_language"

    def check(self, context: ValidationContext) -> CheckResult:
        design = str(context.session.metadata.get("analysis_design", "")).casefold()
        explicitly_supported = context.session.metadata.get("causal_design") is True
        if explicitly_supported or design in _SUPPORTED_CAUSAL_DESIGNS:
            return CheckResult(self.name)
        findings: list[ValidationFinding] = []
        claims = context.claim_texts or [item.claim for item in context.evidence if item.material]
        for claim in claims:
            if _CAUSAL.search(claim):
                findings.append(
                    _finding(
                        context,
                        self.name,
                        "UNSUPPORTED_CAUSAL_CLAIM",
                        ValidationSeverity.BLOCKING,
                        f"Causal language is unsupported by the registered design: {claim}",
                        details={"analysis_design": design or None, "claim": claim},
                        remediation="Use associational wording or register a supported causal design.",
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
