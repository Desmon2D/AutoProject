from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

TOKEN_PATTERN = re.compile(r"[\w-]+", re.UNICODE)


def load_documents(root: Path) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    if not root.is_dir():
        return documents
    for path in sorted(root.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload.get("documents", []) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise TypeError(f"{path.name}: documents must be an array")
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            url = str(item.get("url", "")).strip()
            if not title or not url:
                continue
            documents.append(
                {
                    "title": title[:1000],
                    "body": str(item.get("body", "")).strip()[:4000],
                    "url": url[:4000],
                    "source": str(item.get("source", "Local BookStack")).strip()[:300],
                    "date_published": str(item.get("date_published", "")).strip()[:100],
                }
            )
    return documents


def search_documents(
    documents: list[dict[str, Any]],
    query: str,
    *,
    providers: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    tokens = {token.casefold() for token in TOKEN_PATTERN.findall(query) if len(token) > 1}
    provider_terms = {provider.casefold().strip() for provider in providers if provider.strip()}
    ranked: list[tuple[float, dict[str, Any]]] = []
    for document in documents:
        source = document["source"].casefold()
        if provider_terms and not any(term == source or term in source for term in provider_terms):
            continue
        haystack = f"{document['title']} {document['body']}".casefold()
        matches = sum(1 for token in tokens if token in haystack)
        if tokens and matches == 0:
            continue
        score = matches / max(len(tokens), 1)
        document_id = str(document.get("document_id", "")).strip()
        if not document_id:
            document_id = hashlib.sha256(document["url"].encode("utf-8")).hexdigest()[:16]
        ranked.append((score, {**document, "document_id": document_id}))
    ranked.sort(key=lambda item: (-item[0], item[1]["title"].casefold()))
    return [{**document, "swirl_score": round(score, 4)} for score, document in ranked[:limit]]


def find_document(documents: list[dict[str, Any]], upstream_url: str) -> dict[str, Any] | None:
    path = urlparse(upstream_url).path.rstrip("/")
    document_id = unquote(path.rsplit("/", 1)[-1]) if path else ""
    if not document_id:
        return None
    for document in documents:
        candidate = str(document.get("document_id", "")).strip()
        if not candidate:
            candidate = hashlib.sha256(document["url"].encode("utf-8")).hexdigest()[:16]
        if hmac.compare_digest(candidate, document_id):
            return document
    return None


def build_response(
    documents: list[dict[str, Any]],
    query: str,
    *,
    providers: list[str],
    limit: int,
) -> dict[str, Any]:
    matches = search_documents(documents, query, providers=providers, limit=limit)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in matches:
        source = item.pop("source")
        grouped[source].append(item)
    search_id = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    return {
        "info": {"search": {"id": search_id, "query_string": query}},
        "results": [
            {
                "searchprovider": source,
                "retrieved": len(items),
                "found": len(items),
                "json_results": items,
            }
            for source, items in grouped.items()
        ],
    }


class SwirlLiteHandler(BaseHTTPRequestHandler):
    server_version = "AutomationSwirlLite/1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, {"status": "ok", "documents": len(self.server.documents)})
            return
        route = parsed.path.rstrip("/")
        if route not in {"/api/swirl/search", "/api/swirl/fetch-document"}:
            self._json(404, {"detail": "not found"})
            return
        if not self._authorized():
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="swirl-lite"')
            self.end_headers()
            return
        parameters = parse_qs(parsed.query)
        if route == "/api/swirl/fetch-document":
            upstream_url = parameters.get("url", [""])[0].strip()
            document = find_document(self.server.documents, upstream_url)
            if document is None:
                self._json(404, {"detail": "document not found"})
                return
            self._json(200, {"markdown": document["body"]})
            return
        query = parameters.get("qs", [""])[0].strip()
        if not query or len(query) > 2000:
            self._json(400, {"detail": "qs must contain 1..2000 characters"})
            return
        try:
            limit = max(1, min(int(parameters.get("result_count", ["10"])[0]), 50))
        except ValueError:
            self._json(400, {"detail": "result_count must be an integer"})
            return
        providers = [
            value.strip()
            for value in parameters.get("providers", [""])[0].split(",")
            if value.strip()
        ]
        self._json(
            200,
            build_response(
                self.server.documents,
                query,
                providers=providers,
                limit=limit,
            ),
        )

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _authorized(self) -> bool:
        expected = base64.b64encode(
            f"{self.server.username}:{self.server.password}".encode()
        ).decode("ascii")
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, f"Basic {expected}")

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class SwirlLiteServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        documents: list[dict[str, Any]],
        username: str,
        password: str,
    ):
        super().__init__(address, SwirlLiteHandler)
        self.documents = documents
        self.username = username
        self.password = password


def main() -> None:
    username = os.environ.get("SWIRL_LITE_USERNAME", "local").strip()
    password = os.environ.get("SWIRL_LITE_PASSWORD", "local-development")
    if not username or not password:
        raise SystemExit("SWIRL lite credentials must not be empty")
    root = Path(os.environ.get("SWIRL_LITE_DATA_ROOT", "/data"))
    documents = load_documents(root)
    server = SwirlLiteServer(("0.0.0.0", 8000), documents, username, password)
    server.serve_forever()


if __name__ == "__main__":
    main()
