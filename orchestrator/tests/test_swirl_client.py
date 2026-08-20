from __future__ import annotations

import json
from pathlib import Path

import pytest

from automation_orchestrator.swirl_client import (
    SwirlClient,
    SwirlSearchError,
    normalize_swirl_response,
)


class FakeResponse:
    def __init__(self, payload: object):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int) -> bytes:
        return self.payload


def test_normalizes_grouped_results_and_deduplicates():
    response = normalize_swirl_response(
        "deploy",
        {
            "id": 42,
            "results": [
                {
                    "searchprovider": "Confluence",
                    "json_results": [
                        {
                            "title": "Runbook",
                            "body": "  Safe   excerpt ",
                            "url": "https://kb/runbook",
                            "score": 0,
                        },
                        {
                            "title": "Runbook",
                            "body": "duplicate",
                            "url": "https://kb/runbook",
                        },
                    ],
                }
            ],
        },
        max_results=10,
    )

    assert response.search_id == "42"
    assert len(response.results) == 1
    assert response.results[0].source == "Confluence"
    assert response.results[0].snippet == "Safe excerpt"
    assert response.results[0].score == 0


def test_normalizes_community_4_response_search_id():
    response = normalize_swirl_response(
        "deploy",
        {
            "info": {"search": {"id": 73, "query_string": "deploy"}},
            "results": [
                {
                    "title": "Deployment runbook",
                    "body": "Safe excerpt",
                    "url": "https://kb/runbook",
                    "searchprovider": "Confluence",
                    "swirl_score": 0.91,
                }
            ],
        },
        max_results=10,
    )

    assert response.search_id == "73"
    assert response.results[0].source == "Confluence"
    assert response.results[0].score == 0.91


def test_normalizes_bookstack_results_from_fixture():
    fixture = Path(__file__).parent / "fixtures" / "swirl-bookstack-response.json"
    response = normalize_swirl_response(
        "payment retry",
        json.loads(fixture.read_text(encoding="utf-8")),
        max_results=1,
    )

    assert response.search_id == "184"
    assert len(response.results) == 1
    assert response.results[0].source == "Local BookStack"
    assert response.results[0].title == "Payment retry runbook"
    assert response.results[0].snippet.startswith("Retry failed payments")
    assert response.results[0].url.endswith("/retry-runbook")


def test_bookstack_provider_template_is_scoped_and_contains_no_secret():
    template = Path(__file__).parents[2] / "swirl" / "searchproviders" / "bookstack.json"
    provider = json.loads(template.read_text(encoding="utf-8"))

    assert provider["connector"] == "RequestsGet"
    assert provider["default"] is False
    assert provider["tags"] == ["bookstack"]
    assert provider["response_mappings"] == "FOUND=total,RESULTS=data"
    assert "preview_html.content" in provider["result_mappings"]
    assert "NO_PAYLOAD" in provider["result_mappings"]
    assert provider["http_request_headers"]["Authorization"] == ("Token <token-id>:<token-secret>")


def test_client_sends_bounded_search_without_exposing_credentials():
    captured = {}

    def open_request(request, *, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return FakeResponse(
            {
                "structured": {
                    "search_id": 7,
                    "results": [
                        {"title": "Ticket", "snippet": "Details", "url": "https://jira/A-1"}
                    ],
                }
            }
        )

    client = SwirlClient(
        "http://swirl:8000",
        "agent",
        "secret",
        timeout_seconds=3,
        max_results=5,
        opener=open_request,
    )
    response = client.search("A-1 requirements", providers=["jira"], max_results=100)

    assert response.results[0].url == "https://jira/A-1"
    assert "result_count=5" in captured["url"]
    assert "providers=jira" in captured["url"]
    assert "secret" not in captured["url"]
    assert captured["authorization"].startswith("Basic ")
    assert captured["timeout"] == 3


def test_client_rejects_invalid_payload():
    client = SwirlClient(
        "http://swirl:8000",
        "agent",
        "secret",
        opener=lambda *_args, **_kwargs: FakeResponse([]),
    )

    with pytest.raises(SwirlSearchError, match="JSON object"):
        client.search("query")
