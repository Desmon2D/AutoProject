from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

QueueStatus = Literal["PENDING", "RUNNING", "COMPLETED", "FAILED"]


@dataclass(frozen=True)
class QueueJob:
    workflow_id: str
    status: QueueStatus
    attempts: int
    available_at: float
    lease_until: float | None
    worker_id: str | None
    requeue_requested: bool
    last_error: str | None
    created_at: float
    updated_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> QueueJob:
        return cls(
            workflow_id=row["workflow_id"],
            status=row["status"],
            attempts=row["attempts"],
            available_at=row["available_at"],
            lease_until=row["lease_until"],
            worker_id=row["worker_id"],
            requeue_requested=bool(row["requeue_requested"]),
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class WorkflowQueue:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self.path = path
        self.clock = clock
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_queue (
                    workflow_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL CHECK (
                        status IN ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED')
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    available_at REAL NOT NULL,
                    lease_until REAL,
                    worker_id TEXT,
                    requeue_requested INTEGER NOT NULL DEFAULT 0 CHECK (
                        requeue_requested IN (0, 1)
                    ),
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS workflow_queue_claim_idx
                    ON workflow_queue (status, available_at, created_at);
                CREATE TABLE IF NOT EXISTS worker_heartbeats (
                    worker_id TEXT PRIMARY KEY,
                    heartbeat_at REAL NOT NULL
                );
                """
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(workflow_queue)").fetchall()
            }
            if "requeue_requested" not in columns:
                connection.execute(
                    "ALTER TABLE workflow_queue "
                    "ADD COLUMN requeue_requested INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute("PRAGMA optimize")

    def enqueue(
        self,
        workflow_id: str,
        *,
        available_at: float | None = None,
        requeue_if_running: bool = False,
    ) -> bool:
        now = self.clock()
        ready_at = now if available_at is None else available_at
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO workflow_queue (
                    workflow_id, status, attempts, available_at, lease_until,
                    worker_id, requeue_requested, last_error, created_at, updated_at
                ) VALUES (?, 'PENDING', 0, ?, NULL, NULL, 0, NULL, ?, ?)
                ON CONFLICT(workflow_id) DO UPDATE SET
                    status = 'PENDING', attempts = 0, available_at = excluded.available_at,
                    lease_until = NULL, worker_id = NULL, requeue_requested = 0,
                    last_error = NULL,
                    updated_at = excluded.updated_at
                WHERE workflow_queue.status IN ('COMPLETED', 'FAILED')
                """,
                (workflow_id, ready_at, now, now),
            )
            changed = cursor.rowcount == 1
            if not changed and requeue_if_running:
                cursor = connection.execute(
                    """
                    UPDATE workflow_queue
                    SET requeue_requested = 1, updated_at = ?
                    WHERE workflow_id = ? AND status = 'RUNNING'
                    """,
                    (now, workflow_id),
                )
                changed = cursor.rowcount == 1
            connection.commit()
            return changed

    def claim(self, worker_id: str, *, lease_seconds: float) -> QueueJob | None:
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE workflow_queue
                SET status = 'PENDING', available_at = ?, lease_until = NULL,
                    worker_id = NULL, requeue_requested = 0,
                    last_error = 'worker lease expired', updated_at = ?
                WHERE status = 'RUNNING' AND lease_until <= ?
                """,
                (now, now, now),
            )
            row = connection.execute(
                """
                SELECT * FROM workflow_queue
                WHERE status = 'PENDING' AND available_at <= ?
                ORDER BY available_at, created_at
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            workflow_id = row["workflow_id"]
            connection.execute(
                """
                UPDATE workflow_queue
                SET status = 'RUNNING', attempts = attempts + 1,
                    lease_until = ?, worker_id = ?, last_error = NULL, updated_at = ?
                WHERE workflow_id = ?
                """,
                (now + lease_seconds, worker_id, now, workflow_id),
            )
            claimed = connection.execute(
                "SELECT * FROM workflow_queue WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            connection.commit()
        return QueueJob.from_row(claimed)

    def renew(self, workflow_id: str, worker_id: str, *, lease_seconds: float) -> bool:
        now = self.clock()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE workflow_queue
                SET lease_until = ?, updated_at = ?
                WHERE workflow_id = ? AND status = 'RUNNING' AND worker_id = ?
                """,
                (now + lease_seconds, now, workflow_id, worker_id),
            )
            return cursor.rowcount == 1

    def complete(self, workflow_id: str, worker_id: str) -> bool:
        now = self.clock()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE workflow_queue
                SET status = CASE
                        WHEN requeue_requested = 1 THEN 'PENDING'
                        ELSE 'COMPLETED'
                    END,
                    attempts = CASE WHEN requeue_requested = 1 THEN 0 ELSE attempts END,
                    available_at = CASE
                        WHEN requeue_requested = 1 THEN ?
                        ELSE available_at
                    END,
                    lease_until = NULL, worker_id = NULL, requeue_requested = 0,
                    last_error = NULL, updated_at = ?
                WHERE workflow_id = ? AND status = 'RUNNING' AND worker_id = ?
                """,
                (now, now, workflow_id, worker_id),
            )
            return cursor.rowcount == 1

    def defer(
        self,
        workflow_id: str,
        worker_id: str,
        *,
        available_at: float,
        error: str | None = None,
    ) -> bool:
        now = self.clock()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE workflow_queue
                SET status = 'PENDING', attempts = 0, available_at = ?, lease_until = NULL,
                    worker_id = NULL, requeue_requested = 0, last_error = ?, updated_at = ?
                WHERE workflow_id = ? AND status = 'RUNNING' AND worker_id = ?
                """,
                (
                    max(now, available_at),
                    error[-2000:] if error else None,
                    now,
                    workflow_id,
                    worker_id,
                ),
            )
            return cursor.rowcount == 1

    def cancel_pending(self, workflow_id: str) -> bool:
        now = self.clock()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE workflow_queue
                SET status = 'COMPLETED', lease_until = NULL, worker_id = NULL,
                    requeue_requested = 0, last_error = 'workflow cancelled', updated_at = ?
                WHERE workflow_id = ? AND status = 'PENDING'
                """,
                (now, workflow_id),
            )
            return cursor.rowcount == 1

    def release(
        self,
        workflow_id: str,
        worker_id: str,
        *,
        error: str,
        max_attempts: int,
        retry_delay_seconds: float,
    ) -> QueueStatus:
        now = self.clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT attempts FROM workflow_queue
                WHERE workflow_id = ? AND status = 'RUNNING' AND worker_id = ?
                """,
                (workflow_id, worker_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise RuntimeError("worker no longer owns the queue lease")
            status: QueueStatus = "FAILED" if row["attempts"] >= max_attempts else "PENDING"
            available_at = now if status == "FAILED" else now + retry_delay_seconds
            connection.execute(
                """
                UPDATE workflow_queue
                SET status = ?, available_at = ?, lease_until = NULL, worker_id = NULL,
                    requeue_requested = 0, last_error = ?, updated_at = ?
                WHERE workflow_id = ?
                """,
                (status, available_at, error[-2000:], now, workflow_id),
            )
            connection.commit()
            return status

    def heartbeat(self, worker_id: str) -> None:
        now = self.clock()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO worker_heartbeats (worker_id, heartbeat_at) VALUES (?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at
                """,
                (worker_id, now),
            )

    def get(self, workflow_id: str) -> QueueJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_queue WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
        return QueueJob.from_row(row) if row is not None else None

    def summary(self, *, stale_after_seconds: float = 15) -> dict[str, int | bool | float | None]:
        now = self.clock()
        counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
        with self._connect() as connection:
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM workflow_queue GROUP BY status"
            ):
                counts[row["status"].lower()] = row["count"]
            heartbeat = connection.execute(
                "SELECT MAX(heartbeat_at) AS heartbeat_at FROM worker_heartbeats"
            ).fetchone()["heartbeat_at"]
        return {
            **counts,
            "worker_online": heartbeat is not None and now - heartbeat <= stale_after_seconds,
            "worker_last_heartbeat": heartbeat,
        }
