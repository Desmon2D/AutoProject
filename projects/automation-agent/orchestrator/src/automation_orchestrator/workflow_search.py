from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from .models import SwirlContentExcerpt, SwirlSearchResponse, SwirlSearchResult
from .swirl_client import SwirlSearchError

_TOKEN = re.compile(r"[^\W_]{4,}", flags=re.UNICODE)
_RANK_TOKEN = re.compile(r"[^\W_]{3,}", flags=re.UNICODE)
_MARKDOWN_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_NEGATIVE_DIRECTIVE = re.compile(
    r"\b(?:не\s+(?:описывай|описывать|включай|включать|рассматривай|рассматривать)"
    r"|do\s+not\s+(?:describe|include|use|consider))\b",
    flags=re.IGNORECASE,
)
_STOP_WORDS = frozenset(
    {
        "bookstack",
        "анализ",
        "аналитика",
        "аналитике",
        "аналитику",
        "документ",
        "документа",
        "документации",
        "документацию",
        "документы",
        "документированные",
        "если",
        "изучи",
        "изучить",
        "какая",
        "какие",
        "какой",
        "которые",
        "каждого",
        "меры",
        "мной",
        "мне",
        "напиши",
        "подготовь",
        "предпринимаются",
        "составить",
        "составь",
        "сформулируй",
        "требования",
        "требований",
        "analysis",
        "analyze",
        "create",
        "documentation",
        "document",
        "information",
        "prepare",
        "requirements",
        "study",
        "write",
        "аналитическая",
        "аналитического",
        "аналитической",
        "аналитический",
        "вопросы",
        "для",
        "используй",
        "источники",
        "информация",
        "информации",
        "найденные",
        "нужно",
        "обеспечению",
        "оно",
        "описывай",
        "опиши",
        "открытые",
        "приёмки",
        "приемки",
        "приведи",
        "предмету",
        "подготовки",
        "применяются",
        "сценария",
        "система",
        "системе",
        "системы",
        "сохранение",
        "сохранения",
        "существенного",
        "только",
        "устройство",
        "укажи",
        "утверждения",
        "источник",
        "относится",
        "пожалуйста",
        "запроса",
    }
)


class SwirlSearcher(Protocol):
    def search(
        self,
        query: str,
        *,
        providers: list[str] | None = None,
        max_results: int = 10,
    ) -> SwirlSearchResponse: ...

    def fetch_document(
        self,
        result: SwirlSearchResult,
        *,
        max_characters: int = 50_000,
    ) -> SwirlSearchResult: ...


@dataclass(frozen=True)
class _ContentChunk:
    document_index: int
    chunk_index: int
    heading: str | None
    text: str
    relevance_score: float
    primary_relevance_score: float
    matched_terms: tuple[str, ...]

    @property
    def context_cost(self) -> int:
        return len(self.text) + len(self.heading or "") + 80


@dataclass
class _FusedResult:
    result: SwirlSearchResult
    first_order: int
    retrieval_score: float = 0
    matched_queries: list[str] = field(default_factory=list)


def _relevant_query_segments(query: str) -> list[str]:
    segments = [item.strip() for item in re.split(r"(?<=[.!?])|[\r\n]+", query)]
    relevant = [
        item
        for item in segments
        if item and not _NEGATIVE_DIRECTIVE.search(item)
    ]
    return relevant or [query]


def _terms_from_text(text: str, *, minimum_length: int = 3) -> list[str]:
    pattern = _TOKEN if minimum_length >= 4 else _RANK_TOKEN
    terms: list[str] = []
    seen: set[str] = set()
    for token in pattern.findall(text.casefold()):
        if token in _STOP_WORDS or token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def primary_subject_terms(query: str) -> list[str]:
    """Return topic terms from the first meaningful positive request sentence."""
    for segment in _relevant_query_segments(query):
        terms = _terms_from_text(segment)
        if terms:
            return terms[:4]
    return []


def _term_variants(term: str) -> list[str]:
    variants = [term]
    if term.endswith("ости"):
        variants.append(f"{term[:-1]}ь")
    elif term.endswith("ции"):
        variants.append(f"{term[:-1]}я")
    elif term.endswith(("ения", "ания")):
        variants.append(f"{term[:-1]}е")
    return variants


def focused_search_terms(query: str, *, max_queries: int) -> list[str]:
    """Return bounded topic terms with the primary subject first."""
    terms: list[str] = []
    seen: set[str] = set()
    primary = primary_subject_terms(query)
    all_terms = _query_terms(query)
    ordered = [*primary, *(term for term in all_terms if term not in primary)]
    for term in ordered:
        variants = _term_variants(term) if term in primary else [term]
        for variant in variants:
            key = variant.casefold()
            if key in seen:
                continue
            seen.add(key)
            terms.append(variant)
            if len(terms) >= max_queries:
                return terms
    return terms


def _result_key(result: SwirlSearchResult) -> tuple[str, str]:
    identity = result.document_id or result.url
    return (result.source.casefold(), identity.casefold())


def fuse_search_results(
    searches: list[tuple[str, list[SwirlSearchResult], float]],
    *,
    max_results: int,
    rank_fusion_k: int,
) -> list[SwirlSearchResult]:
    """Merge query result lists with weighted reciprocal rank fusion."""
    fused: dict[tuple[str, str], _FusedResult] = {}
    order = 0
    for query_label, results, weight in searches:
        for rank, result in enumerate(results, start=1):
            key = _result_key(result)
            record = fused.get(key)
            if record is None:
                record = _FusedResult(result=result, first_order=order)
                fused[key] = record
                order += 1
            elif (result.score or 0) > (record.result.score or 0):
                record.result = result
            record.retrieval_score += weight / (rank_fusion_k + rank)
            if query_label not in record.matched_queries:
                record.matched_queries.append(query_label)

    ranked = sorted(
        fused.values(),
        key=lambda item: (
            -item.retrieval_score,
            -len(item.matched_queries),
            -(item.result.score or 0),
            item.first_order,
        ),
    )
    return [
        item.result.model_copy(
            update={
                "retrieval_score": round(item.retrieval_score, 8),
                "matched_queries": item.matched_queries[:20],
            }
        )
        for item in ranked[:max_results]
    ]


def _split_long_text(text: str, *, max_characters: int) -> list[str]:
    parts: list[str] = []
    remaining = text.strip()
    while len(remaining) > max_characters:
        boundary = remaining.rfind(" ", max_characters // 2, max_characters + 1)
        if boundary < 1:
            boundary = max_characters
        parts.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].strip()
    if remaining:
        parts.append(remaining)
    return parts


def _section_chunks(
    heading: str | None,
    lines: list[str],
    *,
    max_characters: int,
) -> list[tuple[str | None, str]]:
    body = "\n".join(lines).strip()
    if not body:
        return []
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", body) if item.strip()]
    chunks: list[tuple[str | None, str]] = []
    current: list[str] = []
    current_size = 0
    for paragraph in paragraphs:
        parts = _split_long_text(paragraph, max_characters=max_characters)
        for part in parts:
            separator = 2 if current else 0
            if current and current_size + separator + len(part) > max_characters:
                chunks.append((heading, "\n\n".join(current)))
                current = []
                current_size = 0
                separator = 0
            current.append(part)
            current_size += separator + len(part)
    if current:
        chunks.append((heading, "\n\n".join(current)))
    return chunks


def markdown_content_chunks(
    content: str,
    *,
    max_characters: int,
) -> list[tuple[str | None, str]]:
    """Split Markdown on headings, then bound large sections on paragraph boundaries."""
    chunks: list[tuple[str | None, str]] = []
    heading: str | None = None
    section_lines: list[str] = []
    for line in content.splitlines():
        match = _MARKDOWN_HEADING.match(line)
        if match:
            chunks.extend(
                _section_chunks(
                    heading,
                    section_lines,
                    max_characters=max_characters,
                )
            )
            heading = match.group(2).strip()[:500]
            section_lines = []
        else:
            section_lines.append(line)
    chunks.extend(
        _section_chunks(heading, section_lines, max_characters=max_characters)
    )
    return chunks


def _query_terms(query: str) -> list[str]:
    relevant_text = " ".join(_relevant_query_segments(query))
    return _terms_from_text(relevant_text, minimum_length=3)[:40]


def _term_occurrences(term: str, candidates: list[str]) -> int:
    count = 0
    for candidate in candidates:
        prefix_length = min(6, len(term), len(candidate))
        if candidate == term or (
            prefix_length >= 5 and candidate[:prefix_length] == term[:prefix_length]
        ):
            count += 1
    return count


def _chunk_evidence(
    heading: str | None,
    text: str,
    terms: list[str],
) -> tuple[float, tuple[str, ...]]:
    heading_tokens = _RANK_TOKEN.findall((heading or "").casefold())
    text_tokens = _RANK_TOKEN.findall(text.casefold())
    score = 0.0
    matched: list[str] = []
    for term in terms:
        heading_count = _term_occurrences(term, heading_tokens)
        text_count = _term_occurrences(term, text_tokens)
        if heading_count or text_count:
            matched.append(term)
        weight = 1.0 + min(len(term), 12) / 12
        score += heading_count * weight * 4
        score += min(text_count, 8) * weight
    return round(score, 4), tuple(matched)


def pack_document_context(
    query: str,
    results: list[SwirlSearchResult],
    *,
    max_context_characters: int,
    max_chunk_characters: int,
    max_chunks_per_document: int,
    max_context_results: int,
    max_snippet_characters: int,
    min_chunk_relevance: float,
) -> list[SwirlSearchResult]:
    """Select relevant chunks with source diversity and a global character budget."""
    terms = _query_terms(query)
    primary_terms = primary_subject_terms(query)
    effective_min_relevance = min_chunk_relevance if terms else 0
    by_document: dict[int, list[_ContentChunk]] = {}
    for document_index, result in enumerate(results):
        if not result.content:
            continue
        raw_chunks = markdown_content_chunks(
            result.content,
            max_characters=max_chunk_characters,
        )
        candidates: list[_ContentChunk] = []
        for chunk_index, (heading, text) in enumerate(raw_chunks):
            relevance_score, matched_terms = _chunk_evidence(heading, text, terms)
            primary_score, _matched_primary = _chunk_evidence(
                heading,
                text,
                primary_terms,
            )
            if relevance_score < effective_min_relevance:
                continue
            candidates.append(
                _ContentChunk(
                    document_index=document_index,
                    chunk_index=chunk_index,
                    heading=heading,
                    text=text,
                    relevance_score=round(relevance_score + primary_score * 4, 4),
                    primary_relevance_score=primary_score,
                    matched_terms=matched_terms,
                )
            )
        by_document[document_index] = sorted(
            candidates,
            key=lambda item: (
                -item.primary_relevance_score,
                -item.relevance_score,
                item.chunk_index,
            ),
        )

    document_order = sorted(
        (index for index, candidates in by_document.items() if candidates),
        key=lambda index: (
            -max(item.primary_relevance_score for item in by_document[index]),
            -max(item.relevance_score for item in by_document[index]),
            -(results[index].retrieval_score or 0),
            index,
        ),
    )[:max_context_results]
    by_document = {index: by_document[index] for index in document_order}

    selected: dict[int, list[_ContentChunk]] = {}
    selected_keys: set[tuple[int, int]] = set()
    remaining = max_context_characters

    def add(candidate: _ContentChunk) -> bool:
        nonlocal remaining
        heading_cost = len(candidate.heading or "") + 80
        available_text = remaining - heading_cost
        if available_text < 200:
            return False
        text = candidate.text[:available_text].rstrip()
        chosen = _ContentChunk(
            document_index=candidate.document_index,
            chunk_index=candidate.chunk_index,
            heading=candidate.heading,
            text=text,
            relevance_score=candidate.relevance_score,
            primary_relevance_score=candidate.primary_relevance_score,
            matched_terms=candidate.matched_terms,
        )
        selected.setdefault(candidate.document_index, []).append(chosen)
        selected_keys.add((candidate.document_index, candidate.chunk_index))
        remaining -= chosen.context_cost
        return True

    # Preserve source diversity before spending the remaining budget on globally best chunks.
    for document_index in document_order:
        if by_document[document_index] and not add(by_document[document_index][0]):
            break

    remaining_candidates = sorted(
        (candidate for candidates in by_document.values() for candidate in candidates),
        key=lambda item: (
            -item.primary_relevance_score,
            -item.relevance_score,
            item.document_index,
            item.chunk_index,
        ),
    )
    for candidate in remaining_candidates:
        key = (candidate.document_index, candidate.chunk_index)
        if key in selected_keys:
            continue
        if len(selected.get(candidate.document_index, [])) >= max_chunks_per_document:
            continue
        if not add(candidate):
            break

    packed: list[SwirlSearchResult] = []
    for index in document_order:
        result = results[index]
        excerpts = [
            SwirlContentExcerpt(
                heading=item.heading,
                text=item.text,
                relevance_score=item.relevance_score,
                matched_terms=list(item.matched_terms),
            )
            for item in sorted(
                selected.get(index, []),
                key=lambda item: (-item.relevance_score, item.chunk_index),
            )
        ]
        was_fetched = result.content_fetched or bool(result.content)
        if was_fetched and not excerpts:
            continue
        selected_characters = sum(len(item.text) for item in excerpts)
        packed.append(
            result.model_copy(
                update={
                    "snippet": result.snippet[:max_snippet_characters],
                    "content": None,
                    "excerpts": excerpts,
                    "content_fetched": was_fetched,
                    "content_truncated": result.content_truncated
                    or bool(result.content and selected_characters < len(result.content)),
                }
            )
        )
    return packed


def summarize_retrieval(
    query: str,
    results: list[SwirlSearchResult],
) -> dict[str, object]:
    query_terms = _query_terms(query)
    primary_terms = primary_subject_terms(query)
    matched_terms = {
        term
        for result in results
        for excerpt in result.excerpts
        for term in excerpt.matched_terms
    }
    relevant_documents = sum(bool(result.excerpts) for result in results)
    coverage = len(matched_terms) / len(query_terms) if query_terms else 1.0
    matched_primary = [term for term in primary_terms if term in matched_terms]
    primary_coverage = len(matched_primary) / len(primary_terms) if primary_terms else 1.0
    return {
        "primary_topic_terms": primary_terms,
        "matched_primary_topic_terms": matched_primary,
        "uncovered_primary_topic_terms": [
            term for term in primary_terms if term not in matched_terms
        ],
        "primary_topic_coverage": round(primary_coverage, 3),
        "query_terms": query_terms,
        "matched_terms": [term for term in query_terms if term in matched_terms],
        "uncovered_terms": [term for term in query_terms if term not in matched_terms],
        "term_coverage": round(coverage, 3),
        "relevant_documents": relevant_documents,
        "topic_coverage_sufficient": (
            relevant_documents > 0 and coverage >= 0.25 and primary_coverage == 1.0
        ),
    }


def search_workflow_context(
    client: SwirlSearcher,
    query: str,
    *,
    providers: list[str],
    max_results: int,
    fallback_on_empty: bool,
    max_fallback_queries: int,
    expand_query: bool = False,
    rank_fusion_k: int = 60,
    focused_query_weight: float = 1.5,
    fetch_content: bool = False,
    max_content_documents: int = 5,
    max_content_characters: int = 50_000,
    min_content_documents: int = 1,
    max_context_characters: int = 12_000,
    max_chunk_characters: int = 2000,
    max_chunks_per_document: int = 2,
    max_context_results: int = 8,
    max_snippet_characters: int = 600,
    min_chunk_relevance: float = 1.0,
) -> list[SwirlSearchResult]:
    response = client.search(query, providers=providers, max_results=max_results)
    searches: list[tuple[str, list[SwirlSearchResult], float]] = [
        ("full_query", response.results, 1.0)
    ]
    if expand_query or (not response.results and fallback_on_empty):
        primary_terms = primary_subject_terms(query)
        for term in focused_search_terms(query, max_queries=max_fallback_queries):
            fallback = client.search(term, providers=providers, max_results=max_results)
            is_primary = any(_term_occurrences(primary, [term.casefold()]) for primary in primary_terms)
            weight = focused_query_weight * (4 if is_primary else 1)
            searches.append((term, fallback.results, weight))
    results = fuse_search_results(
        searches,
        max_results=max_results,
        rank_fusion_k=rank_fusion_k,
    )
    if not results:
        return []

    if not fetch_content:
        return results

    hydrated = list(results)
    loaded = 0
    last_error: SwirlSearchError | None = None
    for index, result in enumerate(results[:max_content_documents]):
        if not result.document_id:
            continue
        try:
            hydrated[index] = client.fetch_document(
                result,
                max_characters=max_content_characters,
            )
            loaded += 1
        except SwirlSearchError as exc:
            last_error = exc
    if loaded < min_content_documents:
        detail = f": {last_error}" if last_error is not None else ""
        raise SwirlSearchError(
            "SWIRL full-content retrieval returned "
            f"{loaded} document(s), required {min_content_documents}{detail}"
        )
    return pack_document_context(
        query,
        hydrated,
        max_context_characters=max_context_characters,
        max_chunk_characters=max_chunk_characters,
        max_chunks_per_document=max_chunks_per_document,
        max_context_results=max_context_results,
        max_snippet_characters=max_snippet_characters,
        min_chunk_relevance=min_chunk_relevance,
    )
