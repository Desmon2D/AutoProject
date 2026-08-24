from automation_orchestrator.models import SwirlSearchResponse, SwirlSearchResult
from automation_orchestrator.workflow_search import (
    focused_search_terms,
    markdown_content_chunks,
    pack_document_context,
    primary_subject_terms,
    search_workflow_context,
    summarize_retrieval,
)


def result(title: str, content: str, document_id: str) -> SwirlSearchResult:
    return SwirlSearchResult(
        title=title,
        snippet="Search preview",
        url=f"http://bookstack/pages/{document_id}",
        source="Local BookStack",
        document_id=document_id,
        content=content,
        content_fetched=True,
        content_format="markdown",
    )


def test_markdown_chunks_preserve_headings_and_bound_large_sections():
    chunks = markdown_content_chunks(
        "# Overview\n\nShort introduction.\n\n"
        "## Detailed controls\n\n"
        + "encryption policy " * 80,
        max_characters=300,
    )

    assert chunks[0] == ("Overview", "Short introduction.")
    assert {heading for heading, _text in chunks[1:]} == {"Detailed controls"}
    assert all(len(text) <= 300 for _heading, text in chunks)


def test_context_packing_ranks_sections_preserves_sources_and_removes_full_text():
    packed = pack_document_context(
        "confidentiality encryption audit",
        [
            result(
                "Security",
                "# Introduction\n\nGeneral background.\n\n"
                "## Confidentiality\n\nEncryption protects confidential records.\n\n"
                "## Availability\n\nBackups are retained.",
                "17",
            ),
            result(
                "Audit",
                "# Audit trail\n\nAudit events record access to confidential records.\n\n"
                "## Operations\n\nRoutine maintenance guidance.",
                "18",
            ),
        ],
        max_context_characters=700,
        max_chunk_characters=300,
        max_chunks_per_document=2,
        max_context_results=2,
        max_snippet_characters=100,
        min_chunk_relevance=1.0,
    )

    assert all(item.content is None for item in packed)
    assert all(item.content_fetched for item in packed)
    assert all(item.excerpts for item in packed)
    assert packed[0].excerpts[0].heading == "Confidentiality"
    assert packed[1].excerpts[0].heading == "Audit trail"
    assert all(item.content_truncated for item in packed)
    packed_cost = sum(
        len(excerpt.text) + len(excerpt.heading or "") + 80
        for item in packed
        for excerpt in item.excerpts
    )
    assert packed_cost <= 700


def test_context_packing_limits_chunks_per_document():
    packed = pack_document_context(
        "security",
        [
            result(
                "Controls",
                "# First security section\n\nSecurity one.\n\n"
                "## Second security section\n\nSecurity two.\n\n"
                "## Third security section\n\nSecurity three.",
                "19",
            )
        ],
        max_context_characters=2000,
        max_chunk_characters=500,
        max_chunks_per_document=2,
        max_context_results=1,
        max_snippet_characters=100,
        min_chunk_relevance=1.0,
    )

    assert len(packed[0].excerpts) == 2


def test_context_packing_matches_russian_word_forms_and_limits_metadata():
    policy = result(
        "Политика",
        "# Введение\n\nОбщие сведения.\n\n"
        "## Конфиденциальность\n\nДоступ предоставляется по ролям.",
        "20",
    ).model_copy(update={"snippet": "S" * 150})
    packed = pack_document_context(
        "Опиши меры конфиденциальности",
        [
            policy,
            result("Журналирование", "# Аудит\n\nСобытия сохраняются.", "21"),
        ],
        max_context_characters=1000,
        max_chunk_characters=500,
        max_chunks_per_document=1,
        max_context_results=1,
        max_snippet_characters=100,
        min_chunk_relevance=1.0,
    )

    assert len(packed) == 1
    assert len(packed[0].snippet) == 100
    assert packed[0].excerpts[0].heading == "Конфиденциальность"


def test_expanded_search_promotes_focused_topic_over_generic_full_query_results():
    class SearchClient:
        def __init__(self):
            self.calls = []

        def search(self, query, *, providers, max_results):
            self.calls.append(query)
            if query == "конфиденциальность":
                results = [
                    SwirlSearchResult(
                        title="Безопасность",
                        url="http://bookstack/security",
                        source="Local BookStack",
                        document_id="17",
                        score=700,
                    )
                ]
            elif query.startswith("Изучи"):
                results = [
                    SwirlSearchResult(
                        title="Процесс аналитики",
                        url="http://bookstack/process",
                        source="Local BookStack",
                        document_id="14",
                        score=900,
                    )
                ]
            else:
                results = []
            return SwirlSearchResponse(query=query, results=results)

    query = (
        "Изучи документацию и составь требования к обеспечению "
        "конфиденциальности информации. Укажи риски и открытые вопросы."
    )
    client = SearchClient()

    results = search_workflow_context(
        client,
        query,
        providers=["bookstack"],
        max_results=8,
        fallback_on_empty=True,
        max_fallback_queries=8,
        expand_query=True,
        focused_query_weight=1.5,
    )

    assert "конфиденциальность" in focused_search_terms(query, max_queries=8)
    assert results[0].title == "Безопасность"
    assert results[0].matched_queries == ["конфиденциальность"]
    assert client.calls[0] == query
    assert "конфиденциальность" in client.calls


def test_context_packing_omits_fetched_documents_below_relevance_threshold():
    packed = pack_document_context(
        "encryption confidentiality",
        [
            result("Security", "# Encryption\n\nConfidential records are encrypted.", "22"),
            result("Catering", "# Lunch\n\nThe menu changes every day.", "23"),
        ],
        max_context_characters=2000,
        max_chunk_characters=500,
        max_chunks_per_document=2,
        max_context_results=2,
        max_snippet_characters=100,
        min_chunk_relevance=1.0,
    )

    assert [item.title for item in packed] == ["Security"]


def test_long_request_keeps_primary_topic_and_ignores_negative_directive():
    query = (
        "Изучи документацию в BookStack и составь требования по обеспечению "
        "конфиденциальности информации. Укажи документированные меры, роли и права "
        "доступа, функциональные и нефункциональные требования, критерии приёмки, "
        "риски и открытые вопросы. Для каждого существенного утверждения приведи "
        "источник. Не описывай устройство сценария аналитики, если оно не относится "
        "к предмету запроса."
    )

    terms = focused_search_terms(query, max_queries=8)

    assert primary_subject_terms(query) == ["конфиденциальности"]
    assert terms[:2] == ["конфиденциальности", "конфиденциальность"]
    assert "аналитики" not in terms
    assert "устройство" not in terms


def test_context_packing_promotes_primary_topic_outside_initial_result_window():
    query = (
        "Составь требования по обеспечению конфиденциальности информации. "
        "Укажи роли, доступ, критерии и риски."
    )
    generic = [
        result(
            f"Generic {index}",
            f"# Roles {index}\n\nРоли, доступ, критерии и риски процесса.",
            str(index),
        )
        for index in range(1, 7)
    ]
    security = result(
        "Security",
        "# Конфиденциальность\n\nТокены маскируются, обращения к данным записываются в аудит.",
        "17",
    )

    packed = pack_document_context(
        query,
        [*generic, security],
        max_context_characters=3000,
        max_chunk_characters=500,
        max_chunks_per_document=1,
        max_context_results=3,
        max_snippet_characters=100,
        min_chunk_relevance=1.0,
    )
    summary = summarize_retrieval(query, packed)

    assert packed[0].document_id == "17"
    assert summary["primary_topic_terms"] == ["конфиденциальности"]
    assert summary["primary_topic_coverage"] == 1.0
    assert summary["topic_coverage_sufficient"] is True
