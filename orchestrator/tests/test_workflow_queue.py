import sqlite3
from pathlib import Path

from automation_orchestrator.workflow_queue import WorkflowQueue


def test_queue_claim_is_idempotent_and_recovers_expired_lease(tmp_path: Path):
    now = [100.0]
    queue = WorkflowQueue(tmp_path / "queue.sqlite3", clock=lambda: now[0])

    assert queue.enqueue("wf-1") is True
    assert queue.enqueue("wf-1") is False
    queue = WorkflowQueue(tmp_path / "queue.sqlite3", clock=lambda: now[0])
    first = queue.claim("worker-a", lease_seconds=10)

    assert first.workflow_id == "wf-1"
    assert first.attempts == 1
    assert queue.claim("worker-b", lease_seconds=10) is None

    now[0] = 111.0
    recovered = queue.claim("worker-b", lease_seconds=10)

    assert recovered.workflow_id == "wf-1"
    assert recovered.worker_id == "worker-b"
    assert recovered.attempts == 2
    assert queue.complete("wf-1", "worker-b") is True
    assert queue.get("wf-1").status == "COMPLETED"
    assert queue.enqueue("wf-1") is True
    resumed = queue.claim("worker-c", lease_seconds=10)
    assert resumed.workflow_id == "wf-1"
    assert resumed.attempts == 1


def test_queue_retries_then_fails_and_reports_worker_health(tmp_path: Path):
    now = [200.0]
    queue = WorkflowQueue(tmp_path / "queue.sqlite3", clock=lambda: now[0])
    queue.enqueue("wf-2")
    queue.heartbeat("worker-a")

    first = queue.claim("worker-a", lease_seconds=10)
    status = queue.release(
        first.workflow_id,
        "worker-a",
        error="temporary failure",
        max_attempts=2,
        retry_delay_seconds=5,
    )

    assert status == "PENDING"
    assert queue.claim("worker-a", lease_seconds=10) is None
    now[0] = 205.0
    second = queue.claim("worker-a", lease_seconds=10)
    status = queue.release(
        second.workflow_id,
        "worker-a",
        error="permanent failure",
        max_attempts=2,
        retry_delay_seconds=5,
    )

    assert status == "FAILED"
    assert queue.summary()["failed"] == 1
    assert queue.summary()["worker_online"] is True
    now[0] = 221.0
    assert queue.summary()["worker_online"] is False


def test_queue_can_settle_pending_cancelled_work(tmp_path: Path):
    queue = WorkflowQueue(tmp_path / "queue.sqlite3")
    queue.enqueue("wf-cancel")

    assert queue.cancel_pending("wf-cancel") is True
    assert queue.get("wf-cancel").status == "COMPLETED"
    assert queue.claim("worker-a", lease_seconds=10) is None


def test_queue_defers_claimed_work_until_retry_time(tmp_path: Path):
    now = [300.0]
    queue = WorkflowQueue(tmp_path / "queue.sqlite3", clock=lambda: now[0])
    queue.enqueue("wf-deferred")
    claimed = queue.claim("worker-a", lease_seconds=10)

    assert claimed is not None
    assert queue.defer(
        claimed.workflow_id,
        "worker-a",
        available_at=310.0,
        error="temporary service failure",
    )
    deferred = queue.get("wf-deferred")
    assert deferred.status == "PENDING"
    assert deferred.available_at == 310.0
    assert deferred.last_error == "temporary service failure"
    assert queue.claim("worker-b", lease_seconds=10) is None

    now[0] = 310.0
    retried = queue.claim("worker-b", lease_seconds=10)

    assert retried is not None
    assert retried.attempts == 1
    assert retried.worker_id == "worker-b"


def test_queue_preserves_resume_requested_while_worker_is_finishing(tmp_path: Path):
    now = [400.0]
    queue = WorkflowQueue(tmp_path / "queue.sqlite3", clock=lambda: now[0])
    queue.enqueue("wf-review")
    claimed = queue.claim("worker-a", lease_seconds=10)

    assert claimed is not None
    assert queue.enqueue("wf-review") is False
    assert queue.enqueue("wf-review", requeue_if_running=True) is True
    assert queue.get("wf-review").requeue_requested is True

    now[0] = 401.0
    assert queue.complete("wf-review", "worker-a") is True
    resumed = queue.get("wf-review")
    assert resumed.status == "PENDING"
    assert resumed.requeue_requested is False
    assert resumed.available_at == 401.0
    assert queue.claim("worker-b", lease_seconds=10) is not None


def test_queue_migrates_existing_database_for_safe_requeue(tmp_path: Path):
    path = tmp_path / "queue.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE workflow_queue (
                workflow_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL,
                available_at REAL NOT NULL,
                lease_until REAL,
                worker_id TEXT,
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO workflow_queue VALUES (
                'wf-existing', 'COMPLETED', 1, 100, NULL, NULL, NULL, 100, 100
            )
            """
        )

    queue = WorkflowQueue(path, clock=lambda: 200.0)

    assert queue.get("wf-existing").requeue_requested is False
    assert queue.enqueue("wf-existing") is True
    assert queue.get("wf-existing").status == "PENDING"
