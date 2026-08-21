import io
import json

import pytest

from automation_orchestrator.plane_client import PlaneClient, PlaneClientError


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_records_comment_and_moves_accepted_issue():
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return Response(json.dumps({"id": "result"}).encode())

    client = PlaneClient(
        "http://plane",
        "plane_api_secret",
        "automation",
        {"accepted": "state-done"},
        opener=opener,
    )

    result = client.record_result(
        project_id="project-1",
        issue_id="issue-1",
        workflow_id="wf-1",
        recommendation="accepted",
        summary="Merged",
        details={"pull_request": {"index": 8}},
    )

    assert result == {
        "recommendation": "accepted",
        "comment_created": True,
        "state_updated": True,
        "state_id": "state-done",
        "source_links_updated": 0,
    }
    assert [request.method for request in requests] == ["POST", "PATCH"]
    assert requests[0].get_header("X-api-key") == "plane_api_secret"
    comment = json.loads(requests[0].data)
    assert comment["external_id"] == "wf-1:accepted"
    assert "Merged" in comment["comment_html"]
    assert json.loads(requests[1].data) == {"state": "state-done"}


def test_ready_implementation_adds_comment_without_changing_state():
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        if request.method == "GET":
            return Response(b'{"results": []}')
        return Response(b"{}")

    client = PlaneClient(
        "http://plane",
        "plane_api_secret",
        "automation",
        {},
        gitea_public_base_url="http://localhost:3000",
        opener=opener,
    )

    result = client.record_result(
        project_id="project-1",
        issue_id="issue-1",
        workflow_id="wf-1",
        recommendation="move_to_testing",
        summary="Ready",
        details={
            "implementation_change": {
                "repository": "team/service",
                "branch": "automation/wf-1",
                "commit": "a" * 40,
            }
        },
    )

    assert result["state_updated"] is False
    assert result["source_links_updated"] == 2
    assert len(requests) == 5
    posted_links = [json.loads(request.data) for request in requests if "/links/" in request.full_url and request.method == "POST"]
    assert posted_links[0]["title"] == "Рабочая ветка: automation/wf-1"
    assert posted_links[1]["title"] == f"Коммит реализации: {'a' * 40}"


def test_reads_exact_implementation_source_from_work_item_links():
    def opener(_request, **_kwargs):
        return Response(
            json.dumps(
                {
                    "results": [
                        {"title": "Рабочая ветка: feature/payment-rules"},
                        {"title": f"Коммит реализации: {'A' * 40}"},
                    ]
                }
            ).encode()
        )

    client = PlaneClient(
        "http://plane",
        "plane_api_secret",
        "automation",
        {},
        opener=opener,
    )

    assert client.get_implementation_source(
        project_id="project-1", issue_id="issue-1"
    ) == {
        "implementation_ref": "feature/payment-rules",
        "implementation_commit": "a" * 40,
    }


def test_rejects_terminal_result_without_configured_state():
    client = PlaneClient(
        "http://plane",
        "plane_api_secret",
        "automation",
        {},
        opener=lambda *_args, **_kwargs: Response(b"{}"),
    )

    with pytest.raises(PlaneClientError, match="state is not configured"):
        client.record_result(
            project_id="project-1",
            issue_id="issue-1",
            workflow_id="wf-1",
            recommendation="rejected",
            summary="Rejected",
            details={},
        )
