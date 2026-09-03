from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import AuditEvent

SENSITIVE_KEY = re.compile(
    r"(?:authorization|credential|password|secret|token|api.?key)", re.IGNORECASE
)


def _safe_details(value: Any, *, depth: int = 0) -> Any:
    if depth >= 6:
        return "<truncated>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 50:
                result["_truncated"] = True
                break
            name = str(key)[:200]
            result[name] = (
                "<redacted>" if SENSITIVE_KEY.search(name) else _safe_details(item, depth=depth + 1)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_details(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return value[:2000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2000]


class AuditStore:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None):
        self.path = path
        self.clock = clock or (lambda: datetime.now(UTC))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(audit_events)").fetchall()
            }
            if "role" in columns:
                connection.executescript(
                    """
                    CREATE TABLE audit_events_without_roles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        occurred_at TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        action TEXT NOT NULL,
                        resource_type TEXT NOT NULL,
                        resource_id TEXT,
                        outcome TEXT NOT NULL CHECK (
                            outcome IN ('SUCCESS', 'DENIED', 'ERROR')
                        ),
                        request_id TEXT,
                        source_ip TEXT,
                        details_json TEXT NOT NULL
                    );
                    INSERT INTO audit_events_without_roles (
                        id, occurred_at, actor, action, resource_type, resource_id,
                        outcome, request_id, source_ip, details_json
                    )
                    SELECT
                        id, occurred_at, actor, action, resource_type, resource_id,
                        outcome, request_id, source_ip, details_json
                    FROM audit_events;
                    DROP TABLE audit_events;
                    ALTER TABLE audit_events_without_roles RENAME TO audit_events;
                    """
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_id TEXT,
                    outcome TEXT NOT NULL CHECK (outcome IN ('SUCCESS', 'DENIED', 'ERROR')),
                    request_id TEXT,
                    source_ip TEXT,
                    details_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS audit_events_resource_idx
                    ON audit_events (resource_type, resource_id, id DESC);
                CREATE INDEX IF NOT EXISTS audit_events_action_idx
                    ON audit_events (action, id DESC);
                CREATE TRIGGER IF NOT EXISTS audit_events_no_update
                    BEFORE UPDATE ON audit_events
                    BEGIN
                        SELECT RAISE(ABORT, 'audit events are append-only');
                    END;
                CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
                    BEFORE DELETE ON audit_events
                    BEGIN
                        SELECT RAISE(ABORT, 'audit events are append-only');
                    END;
                """
            )
            connection.execute("PRAGMA optimize")

    def record(
        self,
        *,
        actor: str,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        outcome: str = "SUCCESS",
        request_id: str | None = None,
        source_ip: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        occurred_at = self.clock()
        safe = _safe_details(details or {})
        encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 32_000:
            encoded = json.dumps({"_truncated": True}, separators=(",", ":"))
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO audit_events (
                    occurred_at, actor, action, resource_type, resource_id,
                    outcome, request_id, source_ip, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    occurred_at.isoformat(),
                    actor[:200],
                    action[:200],
                    resource_type[:100],
                    resource_id[:200] if resource_id else None,
                    outcome,
                    request_id[:200] if request_id else None,
                    source_ip[:200] if source_ip else None,
                    encoded,
                ),
            )
            event_id = cursor.lastrowid
        return AuditEvent(
            id=event_id,
            occurred_at=occurred_at,
            actor=actor[:200],
            action=action[:200],
            resource_type=resource_type[:100],
            resource_id=resource_id[:200] if resource_id else None,
            outcome=outcome,
            request_id=request_id[:200] if request_id else None,
            source_ip=source_ip[:200] if source_ip else None,
            details=safe,
        )

    def list(
        self,
        *,
        limit: int = 100,
        before_id: int | None = None,
        action: str | None = None,
        resource_id: str | None = None,
    ) -> list[AuditEvent]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if before_id is not None:
            clauses.append("id < ?")
            parameters.append(before_id)
        if action is not None:
            clauses.append("action = ?")
            parameters.append(action)
        if resource_id is not None:
            clauses.append("resource_id = ?")
            parameters.append(resource_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(max(1, min(limit, 500)))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM audit_events {where} ORDER BY id DESC LIMIT ?",
                parameters,
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> AuditEvent:
        return AuditEvent(
            id=row["id"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            actor=row["actor"],
            action=row["action"],
            resource_type=row["resource_type"],
            resource_id=row["resource_id"],
            outcome=row["outcome"],
            request_id=row["request_id"],
            source_ip=row["source_ip"],
            details=json.loads(row["details_json"]),
        )
