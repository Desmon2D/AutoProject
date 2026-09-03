from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .models import ArtifactRecord

INTERNAL_OUTPUT_FILES = {"result.json", "agent-result.json"}


class ArtifactStorage(Protocol):
    def collect(
        self,
        execution_id: str,
        output_root: Path,
        *,
        created_at: datetime,
        expires_at: datetime | None,
    ) -> list[ArtifactRecord]: ...

    def resolve(self, execution_id: str, relative_path: str) -> Path | None: ...

    def delete(self, record: ArtifactRecord) -> bool: ...


class LocalArtifactStorage:
    def __init__(self, jobs_root: Path):
        self.jobs_root = jobs_root.resolve()

    def collect(
        self,
        execution_id: str,
        output_root: Path,
        *,
        created_at: datetime,
        expires_at: datetime | None,
    ) -> list[ArtifactRecord]:
        if not output_root.is_dir():
            return []
        root = output_root.resolve()
        records: list[ArtifactRecord] = []
        for path in sorted(output_root.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.name in INTERNAL_OUTPUT_FILES:
                continue
            resolved = path.resolve()
            if root not in resolved.parents:
                continue
            digest = hashlib.sha256()
            with resolved.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            records.append(
                ArtifactRecord(
                    execution_id=execution_id,
                    path=resolved.relative_to(root).as_posix(),
                    size_bytes=resolved.stat().st_size,
                    sha256=digest.hexdigest(),
                    created_at=created_at,
                    expires_at=expires_at,
                )
            )
        return records

    def resolve(self, execution_id: str, relative_path: str) -> Path | None:
        output_root = (self.jobs_root / execution_id / "output").resolve()
        target = output_root.joinpath(*Path(relative_path).parts).resolve()
        if output_root not in target.parents or not target.is_file() or target.is_symlink():
            return None
        return target

    def delete(self, record: ArtifactRecord) -> bool:
        target = self.resolve(record.execution_id, record.path)
        if target is None:
            return False
        output_root = (self.jobs_root / record.execution_id / "output").resolve()
        target.unlink()
        parent = target.parent
        while parent != output_root and output_root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        return True
