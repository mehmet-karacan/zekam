"""Baglam paketleme sozlesmesi: cok aday butceyi sifira bolmemeli (RAG26-R04)."""

from __future__ import annotations

import pytest

from zekam.application.retrieval_service import (
    MAX_PACKED_CHUNKS,
    MIN_CONTEXT_WINDOW_TOKENS,
    ChunkView,
    RetrievalService,
    RetrievalTrace,
)
from zekam.domain.canonical import digest
from zekam.domain.knowledge import Locator
from zekam.domain.retrieval import AnswerState, FusedHit, RetrievalChannel


def _view(chunk_id: str, text: str) -> ChunkView:
    return ChunkView(
        chunk_id=chunk_id,
        document_id=f"d-{chunk_id}",
        text=text,
        locator=Locator(page=1),
        content_digest=digest(chunk_id),
    )


def _trace(count: int) -> RetrievalTrace:
    return RetrievalTrace(
        identifiers=(),
        per_channel={"dense": count},
        fused_count=count,
        after_dedupe=count,
        reranker_used=False,
        reranker_failed=False,
    )


def _answer(count: int, words: int, budget: int = 1200, minimum: int = 1):
    hits = tuple(FusedHit(f"c{i}", 1.0 - i / 1000, (RetrievalChannel.DENSE,)) for i in range(count))
    views = {f"c{i}": _view(f"c{i}", f"kelime{i} " * words) for i in range(count)}
    service = RetrievalService(object())  # type: ignore[arg-type]
    return service.build_answer(
        "soru",
        hits,
        _trace(count),
        views=views,
        token_budget=budget,
        minimum_citations=minimum,
    )


@pytest.mark.unit
def test_many_candidates_do_not_starve_the_budget() -> None:
    answer = _answer(count=40, words=150)
    assert answer.state is AnswerState.ANSWERED
    assert len(answer.citations) >= 3
    assert answer.tokens_used <= 1200


@pytest.mark.unit
def test_share_never_drops_below_the_minimum_window() -> None:
    answer = _answer(count=200, words=400)
    assert answer.state is AnswerState.ANSWERED
    assert answer.tokens_used >= MIN_CONTEXT_WINDOW_TOKENS
    assert answer.tokens_used <= 1200


@pytest.mark.unit
def test_top_ranked_chunks_win_and_order_is_preserved() -> None:
    answer = _answer(count=40, words=60)
    assert list(answer.used_chunk_ids) == [f"c{i}" for i in range(len(answer.used_chunk_ids))]
    assert len(answer.used_chunk_ids) <= 40


@pytest.mark.unit
def test_small_chunks_fit_whole_up_to_the_budget() -> None:
    answer = _answer(count=10, words=10)
    assert len(answer.used_chunk_ids) == 10


@pytest.mark.unit
def test_multi_object_minimum_citations_raises_the_packed_chunk_cap() -> None:
    answer = _answer(count=40, words=150, minimum=MAX_PACKED_CHUNKS + 3)
    assert len(answer.citations) >= 1
    assert answer.tokens_used <= 1200
