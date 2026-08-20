from __future__ import annotations

import logging
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

from .bootstrap import build_service
from .service import AgentService

LOGGER = logging.getLogger(__name__)


def cleanup_expired_artifacts(service: AgentService, *, worker_id: str):
    result = service.job_store.cleanup_expired()
    if result.examined or result.failed:
        service.audit_store.record(
            actor=worker_id,
            action="artifacts.cleanup.completed",
            resource_type="artifact-registry",
            details=result.model_dump(mode="json"),
        )
        LOGGER.info(
            "artifact cleanup: examined=%s removed=%s failed=%s",
            result.examined,
            result.removed_records,
            len(result.failed),
        )
    return result


def _advance(service: AgentService, workflow_id: str):
    workflow = service.workflow_engine.get(workflow_id)
    if workflow is None:
        raise RuntimeError(f"queued workflow does not exist: {workflow_id}")
    if workflow.status in {"WAITING", "COMPLETED", "FAILED", "CANCELLED"}:
        return workflow
    return service.workflow_engine.advance_safely(workflow)


def reconcile_workflows(service: AgentService) -> dict[str, int]:
    recovered = 0
    failed = 0
    for workflow in service.workflow_engine.store.list():
        if workflow.status not in {"CREATED", "RUNNING"}:
            continue
        queue_job = service.workflow_queue.get(workflow.id)
        if queue_job is not None and queue_job.status == "FAILED":
            service.workflow_engine.fail_processing(
                workflow.id,
                message=queue_job.last_error or "workflow queue exhausted its retries",
            )
            failed += 1
            continue
        if queue_job is None or queue_job.status == "COMPLETED":
            available_at = (
                workflow.pending_retry.available_at.timestamp()
                if workflow.pending_retry is not None
                else None
            )
            if service.workflow_queue.enqueue(workflow.id, available_at=available_at):
                recovered += 1
    return {"recovered": recovered, "failed": failed}


def process_one(
    service: AgentService,
    *,
    worker_id: str,
    lease_seconds: float = 30,
    heartbeat_seconds: float = 5,
    max_attempts: int = 3,
    retry_delay_seconds: float = 5,
) -> bool:
    queue = service.workflow_queue
    queue.heartbeat(worker_id)
    job = queue.claim(worker_id, lease_seconds=lease_seconds)
    if job is None:
        return False

    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="workflow") as executor:
            future = executor.submit(_advance, service, job.workflow_id)
            while True:
                try:
                    workflow = future.result(timeout=heartbeat_seconds)
                    break
                except FutureTimeoutError:
                    queue.heartbeat(worker_id)
                    if not queue.renew(
                        job.workflow_id,
                        worker_id,
                        lease_seconds=lease_seconds,
                    ):
                        raise RuntimeError("worker lost the queue lease")
        if workflow.pending_retry is not None and workflow.status == "RUNNING":
            retry_error = workflow.error.message if workflow.error else None
            if not queue.defer(
                job.workflow_id,
                worker_id,
                available_at=workflow.pending_retry.available_at.timestamp(),
                error=retry_error,
            ):
                raise RuntimeError("worker could not defer the queue job")
            LOGGER.info(
                "workflow retry scheduled: %s at %s",
                job.workflow_id,
                workflow.pending_retry.available_at.isoformat(),
            )
        else:
            if not queue.complete(job.workflow_id, worker_id):
                raise RuntimeError("worker could not complete the queue job")
            LOGGER.info("workflow completed: %s", job.workflow_id)
    except (OSError, RuntimeError, ValueError) as exc:
        status = queue.release(
            job.workflow_id,
            worker_id,
            error=str(exc),
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
        )
        if status == "FAILED":
            service.workflow_engine.fail_processing(
                job.workflow_id,
                message=str(exc),
            )
        LOGGER.exception("workflow processing failed (%s): %s", status, job.workflow_id)
    finally:
        queue.heartbeat(worker_id)
    return True


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    service = build_service()
    worker_id = os.environ.get("WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
    poll_seconds = float(os.environ.get("WORKER_POLL_SECONDS", "1"))
    lease_seconds = float(os.environ.get("WORKER_LEASE_SECONDS", "30"))
    heartbeat_seconds = float(os.environ.get("WORKER_HEARTBEAT_SECONDS", "5"))
    max_attempts = int(os.environ.get("WORKER_MAX_ATTEMPTS", "3"))
    retry_delay_seconds = float(os.environ.get("WORKER_RETRY_DELAY_SECONDS", "5"))
    cleanup_interval_seconds = float(os.environ.get("ARTIFACT_CLEANUP_INTERVAL_SECONDS", "3600"))
    reconcile_interval_seconds = float(
        os.environ.get("WORKFLOW_RECONCILE_INTERVAL_SECONDS", "60")
    )
    last_cleanup = 0.0
    last_reconcile = 0.0
    LOGGER.info("workflow worker started: %s", worker_id)
    while True:
        current = time.monotonic()
        if cleanup_interval_seconds > 0 and current - last_cleanup >= cleanup_interval_seconds:
            cleanup_expired_artifacts(service, worker_id=worker_id)
            last_cleanup = current
        if reconcile_interval_seconds > 0 and current - last_reconcile >= reconcile_interval_seconds:
            result = reconcile_workflows(service)
            if result["recovered"] or result["failed"]:
                LOGGER.warning("workflow reconciliation: %s", result)
            last_reconcile = current
        processed = process_one(
            service,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
            max_attempts=max_attempts,
            retry_delay_seconds=retry_delay_seconds,
        )
        if not processed:
            time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
