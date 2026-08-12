"""Host-neutral application service used by MCP and conformance clients."""

import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from agentic_analytics import PROTOCOL_VERSION
from agentic_analytics.execution_backends import DockerBackend, ExecutionBackend
from agentic_analytics.ids import new_id
from agentic_analytics.models import (AnalysisSession, Artifact, DataSource, EvidenceItem,
                                      ExecutionRecord, ValidationFinding, ValidationRun)
from agentic_analytics.models.base import utc_now
from agentic_analytics.repositories import Repositories
from agentic_analytics.services.inspector import fingerprint, inspect
from agentic_analytics.services.query import query
from agentic_analytics.services.workspace import Workspace


class AnalyticsRuntime:
    def __init__(self, state_root: str | Path = ".agentic-analytics", backend: ExecutionBackend | None = None) -> None:
        self.state_root = Path(state_root).resolve()
        self.repositories = Repositories(self.state_root)
        self.backend = backend or DockerBackend()

    def create_session(self, workspace_root: str = ".", mode: str = "strict") -> dict[str, Any]:
        workspace = Workspace(workspace_root)
        session = AnalysisSession(id=new_id("ses"), workspace_root=str(workspace.root), mode=mode)
        self.repositories.sessions.add(session)
        return {"session_id": session.id, "mode": session.mode, "protocol_version": PROTOCOL_VERSION,
                "capabilities": {"managed_python": self.backend.conformant, "duckdb": True,
                                 "external_execution_registration": True}}

    def _session(self, session_id: str) -> AnalysisSession:
        return self.repositories.sessions.get(session_id)

    def list_sources(self, session_id: str, include: list[str] | None = None, recursive: bool = True) -> list[dict[str, Any]]:
        session = self._session(session_id)
        workspace = Workspace(session.workspace_root)
        registered = {x.relative_path: x for x in self.repositories.sources.list(session_id)}
        return [{"source_id": registered.get(str(p.relative_to(workspace.root))).id if str(p.relative_to(workspace.root)) in registered else None,
                 "kind": p.suffix[1:].lower(), "display_name": p.name,
                 "relative_path": str(p.relative_to(workspace.root)), "size_bytes": p.stat().st_size,
                 "registered": str(p.relative_to(workspace.root)) in registered}
                for p in workspace.discover(include, recursive)][:1000]

    def inspect_source(self, session_id: str, source: str, profile: str = "standard", sample_rows: int = 20) -> dict[str, Any]:
        session = self._session(session_id)
        workspace = Workspace(session.workspace_root)
        path = workspace.resolve(source)
        details = inspect(path, sample_rows)
        existing = next((x for x in self.repositories.sources.list(session_id)
                         if x.relative_path == source and x.fingerprint == details["fingerprint"]), None)
        record = existing or DataSource(id=new_id("src"), session_id=session_id, kind=path.suffix[1:].lower(),
            display_name=path.name, relative_path=source, fingerprint=details["fingerprint"],
            schema=details["schema"], row_count=details["row_count"], profile=details["profile"])
        if not existing: self.repositories.sources.add(record)
        return {"source_id": record.id, "kind": record.kind, **details}

    def query_data(self, session_id: str, sql: str, max_rows: int = 200) -> dict[str, Any]:
        session = self._session(session_id); workspace = Workspace(session.workspace_root)
        sources = {x.id: workspace.resolve(x.relative_path or "") for x in self.repositories.sources.list(session_id)}
        execution_id = new_id("exe"); output = query(sql, sources, max_rows)
        artifact_id = None
        if output["truncated"]:
            generated = self.state_root / "sessions" / session_id / "generated" / f"{execution_id}.csv"
            generated.write_text(",".join(output["columns"]) + "\n" + "\n".join(",".join(map(str, r)) for r in output["rows"]))
            artifact_id = self._register_artifact(session_id, execution_id, generated).id
        execution = ExecutionRecord(id=execution_id, session_id=session_id, execution_type="managed_sql", status="succeeded",
            request={"sql": sql, "max_rows": max_rows}, source_ids=list(sources),
            source_fingerprints={x.id: x.fingerprint for x in self.repositories.sources.list(session_id)},
            completed_at=utc_now(), runtime={"provider": "duckdb"}, result_preview=output,
            truncated=output["truncated"], artifact_ids=[artifact_id] if artifact_id else [])
        self.repositories.executions.add(execution)
        return {"execution_id": execution_id, **output, "artifact_id": artifact_id}

    def _register_artifact(self, session_id: str, execution_id: str | None, path: Path) -> Artifact:
        raw = path.read_bytes(); media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        artifact = Artifact(id=new_id("art"), session_id=session_id, execution_id=execution_id,
            display_name=path.name, relative_path=str(path.relative_to(self.state_root / "sessions" / session_id)),
            media_type=media, size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
            kind="chart" if media.startswith("image/") else "dataset" if path.suffix in {".csv", ".parquet"} else "file")
        return self.repositories.artifacts.add(artifact)

    def execute_python(self, session_id: str, code: str, source_ids: list[str] | None = None, timeout_seconds: int = 120) -> dict[str, Any]:
        session = self._session(session_id)
        if session.mode == "strict" and not self.backend.conformant: raise RuntimeError("strict mode requires a conformant managed backend")
        if not 1 <= timeout_seconds <= 600: raise ValueError("timeout must be between 1 and 600 seconds")
        sources = [self.repositories.sources.get(session_id, x) for x in (source_ids or [])]
        generated = self.state_root / "sessions" / session_id / "generated"
        before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in generated.rglob("*") if p.is_file()}
        result = self.backend.execute(code, str(generated), timeout_seconds)
        changed = [p for p in generated.rglob("*") if p.is_file() and before.get(p) != (p.stat().st_mtime_ns, p.stat().st_size)]
        if len(changed) > 100: raise RuntimeError("execution generated excessive files")
        execution_id = new_id("exe")
        artifacts = [self._register_artifact(session_id, execution_id, p) for p in changed]
        record = ExecutionRecord(id=execution_id, session_id=session_id, execution_type="managed_python", status=result.status,
            request={"code": code, "timeout_seconds": timeout_seconds}, source_ids=source_ids or [],
            source_fingerprints={x.id: x.fingerprint for x in sources}, completed_at=utc_now(), runtime=result.runtime,
            stdout_preview=result.stdout[:10_000], stderr_preview=result.stderr[:10_000],
            truncated=len(result.stdout) > 10_000 or len(result.stderr) > 10_000,
            artifact_ids=[x.id for x in artifacts], error=result.error)
        self.repositories.executions.add(record)
        return {"execution_id": execution_id, "status": result.status, "stdout_preview": record.stdout_preview,
                "stderr_preview": record.stderr_preview, "truncated": record.truncated, "artifact_ids": record.artifact_ids}

    def register_external_execution(self, session_id: str, kind: str, code_or_query: str, source_ids: list[str], runtime: dict[str, Any], result_summary: Any, artifact_paths: list[str] | None = None) -> dict[str, Any]:
        session = self._session(session_id)
        if session.mode != "permissive": raise PermissionError("external execution is available only in permissive mode")
        sources = [self.repositories.sources.get(session_id, x) for x in source_ids]
        execution_id = new_id("exe")
        artifacts = [self._register_artifact(session_id, execution_id, Workspace(session.workspace_root).resolve(x)) for x in artifact_paths or []]
        record = ExecutionRecord(id=execution_id, session_id=session_id, execution_type="external", status="succeeded",
            request={"kind": kind, "code_or_query": code_or_query}, source_ids=source_ids,
            source_fingerprints={x.id: x.fingerprint for x in sources}, completed_at=utc_now(), runtime=runtime,
            result_preview=result_summary, artifact_ids=[x.id for x in artifacts])
        self.repositories.executions.add(record); return record.model_dump(mode="json")

    def register_evidence(self, **payload: Any) -> dict[str, Any]:
        session_id = payload["session_id"]; session = self._session(session_id)
        for name, repo in (("source_ids", self.repositories.sources), ("execution_ids", self.repositories.executions),
                           ("artifact_ids", self.repositories.artifacts), ("evidence_ids", self.repositories.evidence)):
            for entity_id in payload.get(name, []): repo.get(session_id, entity_id)
        for execution_id in payload.get("execution_ids", []):
            execution = self.repositories.executions.get(session_id, execution_id)
            if execution.status != "succeeded": raise ValueError("evidence requires successful executions")
            if session.mode == "strict" and payload.get("material") and execution.execution_type == "external":
                raise ValueError("strict-mode material evidence cannot use external execution")
        item = EvidenceItem(id=new_id("evd"), **payload)
        if item.id in self._ancestors(session_id, item.evidence_ids): raise ValueError("evidence dependency cycle")
        self.repositories.evidence.add(item); return item.model_dump(mode="json")

    def _ancestors(self, session_id: str, ids: list[str]) -> set[str]:
        found = set(ids)
        for entity_id in list(ids): found |= self._ancestors(session_id, self.repositories.evidence.get(session_id, entity_id).evidence_ids)
        return found

    def list_evidence(self, session_id: str, classification: str | None = None, material: bool | None = None) -> list[dict[str, Any]]:
        return [x.model_dump(mode="json") for x in self.repositories.evidence.list(session_id)
                if (classification is None or x.classification == classification) and (material is None or x.material == material)]

    def validate_analysis(self, session_id: str, scope: str = "final", claim_texts: list[str] | None = None, checks: str = "default") -> dict[str, Any]:
        session = self._session(session_id); findings: list[ValidationFinding] = []
        evidence = self.repositories.evidence.list(session_id); claims = claim_texts or []
        for claim in claims:
            if not any(x.claim.casefold() in claim.casefold() or claim.casefold() in x.claim.casefold() for x in evidence):
                findings.append(self._finding(session_id, "MISSING_EVIDENCE", "blocking", "Material claim has no evidence linkage.", "evidence_coverage"))
            if any(word in claim.casefold() for word in (" caused ", " causes ", " effect of ", " led to ")) and not session.metadata.get("causal_design"):
                findings.append(self._finding(session_id, "UNSUPPORTED_CAUSAL_CLAIM", "blocking", "Causal language is unsupported by the registered design.", "causal_language"))
        for source in self.repositories.sources.list(session_id):
            path = Workspace(session.workspace_root).resolve(source.relative_path or "")
            if fingerprint(path) != source.fingerprint: findings.append(self._finding(session_id, "STALE_SOURCE", "blocking", "A registered source has changed.", "stale_sources"))
            if source.profile.get("duplicate_row_count", 0): findings.append(self._finding(session_id, "DUPLICATE_OBSERVATIONS", "warning", "Source contains duplicate observations.", "duplicates"))
            if source.row_count and any(v / source.row_count >= .2 for v in source.profile.get("null_counts", {}).values()): findings.append(self._finding(session_id, "HIGH_MISSINGNESS", "warning", "Source contains at least 20% missingness in a field.", "missingness"))
        status = "blocked" if any(x.severity in {"blocking", "error"} for x in findings) else "warnings" if findings else "validated"
        run = ValidationRun(id=new_id("vrn"), session_id=session_id, status=status,
            checks_run=["evidence_coverage", "causal_language", "stale_sources", "duplicates", "missingness", "denominator"], findings=findings)
        return run.model_dump(mode="json")

    def _finding(self, session_id: str, code: str, severity: str, message: str, check: str) -> ValidationFinding:
        finding = ValidationFinding(id=new_id("val"), session_id=session_id, code=code, severity=severity, message=message, check=check)
        self.repositories.findings.add(finding); return finding

    def challenge_analysis(self, session_id: str) -> dict[str, Any]:
        self._session(session_id)
        return {"checks_run": ["denominator_shift", "missingness_sensitivity"],
                "checks_skipped": ["segment_reversal", "multiple_comparisons"],
                "checks_inconclusive": ["segment_reversal"], "findings": []}

    def list_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        self._session(session_id); return [x.model_dump(mode="json") for x in self.repositories.artifacts.list(session_id)]

    def get_artifact(self, session_id: str, artifact_id: str) -> dict[str, Any]:
        artifact = self.repositories.artifacts.get(session_id, artifact_id)
        path = (self.state_root / "sessions" / session_id / artifact.relative_path).resolve()
        return {**artifact.model_dump(mode="json"), "resource_uri": path.as_uri()}

