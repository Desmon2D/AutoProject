from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .flow_validation import published_snapshot
from .models import FlowDefinition, FlowVersion


class FlowStoreError(RuntimeError):
    pass


class FlowAlreadyExists(FlowStoreError):
    pass


class FlowNotFound(FlowStoreError):
    pass


class FlowRevisionConflict(FlowStoreError):
    pass


class FlowStore:
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
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flow_drafts (
                    flow_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    definition_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS flow_versions (
                    flow_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK (version >= 1),
                    sha256 TEXT NOT NULL UNIQUE,
                    definition_json TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    PRIMARY KEY (flow_id, version)
                );
                CREATE INDEX IF NOT EXISTS flow_versions_flow_idx
                    ON flow_versions (flow_id, version DESC);
                INSERT INTO schema_migrations (component, version)
                VALUES ('flow_store', 1)
                ON CONFLICT(component) DO UPDATE SET version = MAX(version, 1);
                """
            )
            connection.execute("PRAGMA optimize")

    def list(self) -> list[FlowDefinition]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT definition_json FROM flow_drafts ORDER BY updated_at DESC"
            ).fetchall()
        return [FlowDefinition.model_validate_json(row["definition_json"]) for row in rows]

    def get(self, flow_id: str) -> FlowDefinition | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT definition_json FROM flow_drafts WHERE flow_id = ?",
                (flow_id,),
            ).fetchone()
        if row is None:
            return None
        return FlowDefinition.model_validate_json(row["definition_json"])

    def create(self, flow: FlowDefinition) -> FlowDefinition:
        now = datetime.now(UTC).isoformat()
        draft = flow.model_copy(
            update={"revision": 1, "version": "draft", "builtin": False, "read_only": False, "status": "draft"}
        )
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO flow_drafts (
                        flow_id, revision, definition_json, created_at, updated_at
                    ) VALUES (?, 1, ?, ?, ?)
                    """,
                    (draft.id, draft.model_dump_json(), now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise FlowAlreadyExists(f"flow already exists: {flow.id}") from exc
        return draft

    def save(self, flow: FlowDefinition, *, expected_revision: int) -> FlowDefinition:
        next_revision = expected_revision + 1
        updated = flow.model_copy(
            update={
                "revision": next_revision,
                "version": "draft",
                "builtin": False,
                "read_only": False,
                "status": "draft",
            }
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE flow_drafts
                SET revision = ?, definition_json = ?, updated_at = ?
                WHERE flow_id = ? AND revision = ?
                """,
                (
                    next_revision,
                    updated.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                    flow.id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM flow_drafts WHERE flow_id = ?", (flow.id,)
                ).fetchone()
                connection.rollback()
                if exists is None:
                    raise FlowNotFound(f"unknown draft: {flow.id}")
                raise FlowRevisionConflict(
                    f"flow revision changed; expected {expected_revision}"
                )
            connection.commit()
        return updated

    def delete(self, flow_id: str, *, expected_revision: int) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM flow_drafts WHERE flow_id = ? AND revision = ?",
                (flow_id, expected_revision),
            )
            if cursor.rowcount != 1:
                exists = connection.execute(
                    "SELECT 1 FROM flow_drafts WHERE flow_id = ?", (flow_id,)
                ).fetchone()
                connection.rollback()
                if exists is None:
                    raise FlowNotFound(f"unknown draft: {flow_id}")
                raise FlowRevisionConflict(
                    f"flow revision changed; expected {expected_revision}"
                )
            connection.commit()

    def publish(self, flow_id: str, *, expected_revision: int) -> FlowVersion:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT revision, definition_json FROM flow_drafts WHERE flow_id = ?",
                (flow_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise FlowNotFound(f"unknown draft: {flow_id}")
            if row["revision"] != expected_revision:
                connection.rollback()
                raise FlowRevisionConflict(
                    f"flow revision changed; expected {expected_revision}"
                )
            next_version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM flow_versions WHERE flow_id = ?",
                (flow_id,),
            ).fetchone()[0]
            draft = FlowDefinition.model_validate_json(row["definition_json"])
            definition, digest = published_snapshot(draft, next_version)
            published_at = datetime.now(UTC)
            connection.execute(
                """
                INSERT INTO flow_versions (
                    flow_id, version, sha256, definition_json, published_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    flow_id,
                    next_version,
                    digest,
                    definition.model_dump_json(),
                    published_at.isoformat(),
                ),
            )
            connection.commit()
        return FlowVersion(
            flow_id=flow_id,
            version=next_version,
            sha256=digest,
            definition=definition,
            published_at=published_at,
        )

    def versions(self, flow_id: str) -> list[FlowVersion]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT version, sha256, definition_json, published_at
                FROM flow_versions WHERE flow_id = ? ORDER BY version DESC
                """,
                (flow_id,),
            ).fetchall()
        return [
            FlowVersion(
                flow_id=flow_id,
                version=row["version"],
                sha256=row["sha256"],
                definition=FlowDefinition.model_validate_json(row["definition_json"]),
                published_at=datetime.fromisoformat(row["published_at"]),
            )
            for row in rows
        ]

    def matching_versions(self, source: str, event: str) -> list[FlowVersion]:
        """Return the latest published version of every enabled matching flow."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT versions.flow_id, versions.version, versions.sha256,
                       versions.definition_json, versions.published_at
                FROM flow_versions AS versions
                INNER JOIN (
                    SELECT flow_id, MAX(version) AS version
                    FROM flow_versions GROUP BY flow_id
                ) AS latest
                    ON latest.flow_id = versions.flow_id
                   AND latest.version = versions.version
                ORDER BY versions.flow_id
                """
            ).fetchall()
        matches: list[FlowVersion] = []
        for row in rows:
            definition = FlowDefinition.model_validate_json(row["definition_json"])
            trigger = next((node for node in definition.nodes if node.type == "trigger"), None)
            if (
                not definition.enabled
                or trigger is None
                or trigger.config.get("source") != source
                or trigger.config.get("event") != event
            ):
                continue
            matches.append(
                FlowVersion(
                    flow_id=row["flow_id"],
                    version=row["version"],
                    sha256=row["sha256"],
                    definition=definition,
                    published_at=datetime.fromisoformat(row["published_at"]),
                )
            )
        return matches

    def get_version(self, flow_id: str, version: int | None = None) -> FlowVersion | None:
        query = """
            SELECT version, sha256, definition_json, published_at
            FROM flow_versions WHERE flow_id = ?
        """
        parameters: tuple = (flow_id,)
        if version is None:
            query += " ORDER BY version DESC LIMIT 1"
        else:
            query += " AND version = ?"
            parameters = (flow_id, version)
        with self._connect() as connection:
            row = connection.execute(query, parameters).fetchone()
        if row is None:
            return None
        return FlowVersion(
            flow_id=flow_id,
            version=row["version"],
            sha256=row["sha256"],
            definition=FlowDefinition.model_validate_json(row["definition_json"]),
            published_at=datetime.fromisoformat(row["published_at"]),
        )
