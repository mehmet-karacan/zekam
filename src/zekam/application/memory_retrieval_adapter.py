"""Native memory corpus'unu ortak retrieval cekirdegine baglayan adapter."""

from __future__ import annotations

from dataclasses import dataclass

from zekam.domain.memory import MemoryRecord
from zekam.domain.retrieval import RetrievalChannel, ScoredHit


@dataclass(frozen=True, slots=True)
class MemoryRetrievalAdapter:
    """Onceden yetki/kapsam filtresinden gecmis bellek adaylarini sunar.

    Bu sinif fusion veya toplam skor hesaplamaz. Exact, lexical ve dense
    kanallari yalniz aday ve kanal-ici sira uretir; RRF ve rerank ortak
    ``RetrievalService`` tarafindan uygulanir.
    """

    records: tuple[MemoryRecord, ...]
    query_text: str
    query_entities: tuple[str, ...] = ()
    lexical_hits: frozenset[str] = frozenset()
    lexical_ranks: dict[str, int] | None = None
    vector_ranks: dict[str, int] | None = None
    source_type: str = "memory"

    def exact(self, identifiers: tuple[str, ...], *, limit: int) -> tuple[ScoredHit, ...]:
        needle = self.query_text.strip().casefold()
        identifiers_folded = tuple(item.casefold() for item in identifiers)
        entities = frozenset(item.casefold() for item in self.query_entities)
        matched = []
        for record in self.records:
            content = record.content.casefold()
            record_entities = frozenset(item.casefold() for item in record.entities)
            if (
                (needle and needle in content)
                or any(item in content for item in identifiers_folded)
                or bool(entities & record_entities)
            ):
                matched.append(record.memory_id)
        return self._hits(tuple(sorted(matched))[:limit], RetrievalChannel.EXACT)

    def lexical(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        allowed = {record.memory_id for record in self.records}
        ranks = self.lexical_ranks or {
            memory_id: index for index, memory_id in enumerate(sorted(self.lexical_hits), start=1)
        }
        ordered = sorted(
            ((memory_id, rank) for memory_id, rank in ranks.items() if memory_id in allowed),
            key=lambda item: (item[1], item[0]),
        )[:limit]
        return tuple(
            ScoredHit(
                chunk_id=memory_id,
                channel=RetrievalChannel.LEXICAL,
                rank=index,
                raw_score=1.0 / (1 + source_rank),
            )
            for index, (memory_id, source_rank) in enumerate(ordered, start=1)
        )

    def dense(self, query: str, *, limit: int) -> tuple[ScoredHit, ...]:
        allowed = {record.memory_id for record in self.records}
        ranks = self.vector_ranks or {}
        ordered = sorted(
            ((memory_id, rank) for memory_id, rank in ranks.items() if memory_id in allowed),
            key=lambda item: (item[1], item[0]),
        )[:limit]
        return tuple(
            ScoredHit(
                chunk_id=memory_id,
                channel=RetrievalChannel.DENSE,
                rank=index,
                raw_score=1.0 / (1 + source_rank),
            )
            for index, (memory_id, source_rank) in enumerate(ordered, start=1)
        )

    @staticmethod
    def _hits(memory_ids: tuple[str, ...], channel: RetrievalChannel) -> tuple[ScoredHit, ...]:
        return tuple(
            ScoredHit(chunk_id=memory_id, channel=channel, rank=index, raw_score=1.0)
            for index, memory_id in enumerate(memory_ids, start=1)
        )
