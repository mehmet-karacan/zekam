"""Hibrit retrieval sozlesmesi.

Chunk yapiyi ve locator'i korur. Dense ve lexical skorlar **kalibrasyonsuz
toplanmaz**; RRF ile birlestirilir. Exact identifier dusuk dense skor yuzunden
elenemez. Yeterli kanit yoksa cevap uretilmez; abstain edilir.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import ContentUnit, Locator, UnitKind

#: Reciprocal Rank Fusion sabiti; kucuk k ust siralari daha cok odullendirir.
RRF_K = 60

DEFAULT_CHUNK_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 64
MAX_CHUNK_TOKENS = 2048

#: Kaba token tahmini icin sozcuk ayirici.
_TOKEN = re.compile(r"\w+|[^\w\s]")

#: Teknik kimlik gorunumundeki sorgu parcalari (ZEKAM-P12-T01, #123, app.musteri).
#: `#123` icin `\b` kullanilamaz: `#` sozcuk karakteri degildir, bu yuzden dizgenin
#: basinda sinir olusturmaz ve `#` kirpilirdi.
_IDENTIFIER = re.compile(r"(?<![\w#])#\d+|\b(?:[A-Z][A-Z0-9]+(?:-[A-Z0-9]+)+|\w+\.\w+|\d{3,})\b")


def estimate_tokens(text: str) -> int:
    """Saglayicidan bagimsiz, deterministik token tahmini."""

    return len(_TOKEN.findall(text))


def extract_identifiers(query: str) -> tuple[str, ...]:
    """Sorgudaki exact teknik kimlikleri dondurur."""

    return tuple(dict.fromkeys(_IDENTIFIER.findall(query)))


class RetrievalChannel(StrEnum):
    EXACT = "exact"
    LEXICAL = "lexical"
    DENSE = "dense"


class AnswerState(StrEnum):
    ANSWERED = "answered"
    ABSTAINED_NO_HIT = "abstained-no-hit"
    ABSTAINED_LOW_EVIDENCE = "abstained-low-evidence"


@dataclass(frozen=True, slots=True)
class ChunkProfile:
    """Chunk uretim profili. Profil degisirse yeniden indeksleme gerekir."""

    name: str
    max_tokens: int = DEFAULT_CHUNK_TOKENS
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS
    keep_tables_whole: bool = True
    keep_code_whole: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.max_tokens <= MAX_CHUNK_TOKENS:
            raise ValidationFailed(f"chunk boyutu 1..{MAX_CHUNK_TOKENS} araliginda olmali")
        if not 0 <= self.overlap_tokens < self.max_tokens:
            raise ValidationFailed("ortusme chunk boyutundan kucuk olmali")
        if not self.name.strip():
            raise ValidationFailed("profil adi bos olamaz")

    @property
    def profile_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "max_tokens": self.max_tokens,
            "overlap_tokens": self.overlap_tokens,
            "keep_tables_whole": self.keep_tables_whole,
            "keep_code_whole": self.keep_code_whole,
        }


@dataclass(frozen=True, slots=True)
class Chunk:
    """Indekslenebilir metin parcasi. Locator ve ebeveyn baglantisi korunur."""

    chunk_id: str
    document_id: str
    text: str
    locator: Locator
    kind: UnitKind
    token_count: int
    order: int
    parent_id: str | None = None
    profile_digest: str = ""

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValidationFailed("chunk bos olamaz")
        if self.token_count <= 0:
            raise ValidationFailed("token sayisi pozitif olmali")
        if self.locator.is_empty:
            raise ValidationFailed("locator'siz chunk kabul edilmez")
        if self.parent_id == self.chunk_id:
            raise ValidationFailed("chunk kendi ebeveyni olamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "text": self.text,
            "locator": self.locator.as_dict(),
            "kind": str(self.kind),
            "token_count": self.token_count,
            "order": self.order,
            "parent_id": self.parent_id,
            "profile_digest": self.profile_digest,
        }

    @property
    def chunk_digest(self) -> str:
        return digest(self.as_dict())


def chunk_units(
    units: tuple[ContentUnit, ...], *, document_id: str, profile: ChunkProfile
) -> tuple[Chunk, ...]:
    """Icerik birimlerini yapiyi bozmadan chunk'lara ayirir.

    Baslik altindaki paragraflar birlestirilir; tablo ve kod bloklari profile
    gore butun kalir. Buyuk birim parcalanirsa parcalar ebeveyn chunk'a baglanir.
    """

    if not units:
        raise ValidationFailed("chunk uretimi icin birim gerekiyor")

    chunks: list[Chunk] = []
    buffer: list[ContentUnit] = []

    def flush() -> None:
        if not buffer:
            return
        text = "\n\n".join(item.text for item in buffer)
        chunks.append(
            Chunk(
                chunk_id=f"{document_id}-c{len(chunks)}",
                document_id=document_id,
                text=text,
                locator=buffer[0].locator,
                kind=buffer[0].kind,
                token_count=estimate_tokens(text),
                order=len(chunks),
                profile_digest=profile.profile_digest,
            )
        )
        buffer.clear()

    for unit in units:
        whole = (unit.kind is UnitKind.TABLE and profile.keep_tables_whole) or (
            unit.kind is UnitKind.CODE and profile.keep_code_whole
        )
        tokens = estimate_tokens(unit.text)

        if whole or unit.kind is UnitKind.HEADING:
            flush()
            chunks.extend(_emit(unit, document_id, profile, len(chunks), split=False))
            continue

        if tokens > profile.max_tokens:
            flush()
            chunks.extend(_emit(unit, document_id, profile, len(chunks), split=True))
            continue

        pending = estimate_tokens("\n\n".join(item.text for item in [*buffer, unit]))
        if buffer and pending > profile.max_tokens:
            flush()
        buffer.append(unit)

    flush()
    return tuple(chunks)


def _emit(
    unit: ContentUnit, document_id: str, profile: ChunkProfile, start: int, *, split: bool
) -> list[Chunk]:
    """Tek birimi chunk'a cevirir; gerekiyorsa parent-child olarak boler."""

    tokens = _TOKEN.findall(unit.text)
    parent = Chunk(
        chunk_id=f"{document_id}-c{start}",
        document_id=document_id,
        text=unit.text,
        locator=unit.locator,
        kind=unit.kind,
        token_count=len(tokens),
        order=start,
        profile_digest=profile.profile_digest,
    )
    if not split or len(tokens) <= profile.max_tokens:
        return [parent]

    produced = [parent]
    step = profile.max_tokens - profile.overlap_tokens
    words = unit.text.split()
    window = max(1, profile.max_tokens)
    index = 0
    position = 0
    while position < len(words):
        piece = " ".join(words[position : position + window])
        if not piece.strip():
            break
        produced.append(
            Chunk(
                chunk_id=f"{parent.chunk_id}-p{index}",
                document_id=document_id,
                text=piece,
                locator=unit.locator,
                kind=unit.kind,
                token_count=estimate_tokens(piece),
                order=start + len(produced),
                parent_id=parent.chunk_id,
                profile_digest=profile.profile_digest,
            )
        )
        index += 1
        position += max(1, step)
    return produced


@dataclass(frozen=True, slots=True)
class EmbeddingProfile:
    """Vektor profili. Farkli prefix veya boyut ayni profil altinda karisamaz."""

    model_ref: str
    dimension: int
    distance: str = "cosine"
    query_prefix: str = ""
    passage_prefix: str = ""

    def __post_init__(self) -> None:
        if self.dimension <= 0:
            raise ValidationFailed("boyut pozitif olmali")
        if self.distance not in {"cosine", "l2", "ip"}:
            raise ValidationFailed("uzaklik olcusu taninmiyor")
        if not self.model_ref.strip():
            raise ValidationFailed("model referansi bos olamaz")

    def validate_vector(self, vector: tuple[float, ...]) -> None:
        """Boyut ve sonlu deger kontrolu; NaN/Inf indekslenmez."""

        if len(vector) != self.dimension:
            raise ValidationFailed("vektor boyutu profille uyusmuyor")
        if any(not math.isfinite(value) for value in vector):
            raise ValidationFailed("vektor sonlu olmayan deger tasiyor")

    @property
    def profile_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_ref": self.model_ref,
            "dimension": self.dimension,
            "distance": self.distance,
            "query_prefix": self.query_prefix,
            "passage_prefix": self.passage_prefix,
        }


def bge_m3_profile(*, query_prefix: str = "", passage_prefix: str = "") -> EmbeddingProfile:
    """Ilk kanonik profil: BGE-M3 dense 1024, cosine."""

    return EmbeddingProfile(
        model_ref="openai/BAAI/bge-m3",
        dimension=1024,
        distance="cosine",
        query_prefix=query_prefix,
        passage_prefix=passage_prefix,
    )


def requires_reindex(current: EmbeddingProfile, incoming: EmbeddingProfile) -> bool:
    """Profil degisikligi yeniden indekslemeyi zorunlu kilar mi?"""

    return current.profile_digest != incoming.profile_digest


@dataclass(frozen=True, slots=True)
class ScoredHit:
    """Tek kanaldan gelen sonuc. Ham skor kanallar arasi karsilastirilamaz."""

    chunk_id: str
    channel: RetrievalChannel
    rank: int
    raw_score: float

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValidationFailed("sira 1'den kucuk olamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "channel": str(self.channel),
            "rank": self.rank,
            "raw_score": self.raw_score,
        }


@dataclass(frozen=True, slots=True)
class FusedHit:
    """RRF sonrasi birlesik sonuc."""

    chunk_id: str
    score: float
    channels: tuple[RetrievalChannel, ...]
    exact_match: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "score": round(self.score, 6),
            "channels": [str(item) for item in self.channels],
            "exact_match": self.exact_match,
        }


def reciprocal_rank_fusion(
    channels: Mapping[RetrievalChannel, tuple[ScoredHit, ...]],
    *,
    exact_ids: frozenset[str] = frozenset(),
    k: int = RRF_K,
) -> tuple[FusedHit, ...]:
    """Kanallari kalibrasyonsuz toplama yapmadan birlestirir.

    Ham dense ve lexical skorlar farkli olceklerdedir; yalnizca **sira** kullanilir.
    Exact eslesmeler her zaman en uste alinir ve dusuk dense skorla elenemez.
    """

    if k <= 0:
        raise ValidationFailed("RRF sabiti pozitif olmali")

    totals: dict[str, float] = {}
    seen: dict[str, list[RetrievalChannel]] = {}
    for channel, hits in channels.items():
        ranks = [hit.rank for hit in hits]
        if len(set(ranks)) != len(ranks):
            raise ValidationFailed("bir kanalda sira tekrar edemez")
        for hit in hits:
            totals[hit.chunk_id] = totals.get(hit.chunk_id, 0.0) + 1.0 / (k + hit.rank)
            seen.setdefault(hit.chunk_id, []).append(channel)

    fused = [
        FusedHit(
            chunk_id=chunk_id,
            score=score,
            channels=tuple(sorted(seen[chunk_id], key=str)),
            exact_match=chunk_id in exact_ids,
        )
        for chunk_id, score in totals.items()
    ]
    # Exact eslesme once; sonra RRF skoru; esitlikte kimlik ile deterministik sira.
    fused.sort(key=lambda item: (not item.exact_match, -item.score, item.chunk_id))
    return tuple(fused)


def dedupe(hits: tuple[FusedHit, ...], *, content_digests: dict[str, str]) -> tuple[FusedHit, ...]:
    """Ayni icerigi iki kez baglama koymaz; ilk (en yuksek) sonucu korur."""

    kept: list[FusedHit] = []
    seen: set[str] = set()
    for hit in hits:
        content = content_digests.get(hit.chunk_id, hit.chunk_id)
        if content in seen:
            continue
        seen.add(content)
        kept.append(hit)
    return tuple(kept)


def expand_parents(hits: tuple[FusedHit, ...], *, parents: dict[str, str]) -> tuple[str, ...]:
    """Cocuk chunk secildiginde ebeveyni de baglama alir; tekrar uretmez."""

    ordered: list[str] = []
    for hit in hits:
        parent = parents.get(hit.chunk_id)
        if parent is not None and parent not in ordered:
            ordered.append(parent)
        if hit.chunk_id not in ordered:
            ordered.append(hit.chunk_id)
    return tuple(ordered)


@dataclass(frozen=True, slots=True)
class Citation:
    """Cevaptaki her iddianin exact kaynagi."""

    chunk_id: str
    document_id: str
    locator: Locator
    content_digest: str

    def __post_init__(self) -> None:
        parse_digest(self.content_digest)
        if self.locator.is_empty:
            raise ValidationFailed("citation locator'siz olamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "locator": self.locator.as_dict(),
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True, slots=True)
class RetrievalAnswer:
    """Retrieval sonucu. Authority degildir ve kanitsiz cevap uretmez."""

    query_digest: str
    state: AnswerState
    citations: tuple[Citation, ...]
    used_chunk_ids: tuple[str, ...]
    token_budget: int
    tokens_used: int
    explanation: tuple[str, ...] = field(default_factory=tuple)
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("retrieval sonucu authority veremez")
        if self.state is AnswerState.ANSWERED and not self.citations:
            raise ValidationFailed("kanitsiz cevap uretilemez")
        if self.state is not AnswerState.ANSWERED and self.citations:
            raise ValidationFailed("abstain eden cevap citation tasiyamaz")
        if self.tokens_used > self.token_budget:
            raise PolicyViolation("baglam token butcesini asiyor")

    @property
    def is_answered(self) -> bool:
        return self.state is AnswerState.ANSWERED

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-retrieval-answer/v1",
            "query_digest": self.query_digest,
            "state": str(self.state),
            "citations": [item.as_dict() for item in self.citations],
            "used_chunk_ids": list(self.used_chunk_ids),
            "token_budget": self.token_budget,
            "tokens_used": self.tokens_used,
            "explanation": list(self.explanation),
            "grants_authority": False,
        }

    @property
    def answer_digest(self) -> str:
        return digest(self.as_dict())
