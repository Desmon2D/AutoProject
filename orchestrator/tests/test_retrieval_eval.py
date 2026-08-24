import json

import pytest

from automation_orchestrator.models import SwirlSearchResponse, SwirlSearchResult
from automation_orchestrator.retrieval_eval import evaluate_cases, load_cases


class FakeSwirlClient:
    def search(self, query, *, providers, max_results):
        document_id = "17" if "конфиденциаль" in query.casefold() else "14"
        return SwirlSearchResponse(
            query=query,
            results=[
                SwirlSearchResult(
                    title=f"Document {document_id}",
                    url=f"http://bookstack/pages/{document_id}",
                    source="Local BookStack",
                    document_id=document_id,
                )
            ],
        )

    def fetch_document(self, result, *, max_characters):
        return result.model_copy(
            update={
                "content": "# Security\n\nМеры конфиденциальности защищают информацию.",
                "content_fetched": True,
                "content_format": "markdown",
            }
        )


def test_load_and_evaluate_retrieval_cases(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "security",
                    "query": "меры конфиденциальности",
                    "expected_document_ids": ["17"],
                    "top_k": 1,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = evaluate_cases(FakeSwirlClient(), load_cases(path))

    assert report["passed"] == 1
    assert report["failed"] == 0
    assert report["cases"][0]["top_document_ids"] == ["17"]
    assert report["cases"][0]["matched_terms"] == ["конфиденциальности"]


def test_rejects_empty_retrieval_eval(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="non-empty JSON array"):
        load_cases(path)
