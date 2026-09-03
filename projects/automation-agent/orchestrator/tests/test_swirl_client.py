from __future__ import annotations

import json
from pathlib import Path

import pytest

from automation_orchestrator.models import SwirlSearchResult
from automation_orchestrator.swirl_client import (
    SwirlClient,
    SwirlContentRoute,
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
                            "title": "<em>Runbook</em>",
                            "body": "  Safe strong <em>excerpt</em> strong &amp; guidance ",
                            "url": "https://kb/runbook",
                            "score": 0,
                        },
                        {
                            "title": "<em>Runbook</em>",
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
    assert response.results[0].title == "Runbook"
    assert response.results[0].snippet == "Safe excerpt & guidance"
    assert response.results[0].document_id is None
    assert response.results[0].score == 0


def test_normalizes_document_identifier_from_bounded_payload():
    response = normalize_swirl_response(
        "security",
        {
            "results": [
                {
                    "title": "Security requirements",
                    "body": "Preview",
                    "url": "http://bookstack/books/analytics/page/security",
                    "searchprovider": "Local BookStack",
                    "payload": {"id": 17, "type": "page"},
                }
            ]
        },
        max_results=10,
    )

    assert response.results[0].document_id == "17"


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
    template = (
        Path(__file__).parents[2]
        / "infra"
        / "swirl"
        / "searchproviders"
        / "bookstack.json"
    )
    provider = json.loads(template.read_text(encoding="utf-8"))

    assert provider["connector"] == "RequestsGet"
    assert provider["default"] is False
    assert provider["tags"] == ["bookstack"]
    assert provider["response_mappings"] == "FOUND=total,RESULTS=data"
    assert "preview_html.content" in provider["result_mappings"]
    assert "NO_PAYLOAD" in provider["result_mappings"]
    assert provider["http_request_headers"]["Authorization"] == ("Token <token-id>:<token-secret>")
    fetch = provider["page_fetch_config_json"]
    assert fetch["automation_content"] == {
        "url_template": "http://bookstack/api/pages/{id}",
        "content_path": "markdown",
        "format": "markdown",
    }
    assert fetch["headers"]["Authorization"] == "Token <token-id>:<token-secret>"


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


def test_client_loads_only_sanitized_content_routes_from_environment(monkeypatch):
    monkeypatch.setenv("SWIRL_BASE_URL", "http://swirl:8000")
    monkeypatch.setenv("SWIRL_USERNAME", "agent")
    monkeypatch.setenv("SWIRL_PASSWORD", "secret")
    monkeypatch.setenv("SWIRL_CONTENT_ALLOWED_ORIGINS", "http://bookstack")
    monkeypatch.setenv(
        "SWIRL_CONTENT_ROUTES_JSON",
        json.dumps(
            [
                {
                    "source": "Local BookStack",
                    "provider_id": 47,
                    "url_template": "http://bookstack/api/pages/{id}",
                    "content_path": "markdown",
                    "format": "markdown",
                }
            ]
        ),
    )

    client = SwirlClient.from_environment()

    assert client is not None
    assert client.content_routes["local bookstack"].provider_id == 47
    assert not hasattr(client.content_routes["local bookstack"], "headers")


def test_client_fetches_full_document_through_swirl_with_allowed_origin():
    captured = []

    def open_request(request, *, timeout):
        captured.append((request.full_url, timeout))
        assert request.full_url.startswith(
            "http://swirl:8000/api/swirl/fetch-document/?"
        )
        return FakeResponse({"markdown": "# Full source\n\nDocument body."})

    client = SwirlClient(
        "http://swirl:8000",
        "agent",
        "secret",
        allowed_content_origins=["http://bookstack"],
        content_routes=[
            SwirlContentRoute(
                provider_id=47,
                source="Local BookStack",
                url_template="http://bookstack/api/pages/{id}",
                content_path="markdown",
                content_format="markdown",
            )
        ],
        opener=open_request,
    )
    result = client.fetch_document(
        SwirlSearchResult(
            title="Security",
            snippet="Preview",
            url="http://bookstack/books/security",
            source="Local BookStack",
            document_id="17",
        ),
        max_characters=1000,
    )

    assert result.content == "# Full source\n\nDocument body."
    assert result.content_fetched is True
    assert result.content_format == "markdown"
    assert result.content_truncated is False
    assert len(captured) == 1
    assert "provider_id=47" in captured[0][0]
    assert "url=http%3A%2F%2Fbookstack%2Fapi%2Fpages%2F17" in captured[0][0]


def test_client_rejects_full_document_origin_outside_allowlist():
    calls = []

    def open_request(request, *, timeout):
        calls.append(request.full_url)
        return FakeResponse({"content": "must not be reached"})

    client = SwirlClient(
        "http://swirl:8000",
        "agent",
        "secret",
        allowed_content_origins=["http://bookstack"],
        content_routes=[
            SwirlContentRoute(
                provider_id=9,
                source="Unsafe",
                url_template="http://metadata.internal/{id}",
                content_path="content",
                content_format="text",
            )
        ],
        opener=open_request,
    )

    with pytest.raises(SwirlSearchError, match="origin is not allowed"):
        client.fetch_document(
            SwirlSearchResult(
                title="Unsafe",
                url="http://example/unsafe",
                source="Unsafe",
                document_id="1",
            )
        )
    assert calls == []
