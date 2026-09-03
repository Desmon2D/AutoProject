from datetime import timedelta
from pathlib import Path

import pytest

from automation_orchestrator.job_store import IdempotencyConflict, JobStore
from automation_orchestrator.models import AgentRunRequest, AgentStep, StepResult


def request(prompt: str = "Do work") -> AgentRunRequest:
    return AgentRunRequest(
        execution_id="exec-1",
        workflow_id="wf-1",
        step=AgentStep(id="agent", prompt=prompt, model="test"),
    )


def test_job_store_caches_result_and_rejects_execution_id_collision(tmp_path: Path):
    store = JobStore(tmp_path)
    assert store.begin(request()) is None
    assert store.get_request("exec-1") == request()
    result = StepResult(
        step_id="agent",
        execution_id="exec-1",
        iteration=1,
        attempt=1,
        execution_status="COMPLETED",
        outcome="SUCCESS",
        data={"summary": "done"},
        artifacts=[],
    )
    store.save(result)

    assert store.begin(request()) == result
    with pytest.raises(IdempotencyConflict):
        store.begin(request("Different work"))


def test_job_store_registers_and_serves_only_safe_artifacts(tmp_path: Path):
    store = JobStore(tmp_path, artifact_ttl_seconds=60)
    store.begin(request())
    output = tmp_path / "exec-1" / "output"
    output.mkdir()
    (output / "report.md").write_text("result", encoding="utf-8")
    (output / "result.json").write_text("{}", encoding="utf-8")
    result = StepResult(
        step_id="agent",
        execution_id="exec-1",
        iteration=1,
        attempt=1,
        execution_status="COMPLETED",
        outcome="SUCCESS",
        data={"summary": "done"},
        artifacts=[],
    )

    store.save(result)

    records = store.artifacts.list("exec-1")
    assert [record.path for record in records] == ["report.md"]
    assert records[0].size_bytes == 6
    assert records[0].expires_at is not None
    assert store.artifact("exec-1", "report.md") == output / "report.md"
    assert store.artifact("exec-1", "../request.json") is None


def test_job_store_removes_only_expired_artifacts(tmp_path: Path):
    store = JobStore(tmp_path, artifact_ttl_seconds=60)
    store.begin(request())
    output = tmp_path / "exec-1" / "output"
    output.mkdir()
    artifact = output / "nested" / "report.md"
    artifact.parent.mkdir()
    artifact.write_text("result", encoding="utf-8")
    (output / "result.json").write_text("{}", encoding="utf-8")
    result = StepResult(
        step_id="agent",
        execution_id="exec-1",
        iteration=1,
        attempt=1,
        execution_status="COMPLETED",
        outcome="SUCCESS",
        data={"summary": "done"},
        artifacts=[],
    )
    store.save(result)
    record = store.artifacts.get("exec-1", "nested/report.md")

    before = store.cleanup_expired(now=record.expires_at - timedelta(seconds=1))
    after = store.cleanup_expired(now=record.expires_at + timedelta(seconds=1))

    assert before.examined == 0
    assert after.examined == 1
    assert after.removed_files == 1
    assert after.removed_records == 1
    assert after.failed == []
    assert not artifact.exists()
    assert (output / "result.json").is_file()
    assert (tmp_path / "exec-1" / "request.json").is_file()
    assert store.artifacts.list("exec-1") == []
