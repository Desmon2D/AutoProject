from __future__ import annotations

import sqlite3

import pytest

from automation_orchestrator.audit_store import AuditStore


def test_audit_store_is_append_only_and_redacts_secrets(tmp_path):
    store = AuditStore(tmp_path / "audit.sqlite3")
    created = store.record(
        actor="local-client",
        action="workflow.trigger.received",
        resource_type="workflow",
        resource_id="wf-1",
        details={"token": "must-not-leak", "nested": {"password": "hidden"}},
    )

    events = store.list(resource_id="wf-1")

    assert events == [created]
    assert events[0].details == {
        "token": "<redacted>",
        "nested": {"password": "<redacted>"},
    }

    connection = sqlite3.connect(store.path)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE audit_events SET actor = 'other' WHERE id = ?", (created.id,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM audit_events WHERE id = ?", (created.id,))
    connection.close()


def test_audit_store_removes_legacy_role_column_without_losing_events(tmp_path):
    path = tmp_path / "audit.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at TEXT NOT NULL,
            actor TEXT NOT NULL,
            role TEXT NOT NULL,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id TEXT,
            outcome TEXT NOT NULL,
            request_id TEXT,
            source_ip TEXT,
            details_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO audit_events (
            occurred_at, actor, role, action, resource_type, outcome, details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-08-20T00:00:00+00:00",
            "local-client",
            "admin",
            "workflow.trigger.received",
            "workflow",
            "SUCCESS",
            "{}",
        ),
    )
    connection.commit()
    connection.close()

    store = AuditStore(path)

    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(audit_events)")}
    assert "role" not in columns
    assert store.list()[0].actor == "local-client"
