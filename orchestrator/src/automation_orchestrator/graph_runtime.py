from __future__ import annotations

import hashlib
import sqlite3
from uuid import uuid4

from .flow_compiler import compile_flow
from .flow_store import FlowStore
from .graph_run_store import GraphRunStore
from .models import FlowRun, ReviewDecision, TriggerEvent, WorkflowInstance
from .workflow_engine import WorkflowEngine
from .workflow_queue import WorkflowQueue


class GraphRuntimeError(RuntimeError):
    pass


class GraphRuntime:
    def __init__(
        self,
        flow_store: FlowStore,
        run_store: GraphRunStore,
        workflow_engine: WorkflowEngine,
        workflow_queue: WorkflowQueue,
    ):
        self.flow_store = flow_store
        self.run_store = run_store
        self.workflow_engine = workflow_engine
        self.workflow_queue = workflow_queue

    def start(
        self, flow_id: str, *, version: int | None = None, inputs: dict | None = None
    ) -> FlowRun:
        published = self.flow_store.get_version(flow_id, version)
        if published is None:
            suffix = "latest" if version is None else str(version)
            raise GraphRuntimeError(f"published flow version not found: {flow_id}@{suffix}")
        scenario = compile_flow(published.definition)
        run_id = f"flow-run-{uuid4().hex}"
        run_inputs = inputs or {}
        trigger_event = TriggerEvent(
            source="manual",
            event="flow.run",
            event_id=run_id,
            data={
                **run_inputs,
                "flow": {"id": scenario.id, "version": scenario.version},
            },
        )
        run = self.run_store.register(
            run_id=run_id,
            version=published,
            scenario=scenario,
            inputs=run_inputs,
            trigger_event=trigger_event,
        )
        self.dispatch(run_id)
        return self.get(run_id) or run

    def start_matching(self, event: TriggerEvent) -> list[FlowRun]:
        runs: list[FlowRun] = []
        for published in self.flow_store.matching_versions(event.source, event.event):
            identity = (
                f"{published.flow_id}\0{event.source}\0{event.event}\0{event.event_id}"
            )
            run_id = f"flow-run-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
            existing = self.get(run_id)
            if existing is not None:
                runs.append(existing)
                continue
            scenario = compile_flow(published.definition)
            trigger_event = event.model_copy(
                update={
                    "data": {
                        **event.data,
                        "flow": {"id": scenario.id, "version": scenario.version},
                    }
                }
            )
            try:
                run = self.run_store.register(
                    run_id=run_id,
                    version=published,
                    scenario=scenario,
                    inputs=event.data,
                    trigger_event=trigger_event,
                )
            except sqlite3.IntegrityError:
                run = self.get(run_id)
                if run is None:  # pragma: no cover - concurrent transaction invariant
                    raise
            self.dispatch(run_id)
            runs.append(self.get(run_id) or run)
        return runs

    def dispatch(self, run_id: str) -> bool:
        payload = self.run_store.dispatch_payload(run_id)
        if payload is None:
            raise GraphRuntimeError(f"unknown flow run: {run_id}")
        scenario, _, event = payload
        workflow, _ = self.workflow_engine.create_for_scenario(
            scenario, event, workflow_id=run_id
        )
        queued = self.workflow_queue.enqueue(workflow.id)
        queue_job = self.workflow_queue.get(workflow.id)
        if not queued and (
            queue_job is None or queue_job.status not in {"PENDING", "RUNNING", "COMPLETED"}
        ):
            raise GraphRuntimeError("flow run could not be queued")
        self.run_store.sync(workflow)
        self.run_store.mark_dispatched(run_id)
        return queued

    def dispatch_pending(self) -> int:
        dispatched = 0
        for run_id in self.run_store.pending_outbox():
            self.dispatch(run_id)
            dispatched += 1
        dispatched += self.dispatch_node_outbox()
        return dispatched

    def dispatch_node_outbox(self, run_id: str | None = None) -> int:
        dispatched = 0
        for delivery_id, delivery_run_id in self.run_store.pending_node_outbox(
            run_id=run_id
        ):
            workflow = self.workflow_engine.get(delivery_run_id)
            if workflow is None:
                raise GraphRuntimeError(f"unknown flow workflow: {delivery_run_id}")
            if workflow.status == "RUNNING":
                queued = self.workflow_queue.enqueue(
                    workflow.id,
                    requeue_if_running=True,
                )
                queue_job = self.workflow_queue.get(workflow.id)
                if not queued and (
                    queue_job is None or queue_job.status not in {"PENDING", "RUNNING"}
                ):
                    raise GraphRuntimeError("flow node continuation could not be queued")
            self.run_store.mark_node_dispatched(delivery_id)
            dispatched += 1
        return dispatched

    def sync(self, workflow: WorkflowInstance) -> bool:
        return self.run_store.sync(workflow)

    def get(self, run_id: str) -> FlowRun | None:
        workflow = self.workflow_engine.get(run_id)
        if workflow is not None:
            self.run_store.sync(workflow)
        return self.run_store.get(run_id)

    def list(self, *, limit: int = 100) -> list[FlowRun]:
        for run in self.run_store.list(limit=limit):
            workflow = self.workflow_engine.get(run.workflow_id)
            if workflow is not None:
                self.run_store.sync(workflow)
        return self.run_store.list(limit=limit)

    def review(self, run_id: str, decision: ReviewDecision) -> FlowRun:
        if self.run_store.get(run_id) is None:
            raise GraphRuntimeError(f"unknown flow run: {run_id}")
        workflow = self.workflow_engine.review(run_id, decision, advance=False)
        self.sync(workflow)
        self.dispatch_node_outbox(run_id)
        return self.get(run_id)  # type: ignore[return-value]

    def cancel(self, run_id: str, *, reason: str) -> FlowRun:
        if self.run_store.get(run_id) is None:
            raise GraphRuntimeError(f"unknown flow run: {run_id}")
        workflow = self.workflow_engine.cancel(run_id, reason=reason)
        self.workflow_queue.cancel_pending(workflow.id)
        self.sync(workflow)
        return self.get(run_id)  # type: ignore[return-value]

    def retry(self, run_id: str, *, reason: str) -> FlowRun:
        if self.run_store.get(run_id) is None:
            raise GraphRuntimeError(f"unknown flow run: {run_id}")
        job = self.workflow_queue.get(run_id)
        if job is not None and job.status in {"PENDING", "RUNNING"}:
            raise GraphRuntimeError("flow run already has active queue work")
        workflow = self.workflow_engine.retry(run_id, reason=reason)
        if not self.workflow_queue.enqueue(workflow.id):
            raise GraphRuntimeError("flow run retry could not be queued")
        self.sync(workflow)
        return self.get(run_id)  # type: ignore[return-value]

    def retry_node(self, run_id: str, node_id: str, *, reason: str) -> FlowRun:
        run = self.get(run_id)
        if run is None:
            raise GraphRuntimeError(f"unknown flow run: {run_id}")
        if run.status != "FAILED":
            raise GraphRuntimeError(f"flow node cannot be retried from run status {run.status}")
        if run.current_node != node_id:
            raise GraphRuntimeError(
                f"only the current failed node may be retried: {run.current_node}"
            )
        return self.retry(run_id, reason=reason)
