from datetime import UTC, datetime
from types import SimpleNamespace

from automation_orchestrator.models import PendingRetry, TriggerEvent, WorkflowInstance
from automation_orchestrator.worker import process_one, reconcile_workflows
from automation_orchestrator.workflow_queue import WorkflowQueue


def test_worker_defers_scheduled_workflow_retry(tmp_path):
    now = [300.0]
    queue = WorkflowQueue(tmp_path / "queue.sqlite3", clock=lambda: now[0])
    workflow = WorkflowInstance(
        id="workflow-1",
        scenario_id="retry-flow",
        scenario_version="1",
        trigger=TriggerEvent(source="manual", event="retry", event_id="event-1"),
        status="RUNNING",
        current_step="broken",
        pending_retry=PendingRetry(
            step_id="broken",
            iteration=1,
            next_attempt=2,
            available_at=datetime.fromtimestamp(310.0, UTC),
        ),
    )

    class FakeEngine:
        def get(self, workflow_id):
            assert workflow_id == workflow.id
            return workflow

        def advance_safely(self, current):
            assert current is workflow
            return current

    service = SimpleNamespace(workflow_queue=queue, workflow_engine=FakeEngine())
    queue.enqueue(workflow.id)

    assert process_one(service, worker_id="worker-a", heartbeat_seconds=0.01)

    deferred = queue.get(workflow.id)
    assert deferred.status == "PENDING"
    assert deferred.attempts == 0
    assert deferred.available_at == 310.0
    assert queue.claim("worker-b", lease_seconds=10) is None


def test_reconcile_recovers_missing_and_settled_queue_jobs(tmp_path):
    queue = WorkflowQueue(tmp_path / "queue.sqlite3")
    missing = WorkflowInstance(
        id="workflow-missing",
        scenario_id="flow",
        scenario_version="1",
        trigger=TriggerEvent(source="manual", event="run", event_id="missing"),
        status="CREATED",
        current_step="work",
    )
    settled = missing.model_copy(
        update={
            "id": "workflow-settled",
            "trigger": TriggerEvent(source="manual", event="run", event_id="settled"),
            "status": "RUNNING",
        }
    )
    waiting = missing.model_copy(
        update={
            "id": "workflow-waiting",
            "trigger": TriggerEvent(source="manual", event="run", event_id="waiting"),
            "status": "WAITING",
        }
    )
    queue.enqueue(settled.id)
    claimed = queue.claim("worker-a", lease_seconds=10)
    assert claimed is not None
    queue.complete(settled.id, "worker-a")

    class FakeStore:
        @staticmethod
        def list():
            return [missing, settled, waiting]

    class FakeEngine:
        store = FakeStore()

    service = SimpleNamespace(workflow_queue=queue, workflow_engine=FakeEngine())

    result = reconcile_workflows(service)

    assert result == {"recovered": 2, "failed": 0}
    assert queue.get(missing.id).status == "PENDING"
    assert queue.get(settled.id).status == "PENDING"
    assert queue.get(waiting.id) is None


def test_worker_marks_workflow_failed_when_queue_retries_are_exhausted(tmp_path):
    queue = WorkflowQueue(tmp_path / "queue.sqlite3")
    workflow = WorkflowInstance(
        id="workflow-broken",
        scenario_id="flow",
        scenario_version="1",
        trigger=TriggerEvent(source="manual", event="run", event_id="broken"),
        status="RUNNING",
        current_step="work",
    )

    class FakeEngine:
        def __init__(self):
            self.failure = None

        def get(self, workflow_id):
            assert workflow_id == workflow.id
            return workflow

        @staticmethod
        def advance_safely(current):
            raise RuntimeError("persistent storage failure")

        def fail_processing(self, workflow_id, *, message):
            self.failure = (workflow_id, message)

    engine = FakeEngine()
    service = SimpleNamespace(workflow_queue=queue, workflow_engine=engine)
    queue.enqueue(workflow.id)

    assert process_one(
        service,
        worker_id="worker-a",
        heartbeat_seconds=0.01,
        max_attempts=1,
    )

    assert queue.get(workflow.id).status == "FAILED"
    assert engine.failure == (workflow.id, "persistent storage failure")
