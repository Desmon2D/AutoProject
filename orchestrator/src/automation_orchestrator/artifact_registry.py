from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import ArtifactRecord


class ArtifactRegistry:
    def __init__(self, path: Path, *, ttl_seconds: int = 30 * 24 * 60 * 60):
        self.path = path
        self.ttl_seconds = max(0, ttl_seconds)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS artifacts (
                    execution_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT,
                    PRIMARY KEY (execution_id, path)
                );
                CREATE INDEX IF NOT EXISTS artifacts_expiry_idx ON artifacts (expires_at);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def expiry(self, *, now: datetime | None = None) -> tuple[datetime, datetime | None]:
        current = now or datetime.now(UTC)
        expires_at = current + timedelta(seconds=self.ttl_seconds) if self.ttl_seconds else None
        return current, expires_at

    def register(self, records: list[ArtifactRecord]) -> list[ArtifactRecord]:
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO artifacts (
                    execution_id, path, size_bytes, sha256, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_id, path) DO UPDATE SET
                    size_bytes = excluded.size_bytes,
                    sha256 = excluded.sha256,
                    expires_at = excluded.expires_at
                """,
                [
                    (
                        record.execution_id,
                        record.path,
                        record.size_bytes,
                        record.sha256,
                        record.created_at.isoformat(),
                        record.expires_at.isoformat() if record.expires_at else None,
                    )
                    for record in records
                ],
            )
        return records

    def delete(self, records: list[ArtifactRecord]) -> int:
        if not records:
            return 0
        with self._connect() as connection:
            cursor = connection.executemany(
                "DELETE FROM artifacts WHERE execution_id = ? AND path = ?",
                [(record.execution_id, record.path) for record in records],
            )
        return cursor.rowcount

    def list(self, execution_id: str | None = None) -> list[ArtifactRecord]:
        query = "SELECT * FROM artifacts"
        parameters: tuple[str, ...] = ()
        if execution_id is not None:
            query += " WHERE execution_id = ?"
            parameters = (execution_id,)
        query += " ORDER BY created_at DESC, path"
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._record(row) for row in rows]

    def get(self, execution_id: str, path: str) -> ArtifactRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE execution_id = ? AND path = ?",
                (execution_id, path),
            ).fetchone()
        return self._record(row) if row is not None else None

    def expired(self, *, now: datetime | None = None) -> list[ArtifactRecord]:
        current = (now or datetime.now(UTC)).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (current,),
            ).fetchall()
        return [self._record(row) for row in rows]

    @staticmethod
    def _record(row: sqlite3.Row) -> ArtifactRecord:
        return ArtifactRecord(
            execution_id=row["execution_id"],
            path=row["path"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
        )
