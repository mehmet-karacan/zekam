"""Hibrit retrieval orchestration ve golden degerlendirme.

Sira: exact identifier -> lexical (FTS/trigram) -> dense -> RRF -> opsiyonel
reranker -> dedupe -> parent expansion -> token butceli baglam. Reranker
basarisiz olursa fusion sonucuna geri donulur; sonuc kaybolmaz.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.retrieval import (
    AnswerState,
    Citation,
    FusedHit,
    RetrievalAnswer,
    RetrievalChannel,
    ScoredHit,
    dedupe,
    estimate_tokens,
    expand_parents,
    extract_identifiers,
    reciprocal_rank_fusion,
)

#: Reranker basarisiz olursa cagiran taraf fusion sirasini kullanir.
Reranker = Callable[[str, tuple[FusedHit, ...]], tuple[FusedHit, ...]]


class SearchBackend(Protocol):
    """Kanal aramalarini saglayan altyapi."""

    def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]: ...

    def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]: ...

    def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]: ...


@dataclass(frozen=True, slots=True)
class ChunkView:
    """Baglam kurulumu icin gereken minimum chunk bilgisi."""

    chunk_id: str
    document_id: str
    text: str
    locator: Any
    content_digest: str
    parent_id: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalTrace:
    """Kararin aciklamasi: hangi kanal ne buldu, ne neden elendi."""

    identifiers: tuple[str, ...]
    per_channel: dict[str, int]
    fused_count: int
    after_dedupe: int
    reranker_used: bool
    reranker_failed: bool
    dropped_for_budget: tuple[str, ...] = field(default_factory=tuple)

    def as_lines(self) -> tuple[str, ...]:
        lines = [
            f"exact kimlik: {', '.join(self.identifiers) or '-'}",
            "kanal sonuclari: "
            + ", ".join(f"{name}={count}" for name, count in sorted(self.per_channel.items())),
            f"fusion sonrasi: {self.fused_count}, dedupe sonrasi: {self.after_dedupe}",
        ]
        if self.reranker_failed:
            lines.append("reranker basarisiz; fusion sirasina geri donuldu")
        elif self.reranker_used:
            lines.append("reranker uygulandi")
        if self.dropped_for_budget:
            lines.append(f"token butcesi nedeniyle disarida: {', '.join(self.dropped_for_budget)}")
        return tuple(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "identifiers": list(self.identifiers),
            "per_channel": dict(self.per_channel),
            "fused_count": self.fused_count,
            "after_dedupe": self.after_dedupe,
            "reranker_used": self.reranker_used,
            "reranker_failed": self.reranker_failed,
            "dropped_for_budget": list(self.dropped_for_budget),
        }


@dataclass(frozen=True, slots=True)
class RetrievalService:
    """Kanal birlestirme, dedupe, genisletme ve butceli baglam kurulumu."""

    backend: SearchBackend
    reranker: Reranker | None = None
    limit: int = 20

    def search(self, query: str) -> tuple[tuple[FusedHit, ...], RetrievalTrace]:
        identifiers = extract_identifiers(query)
        channels: dict[RetrievalChannel, tuple[ScoredHit, ...]] = {}
        if identifiers:
            channels[RetrievalChannel.EXACT] = self.backend.exact(identifiers, limit=self.limit)
        channels[RetrievalChannel.LEXICAL] = self.backend.lexical(query, limit=self.limit)
        channels[RetrievalChannel.DENSE] = self.backend.dense(query, limit=self.limit)

        exact_ids = frozenset(hit.chunk_id for hit in channels.get(RetrievalChannel.EXACT, ()))
        fused = reciprocal_rank_fusion(channels, exact_ids=exact_ids)

        reranked, used, failed = self._rerank(query, fused)
        trace = RetrievalTrace(
            identifiers=identifiers,
            per_channel={str(name): len(hits) for name, hits in channels.items()},
            fused_count=len(fused),
            after_dedupe=len(reranked),
            reranker_used=used,
            reranker_failed=failed,
        )
        return reranked, trace

    def _rerank(
        self, query: str, fused: tuple[FusedHit, ...]
    ) -> tuple[tuple[FusedHit, ...], bool, bool]:
        """Reranker hatasi sonucu kaybetmez; fusion sirasi korunur."""

        if self.reranker is None or not fused:
            return fused, False, False
        try:
            reranked = self.reranker(query, fused)
        except Exception:
            return fused, False, True
        if not reranked or {item.chunk_id for item in reranked} != {
            item.chunk_id for item in fused
        }:
            # Reranker sonuc dusurduyse guvenilmez sayilir.
            return fused, False, True
        exact_first = sorted(reranked, key=lambda item: not item.exact_match)
        return tuple(exact_first), True, False

    def build_answer(
        self,
        query: str,
        hits: tuple[FusedHit, ...],
        trace: RetrievalTrace,
        *,
        views: dict[str, ChunkView],
        token_budget: int,
        minimum_citations: int = 1,
    ) -> RetrievalAnswer:
        """Token butceli baglam kurar; kanit yetersizse abstain eder."""

        if token_budget <= 0:
            raise ValidationFailed("token butcesi pozitif olmali")
        query_digest = digest({"query": query})

        if not hits:
            return RetrievalAnswer(
                query_digest=query_digest,
                state=AnswerState.ABSTAINED_NO_HIT,
                citations=(),
                used_chunk_ids=(),
                token_budget=token_budget,
                tokens_used=0,
                explanation=trace.as_lines(),
            )

        unique = dedupe(hits, content_digests={k: v.content_digest for k, v in views.items()})
        ordered = expand_parents(
            unique, parents={k: v.parent_id for k, v in views.items() if v.parent_id}
        )

        used: list[str] = []
        citations: list[Citation] = []
        tokens = 0
        dropped: list[str] = []
        for chunk_id in ordered:
            view = views.get(chunk_id)
            if view is None:
                continue
            cost = estimate_tokens(view.text)
            if tokens + cost > token_budget:
                dropped.append(chunk_id)
                continue
            tokens += cost
            used.append(chunk_id)
            citations.append(
                Citation(
                    chunk_id=view.chunk_id,
                    document_id=view.document_id,
                    locator=view.locator,
                    content_digest=view.content_digest,
                )
            )

        explanation = RetrievalTrace(
            identifiers=trace.identifiers,
            per_channel=trace.per_channel,
            fused_count=trace.fused_count,
            after_dedupe=len(unique),
            reranker_used=trace.reranker_used,
            reranker_failed=trace.reranker_failed,
            dropped_for_budget=tuple(dropped),
        ).as_lines()

        if len(citations) < minimum_citations:
            return RetrievalAnswer(
                query_digest=query_digest,
                state=AnswerState.ABSTAINED_LOW_EVIDENCE,
                citations=(),
                used_chunk_ids=(),
                token_budget=token_budget,
                tokens_used=0,
                explanation=explanation,
            )

        return RetrievalAnswer(
            query_digest=query_digest,
            state=AnswerState.ANSWERED,
            citations=tuple(citations),
            used_chunk_ids=tuple(used),
            token_budget=token_budget,
            tokens_used=tokens,
            explanation=explanation,
        )


# -- golden degerlendirme -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """Tek degerlendirme ornegi: sorgu ve beklenen chunk kimlikleri."""

    query: str
    relevant_ids: frozenset[str]

    def __post_init__(self) -> None:
        if not self.relevant_ids:
            raise ValidationFailed("golden ornegi en az bir dogru sonuc ister")


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    """Recall, MRR ve nDCG. Baseline karsilastirmasi icin deterministiktir."""

    recall_at_k: float
    mrr: float
    ndcg_at_k: float
    k: int
    case_count: int

    def improves_on(self, baseline: EvaluationResult) -> bool:
        """Hicbir metrik gerilemeden en az birinde iyilesme var mi?"""

        metrics = (
            (self.recall_at_k, baseline.recall_at_k),
            (self.mrr, baseline.mrr),
            (self.ndcg_at_k, baseline.ndcg_at_k),
        )
        if any(current < previous - 1e-9 for current, previous in metrics):
            return False
        return any(current > previous + 1e-9 for current, previous in metrics)

    def as_dict(self) -> dict[str, Any]:
        return {
            "recall_at_k": round(self.recall_at_k, 6),
            "mrr": round(self.mrr, 6),
            "ndcg_at_k": round(self.ndcg_at_k, 6),
            "k": self.k,
            "case_count": self.case_count,
        }


def evaluate(
    cases: tuple[GoldenCase, ...],
    *,
    run: Callable[[str], tuple[str, ...]],
    k: int = 10,
) -> EvaluationResult:
    """Golden kume uzerinde Recall@k, MRR ve nDCG@k hesaplar."""

    if not cases:
        raise ValidationFailed("degerlendirme icin en az bir ornek gerekiyor")
    if k <= 0:
        raise ValidationFailed("k pozitif olmali")

    import math

    recalls: list[float] = []
    reciprocal: list[float] = []
    gains: list[float] = []
    for case in cases:
        ranked = run(case.query)[:k]
        hits = [1.0 if item in case.relevant_ids else 0.0 for item in ranked]
        recalls.append(sum(hits) / len(case.relevant_ids))
        first = next((index for index, value in enumerate(hits, start=1) if value), None)
        reciprocal.append(1.0 / first if first else 0.0)
        dcg = sum(value / math.log2(index + 1) for index, value in enumerate(hits, start=1))
        ideal_count = min(len(case.relevant_ids), k)
        idcg = sum(1.0 / math.log2(index + 1) for index in range(1, ideal_count + 1))
        gains.append(dcg / idcg if idcg else 0.0)

    count = len(cases)
    return EvaluationResult(
        recall_at_k=sum(recalls) / count,
        mrr=sum(reciprocal) / count,
        ndcg_at_k=sum(gains) / count,
        k=k,
        case_count=count,
    )
