from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import (
    ActivatedFlowEdge,
    FlowDefinition,
    FlowNodeRun,
    FlowRun,
    FlowVersion,
    ScenarioManifest,
    StepError,
    StepResult,
    StepStatusChange,
    TriggerEvent,
    WorkflowInstance,
)


class GraphRunStore:
    def __init__(self, path: Path):
        self.path = path
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
                CREATE TABLE IF NOT EXISTS flow_runs (
                    run_id TEXT PRIMARY KEY,
                    flow_id TEXT NOT NULL,
                    flow_version INTEGER NOT NULL,
                    flow_sha256 TEXT NOT NULL,
                    workflow_id TEXT NOT NULL UNIQUE,
                    definition_json TEXT NOT NULL,
                    scenario_json TEXT NOT NULL,
                    inputs_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    outcome TEXT,
                    current_node TEXT,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flow_node_runs (
                    node_run_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES flow_runs(run_id) ON DELETE CASCADE,
                    node_id TEXT NOT NULL,
                    iteration INTEGER NOT NULL,
                    attempt INTEGER NOT NULL,
                    execution_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS flow_node_runs_run_idx
                    ON flow_node_runs (run_id, created_at);
                CREATE TABLE IF NOT EXISTS flow_activated_edges (
                    activation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES flow_runs(run_id) ON DELETE CASCADE,
                    edge_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    target TEXT NOT NULL,
                    outcome TEXT,
                    node_run_id TEXT,
                    activated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS flow_edges_run_idx
                    ON flow_activated_edges (run_id, activated_at);
                CREATE TABLE IF NOT EXISTS flow_run_outbox (
                    run_id TEXT PRIMARY KEY REFERENCES flow_runs(run_id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK (status IN ('PENDING', 'DISPATCHED')),
                    created_at TEXT NOT NULL,
                    dispatched_at TEXT
                );
                CREATE TABLE IF NOT EXISTS flow_node_outbox (
                    delivery_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES flow_runs(run_id) ON DELETE CASCADE,
                    node_run_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('PENDING', 'DISPATCHED')),
                    created_at TEXT NOT NULL,
                    dispatched_at TEXT
                );
                CREATE INDEX IF NOT EXISTS flow_node_outbox_pending_idx
                    ON flow_node_outbox (status, created_at);
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(flow_runs)")
            }
            if "trigger_json" not in columns:
                connection.execute("ALTER TABLE flow_runs ADD COLUMN trigger_json TEXT")

    def register(
        self,
        *,
        run_id: str,
        version: FlowVersion,
        scenario: ScenarioManifest,
        inputs: dict,
        trigger_event: TriggerEvent,
    ) -> FlowRun:
        now = datetime.now(UTC)
        trigger = next(node for node in version.definition.nodes if node.type == "trigger")
        trigger_edge = next(edge for edge in version.definition.edges if edge.source == trigger.id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO flow_runs (
                    run_id, flow_id, flow_version, flow_sha256, workflow_id,
                    definition_json, scenario_json, inputs_json, trigger_json, status, outcome,
                    current_node, error_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'CREATED', NULL, ?, NULL, ?, ?)
                """,
                (
                    run_id,
                    version.flow_id,
                    version.version,
                    version.sha256,
                    run_id,
                    version.definition.model_dump_json(),
                    scenario.model_dump_json(),
                    json.dumps(inputs, ensure_ascii=False),
                    trigger_event.model_dump_json(),
                    version.definition.start_node,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            for node in version.definition.nodes:
                if node.type not in {
                    "agent",
                    "command",
                    "review",
                    "if",
                    "switch",
                    "delay",
                    "merge",
                }:
                    continue
                status = "READY" if node.id == version.definition.start_node else "PENDING"
                execution = StepResult(
                    step_id=node.id,
                    execution_id=f"{run_id}-{node.id}-1-1",
                    iteration=1,
                    attempt=1,
                    execution_status=status,
                    outcome=None,
                    status_history=[StepStatusChange(status=status, occurred_at=now)],
                )
                connection.execute(
                    """
                    INSERT INTO flow_node_runs (
                        node_run_id, run_id, node_id, iteration, attempt,
                        execution_json, created_at, updated_at
                    ) VALUES (?, ?, ?, 1, 1, ?, ?, ?)
                    """,
                    (
                        execution.execution_id,
                        run_id,
                        node.id,
                        execution.model_dump_json(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            connection.execute(
                """
                INSERT INTO flow_activated_edges (
                    activation_id, run_id, edge_id, source, target, outcome,
                    node_run_id, activated_at
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    f"{run_id}:trigger",
                    run_id,
                    trigger_edge.id,
                    trigger_edge.source,
                    trigger_edge.target,
                    now.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO flow_run_outbox (run_id, status, created_at) VALUES (?, 'PENDING', ?)",
                (run_id, now.isoformat()),
            )
            connection.commit()
        result = self.get(run_id)
        if result is None:  # pragma: no cover - guarded by the transaction
            raise RuntimeError("registered flow run disappeared")
        return result

    def pending_outbox(self, *, limit: int = 100) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT run_id FROM flow_run_outbox
                WHERE status = 'PENDING' ORDER BY created_at LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [row["run_id"] for row in rows]

    def dispatch_payload(
        self, run_id: str
    ) -> tuple[ScenarioManifest, dict, TriggerEvent] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT scenario_json, inputs_json, trigger_json FROM flow_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        scenario = ScenarioManifest.model_validate_json(row["scenario_json"])
        inputs = json.loads(row["inputs_json"])
        trigger = (
            TriggerEvent.model_validate_json(row["trigger_json"])
            if row["trigger_json"]
            else TriggerEvent(
                source="manual",
                event="flow.run",
                event_id=run_id,
                data={**inputs, "flow": {"id": scenario.id, "version": scenario.version}},
            )
        )
        return scenario, inputs, trigger

    def mark_dispatched(self, run_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE flow_run_outbox SET status = 'DISPATCHED', dispatched_at = ?
                WHERE run_id = ?
                """,
                (datetime.now(UTC).isoformat(), run_id),
            )

    def contains(self, run_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM flow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return row is not None

    def pending_node_outbox(
        self,
        *,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[tuple[str, str]]:
        query = (
            "SELECT delivery_id, run_id FROM flow_node_outbox "
            "WHERE status = 'PENDING'"
        )
        parameters: tuple = ()
        if run_id is not None:
            query += " AND run_id = ?"
            parameters = (run_id,)
        query += " ORDER BY created_at, delivery_id LIMIT ?"
        parameters = (*parameters, limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [(row["delivery_id"], row["run_id"]) for row in rows]

    def mark_node_dispatched(self, delivery_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE flow_node_outbox
                SET status = 'DISPATCHED', dispatched_at = ?
                WHERE delivery_id = ?
                """,
                (datetime.now(UTC).isoformat(), delivery_id),
            )

    def sync(self, workflow: WorkflowInstance) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT definition_json FROM flow_runs WHERE workflow_id = ?",
                (workflow.id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            definition = FlowDefinition.model_validate_json(row["definition_json"])
            edge_by_outcome = {
                (edge.source, edge.outcome): edge
                for edge in definition.edges
                if edge.outcome is not None
            }
            terminal_node: str | None = None
            for execution in workflow.executions:
                created_at = (
                    execution.status_history[0].occurred_at
                    if execution.status_history
                    else workflow.created_at
                )
                updated_at = (
                    execution.status_history[-1].occurred_at
                    if execution.status_history
                    else workflow.updated_at
                )
                connection.execute(
                    """
                    INSERT INTO flow_node_runs (
                        node_run_id, run_id, node_id, iteration, attempt,
                        execution_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(node_run_id) DO UPDATE SET
                        execution_json = excluded.execution_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        execution.execution_id,
                        workflow.id,
                        execution.step_id,
                        execution.iteration,
                        execution.attempt,
                        execution.model_dump_json(),
                        created_at.isoformat(),
                        updated_at.isoformat(),
                    ),
                )
                if execution.execution_status == "COMPLETED" and workflow.status == "RUNNING":
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO flow_node_outbox (
                            delivery_id, run_id, node_run_id, status, created_at
                        ) VALUES (?, ?, ?, 'PENDING', ?)
                        """,
                        (
                            f"{execution.execution_id}:continue",
                            workflow.id,
                            execution.execution_id,
                            updated_at.isoformat(),
                        ),
                    )
                if execution.execution_status != "COMPLETED" or execution.outcome is None:
                    continue
                edge = edge_by_outcome.get((execution.step_id, execution.outcome))
                if edge is None:
                    continue
                activated_at = updated_at
                connection.execute(
                    """
                    INSERT OR IGNORE INTO flow_activated_edges (
                        activation_id, run_id, edge_id, source, target, outcome,
                        node_run_id, activated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"{execution.execution_id}:{edge.id}",
                        workflow.id,
                        edge.id,
                        edge.source,
                        edge.target,
                        edge.outcome,
                        execution.execution_id,
                        activated_at.isoformat(),
                    ),
                )
                target = next(node for node in definition.nodes if node.id == edge.target)
                if target.type == "terminal":
                    terminal_node = target.id
            current_node = workflow.current_step or terminal_node
            error_json = workflow.error.model_dump_json() if workflow.error else None
            connection.execute(
                """
                UPDATE flow_runs SET status = ?, outcome = ?, current_node = ?,
                    error_json = ?, updated_at = ? WHERE workflow_id = ?
                """,
                (
                    workflow.status,
                    workflow.outcome,
                    current_node,
                    error_json,
                    workflow.updated_at.isoformat(),
                    workflow.id,
                ),
            )
            if workflow.status == "COMPLETED":
                pending_rows = connection.execute(
                    "SELECT node_run_id, execution_json FROM flow_node_runs WHERE run_id = ?",
                    (workflow.id,),
                ).fetchall()
                for pending_row in pending_rows:
                    pending = StepResult.model_validate_json(pending_row["execution_json"])
                    if pending.execution_status not in {"PENDING", "READY"}:
                        continue
                    skipped = pending.model_copy(
                        update={
                            "execution_status": "SKIPPED",
                            "status_history": [
                                *pending.status_history,
                                StepStatusChange(
                                    status="SKIPPED", occurred_at=workflow.updated_at
                                ),
                            ],
                        }
                    )
                    connection.execute(
                        """
                        UPDATE flow_node_runs SET execution_json = ?, updated_at = ?
                        WHERE node_run_id = ?
                        """,
                        (
                            skipped.model_dump_json(),
                            workflow.updated_at.isoformat(),
                            pending_row["node_run_id"],
                        ),
                    )
            connection.commit()
        return True

    def get(self, run_id: str) -> FlowRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM flow_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            node_rows = connection.execute(
                "SELECT * FROM flow_node_runs WHERE run_id = ? ORDER BY created_at, node_run_id",
                (run_id,),
            ).fetchall()
            edge_rows = connection.execute(
                "SELECT * FROM flow_activated_edges WHERE run_id = ? ORDER BY activated_at, activation_id",
                (run_id,),
            ).fetchall()
        node_runs = []
        for node_row in node_rows:
            execution = StepResult.model_validate_json(node_row["execution_json"])
            node_runs.append(
                FlowNodeRun(
                    id=execution.execution_id,
                    node_id=execution.step_id,
                    iteration=execution.iteration,
                    attempt=execution.attempt,
                    status=execution.execution_status,
                    outcome=execution.outcome,
                    inputs=execution.inputs,
                    data=execution.data,
                    artifacts=execution.artifacts,
                    error=execution.error,
                    status_history=execution.status_history,
                    created_at=datetime.fromisoformat(node_row["created_at"]),
                    updated_at=datetime.fromisoformat(node_row["updated_at"]),
                )
            )
        return FlowRun(
            id=row["run_id"],
            flow_id=row["flow_id"],
            flow_version=row["flow_version"],
            flow_sha256=row["flow_sha256"],
            workflow_id=row["workflow_id"],
            status=row["status"],
            outcome=row["outcome"],
            current_node=row["current_node"],
            inputs=json.loads(row["inputs_json"]),
            node_runs=node_runs,
            activated_edges=[
                ActivatedFlowEdge(
                    activation_id=edge["activation_id"],
                    edge_id=edge["edge_id"],
                    source=edge["source"],
                    target=edge["target"],
                    outcome=edge["outcome"],
                    node_run_id=edge["node_run_id"],
                    activated_at=datetime.fromisoformat(edge["activated_at"]),
                )
                for edge in edge_rows
            ],
            error=StepError.model_validate_json(row["error_json"])
            if row["error_json"]
            else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def list(self, *, limit: int = 100) -> list[FlowRun]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id FROM flow_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [run for row in rows if (run := self.get(row["run_id"])) is not None]
