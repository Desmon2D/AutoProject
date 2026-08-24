from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .swirl_client import SwirlClient
from .workflow_search import search_workflow_context


@dataclass(frozen=True)
class RetrievalCase:
    name: str
    query: str
    expected_document_ids: frozenset[str]
    expected_matched_terms: frozenset[str] = frozenset()
    top_k: int = 3


def load_cases(path: Path) -> list[RetrievalCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError("retrieval evaluation file must be a non-empty JSON array")
    cases: list[RetrievalCase] = []
    for item in payload:
        if not isinstance(item, dict):
            raise TypeError("each retrieval evaluation case must be an object")
        name = item.get("name")
        query = item.get("query")
        expected = item.get("expected_document_ids")
        expected_terms = item.get("expected_matched_terms", [])
        top_k = item.get("top_k", 3)
        if (
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(query, str)
            or not query.strip()
            or not isinstance(expected, list)
            or not expected
            or not all(isinstance(value, str) and value for value in expected)
            or not isinstance(expected_terms, list)
            or not all(isinstance(value, str) and value for value in expected_terms)
            or not isinstance(top_k, int)
            or top_k < 1
            or top_k > 20
        ):
            raise ValueError(f"invalid retrieval evaluation case: {name!r}")
        cases.append(
            RetrievalCase(
                name=name.strip(),
                query=query.strip(),
                expected_document_ids=frozenset(expected),
                expected_matched_terms=frozenset(expected_terms),
                top_k=top_k,
            )
        )
    return cases


def evaluate_cases(client: SwirlClient, cases: list[RetrievalCase]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for case in cases:
        found = search_workflow_context(
            client,
            case.query,
            providers=["bookstack"],
            max_results=12,
            fallback_on_empty=True,
            max_fallback_queries=8,
            expand_query=True,
            rank_fusion_k=60,
            focused_query_weight=1.5,
            fetch_content=True,
            max_content_documents=8,
            max_content_characters=50_000,
            min_content_documents=1,
            max_context_characters=12_000,
            max_chunk_characters=1800,
            max_chunks_per_document=2,
            max_context_results=6,
            max_snippet_characters=500,
            min_chunk_relevance=1.0,
        )
        top_ids = [item.document_id for item in found[: case.top_k] if item.document_id]
        matched = next((value for value in top_ids if value in case.expected_document_ids), None)
        matched_terms = {
            term
            for item in found[: case.top_k]
            for excerpt in item.excerpts
            for term in excerpt.matched_terms
        }
        passed = matched is not None and case.expected_matched_terms <= matched_terms
        results.append(
            {
                "name": case.name,
                "passed": passed,
                "matched_document_id": matched,
                "top_document_ids": top_ids,
                "matched_terms": sorted(matched_terms),
                "missing_expected_terms": sorted(
                    case.expected_matched_terms - matched_terms
                ),
            }
        )
    passed = sum(bool(item["passed"]) for item in results)
    return {
        "passed": passed,
        "failed": len(results) - passed,
        "total": len(results),
        "cases": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate BookStack retrieval without an LLM")
    parser.add_argument("cases", type=Path, help="path to a retrieval evaluation JSON file")
    arguments = parser.parse_args()
    client = SwirlClient.from_environment()
    if client is None:
        parser.error("SWIRL connection variables are not configured")
    report = evaluate_cases(client, load_cases(arguments.cases))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
