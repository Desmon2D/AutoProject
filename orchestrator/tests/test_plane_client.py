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
    assert result["source_links_updated"] == 3
    assert len(requests) == 7
    posted_links = [json.loads(request.data) for request in requests if "/links/" in request.full_url and request.method == "POST"]
    assert posted_links[0] == {
        "title": "Рабочий репозиторий: team/service",
        "url": "http://localhost:3000/team/service",
    }
    assert posted_links[1]["title"] == "Рабочая ветка: automation/wf-1"
    assert posted_links[2]["title"] == f"Коммит реализации: {'a' * 40}"


def test_ready_implementation_reuses_user_repository_link_by_url():
    requests = []
    get_count = 0

    def opener(request, **_kwargs):
        nonlocal get_count
        requests.append(request)
        if request.method == "GET":
            get_count += 1
            if get_count == 1:
                return Response(
                    json.dumps(
                        {
                            "results": [
                                {
                                    "id": "source-link",
                                    "title": "Исходный код",
                                    "url": "http://localhost:3000/team/service",
                                }
                            ]
                        }
                    ).encode()
                )
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

    posted_links = [
        json.loads(request.data)
        for request in requests
        if "/links/" in request.full_url and request.method == "POST"
    ]
    assert result["source_links_updated"] == 2
    assert len(posted_links) == 2
    assert all(link["url"] != "http://localhost:3000/team/service" for link in posted_links)
    assert not any(request.method == "PATCH" for request in requests)


@pytest.mark.parametrize(
    ("recommendation", "state_id"),
    [
        ("development_started", "state-development"),
        ("approved_for_testing", "state-testing"),
        ("cancelled", "state-cancelled"),
    ],
)
def test_corporate_lifecycle_recommendations_move_plane_state(recommendation, state_id):
    requests = []

    def opener(request, **_kwargs):
        requests.append(request)
        return Response(b"{}")

    client = PlaneClient(
        "http://plane",
        "plane_api_secret",
        "automation",
        {recommendation: state_id},
        opener=opener,
    )

    result = client.record_result(
        project_id="project-1",
        issue_id="issue-1",
        workflow_id="wf-1",
        recommendation=recommendation,
        summary="Lifecycle transition",
        details={},
    )

    assert result["state_updated"] is True
    assert [request.method for request in requests] == ["POST", "PATCH"]
    assert json.loads(requests[-1].data) == {"state": state_id}


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


def test_reads_repository_from_root_gitea_work_item_link():
    def opener(_request, **_kwargs):
        return Response(
            json.dumps(
                {
                    "results": [
                        {
                            "title": "Исходный код",
                            "url": "http://localhost:3000/team/payments-api",
                        },
                        {
                            "title": "Коммит",
                            "url": "http://localhost:3000/team/payments-api/commit/abc",
                        },
                        {
                            "title": "Внешняя документация",
                            "url": "https://example.com/team/other",
                        },
                    ]
                }
            ).encode()
        )

    client = PlaneClient(
        "http://plane",
        "plane_api_secret",
        "automation",
        {},
        gitea_public_base_url="http://localhost:3000",
        opener=opener,
    )

    assert client.get_repository_source(project_id="project-1", issue_id="issue-1") == {
        "full_name": "team/payments-api",
        "source_url": "http://localhost:3000/team/payments-api",
    }


def test_rejects_multiple_repository_links():
    def opener(_request, **_kwargs):
        return Response(
            json.dumps(
                {
                    "results": [
                        {"url": "http://localhost:3000/team/service"},
                        {"url": "http://localhost:3000/team/other"},
                    ]
                }
            ).encode()
        )

    client = PlaneClient(
        "http://plane",
        "plane_api_secret",
        "automation",
        {},
        gitea_public_base_url="http://localhost:3000",
        opener=opener,
    )

    with pytest.raises(PlaneClientError, match="multiple Gitea repository links"):
        client.get_repository_source(project_id="project-1", issue_id="issue-1")


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
