from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from .artifact_registry import ArtifactRegistry
from .artifact_storage import ArtifactStorage, LocalArtifactStorage
from .models import AgentRunRequest, ArtifactCleanupResult, StepResult


class IdempotencyConflict(ValueError):
    pass


class JobStore:
    def __init__(
        self,
        root: Path,
        *,
        artifact_ttl_seconds: int = 30 * 24 * 60 * 60,
        artifact_storage: ArtifactStorage | None = None,
    ):
        self.root = root
        self.artifact_storage = artifact_storage or LocalArtifactStorage(root)
        self.artifacts = ArtifactRegistry(
            root / "artifact-registry.sqlite3",
            ttl_seconds=artifact_ttl_seconds,
        )

    @staticmethod
    def _request_bytes(request: AgentRunRequest) -> bytes:
        payload = request.model_dump(mode="json")
        return json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def begin(self, request: AgentRunRequest) -> StepResult | None:
        job_root = self.root / request.execution_id
        job_root.mkdir(parents=True, exist_ok=True)
        request_path = job_root / "request.json"
        digest_path = job_root / "request.sha256"
        request_bytes = self._request_bytes(request)
        digest = hashlib.sha256(request_bytes).hexdigest()

        if digest_path.is_file():
            existing = digest_path.read_text(encoding="ascii").strip()
            if existing != digest:
                raise IdempotencyConflict(
                    f"execution_id {request.execution_id} is already bound to another request"
                )
        else:
            request_path.write_bytes(request_bytes)
            digest_path.write_text(digest, encoding="ascii")

        return self.get(request.execution_id)

    def save(self, result: StepResult) -> None:
        job_root = self.root / result.execution_id
        job_root.mkdir(parents=True, exist_ok=True)
        target = job_root / "step-result.json"
        temporary = job_root / "step-result.json.tmp"
        temporary.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)
        created_at, expires_at = self.artifacts.expiry()
        records = self.artifact_storage.collect(
            result.execution_id,
            job_root / "output",
            created_at=created_at,
            expires_at=expires_at,
        )
        self.artifacts.register(records)

    def get(self, execution_id: str) -> StepResult | None:
        path = self.root / execution_id / "step-result.json"
        if not path.is_file():
            return None
        return StepResult.model_validate_json(path.read_text(encoding="utf-8"))

    def get_request(self, execution_id: str) -> AgentRunRequest | None:
        path = self.root / execution_id / "request.json"
        if not path.is_file():
            return None
        return AgentRunRequest.model_validate_json(path.read_text(encoding="utf-8"))

    def artifact(self, execution_id: str, relative_path: str) -> Path | None:
        normalized = Path(relative_path).as_posix()
        if self.artifacts.get(execution_id, normalized) is None:
            output_root = self.root / execution_id / "output"
            created_at, expires_at = self.artifacts.expiry()
            records = self.artifact_storage.collect(
                execution_id,
                output_root,
                created_at=created_at,
                expires_at=expires_at,
            )
            self.artifacts.register(records)
        if self.artifacts.get(execution_id, normalized) is None:
            return None
        return self.artifact_storage.resolve(execution_id, normalized)

    def cleanup_expired(self, *, now: datetime | None = None) -> ArtifactCleanupResult:
        expired = self.artifacts.expired(now=now)
        removed_files = 0
        removed_records: list = []
        failed: list[str] = []
        for record in expired:
            try:
                removed_files += int(self.artifact_storage.delete(record))
                removed_records.append(record)
            except OSError:
                failed.append(f"{record.execution_id}/{record.path}"[:2000])
        deleted = self.artifacts.delete(removed_records)
        return ArtifactCleanupResult(
            examined=len(expired),
            removed_records=deleted,
            removed_files=removed_files,
            failed=failed[:100],
        )
