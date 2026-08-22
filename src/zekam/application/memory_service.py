"""Native MemoryEngine, hibrit bellek aramasi, hijyen ve Mem0 adapteri.

Native PostgreSQL motoru kanoniktir. Mem0 opsiyonel bir adapterdir ve **otorite
degildir**: harici kayit farkli oldugunda native kayit gecerlidir, fark drift
olarak gorunur kalir.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.memory import (
    REVIEW_REQUIRED_CLASSES,
    HygieneFinding,
    HygieneReport,
    MemoryCandidate,
    MemoryClass,
    MemoryHit,
    MemoryQuery,
    MemoryRecord,
    MemoryState,
    RetentionPolicy,
    SyncState,
    SyncStatus,
    supersede,
)

#: Failure dersi icin gereken bagimsiz gozlem sayisi.
MINIMUM_FAILURE_OBSERVATIONS = 2

#: Kullanilmayan kayit esigi (gun).
UNUSED_AFTER_DAYS = 180


@dataclass(frozen=True, slots=True)
class ReviewDecision:
    """Aday inceleme sonucu."""

    approved: bool
    reviewer_ref: str
    reason: str

    def __post_init__(self) -> None:
        if not self.reviewer_ref.strip():
            raise ValidationFailed("reviewer referansi bos olamaz")
        if not self.approved and not self.reason.strip():
            raise ValidationFailed("red gerekce ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reviewer_ref": self.reviewer_ref,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PromotionGate:
    """Adayin aktiflesme kapisi.

    Ham model ciktisi dogrudan gecemez: kanit, bagimsiz review ve failure icin
    yeterli gozlem sarttir.
    """

    minimum_failure_observations: int = MINIMUM_FAILURE_OBSERVATIONS

    def evaluate(
        self, candidate: MemoryCandidate, decision: ReviewDecision | None
    ) -> tuple[bool, str]:
        """Aday aktiflesebilir mi? (karar, gerekce)"""

        if candidate.key.is_ephemeral:
            return False, "gecici kapsam kalici bellek uretemez"
        if not candidate.evidence:
            return False, "kanitsiz aday aktiflesemez"
        if candidate.memory_class in REVIEW_REQUIRED_CLASSES:
            if decision is None:
                return False, "bu sinif bagimsiz review ister"
            if not decision.approved:
                return False, f"review reddetti: {decision.reason}"
            if decision.reviewer_ref == candidate.author_ref:
                return False, "review yazarla ayni kimlik olamaz"
        if (
            candidate.memory_class is MemoryClass.FAILURE
            and candidate.observation_count < self.minimum_failure_observations
        ):
            return False, (
                f"failure dersi en az {self.minimum_failure_observations} bagimsiz gozlem ister"
            )
        return True, "kanit ve review kapilari gecildi"


@dataclass(frozen=True, slots=True)
class NativeMemoryEngine:
    """Kanonik bellek motoru. Kayitlar cagiran tarafca kalicilastirilir."""

    gate: PromotionGate = field(default_factory=PromotionGate)
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)

    def write(
        self,
        candidate: MemoryCandidate,
        *,
        now: dt.datetime,
        decision: ReviewDecision | None = None,
        memory_id: str | None = None,
    ) -> MemoryRecord:
        """Adayi degerlendirir ve gecerse aktif kayda cevirir."""

        allowed, reason = self.gate.evaluate(candidate, decision)
        if not allowed:
            raise PolicyViolation(reason)
        return candidate.promote(
            memory_id=memory_id or candidate.candidate_id,
            reviewed_by=decision.reviewer_ref if decision else None,
            now=now,
        )

    def revise(
        self, current: MemoryRecord, content: str, *, memory_id: str, now: dt.datetime
    ) -> tuple[MemoryRecord, MemoryRecord]:
        """Mevcut kaydi ezmez; supersession iliskisi kurar."""

        return supersede(current, content, memory_id=memory_id, now=now)

    def search(
        self,
        query: MemoryQuery,
        *,
        records: Sequence[MemoryRecord],
        lexical_hits: frozenset[str] = frozenset(),
        vector_ranks: dict[str, int] | None = None,
        now: dt.datetime | None = None,
    ) -> tuple[MemoryHit, ...]:
        """Exact, FTS, vektor, varlik ve zaman bilesenlerini birlestirir.

        Her sonucun **neden secildigi** gorunur; skor tek basina yeterli degildir.
        """

        moment = now or dt.datetime.now(dt.UTC)
        ranks = vector_ranks or {}
        hits: list[MemoryHit] = []
        for record in records:
            if record.state is not MemoryState.ACTIVE:
                continue
            if not query.permits(record):
                continue
            if query.at is not None and not record.is_valid_at(query.at):
                continue

            score = 0.0
            reasons: list[str] = []
            if query.text and query.text.lower() in record.content.lower():
                score += 1.0
                reasons.append("exact metin eslesmesi")
            if record.memory_id in lexical_hits:
                score += 0.6
                reasons.append("FTS eslesmesi")
            rank = ranks.get(record.memory_id)
            if rank is not None:
                score += 1.0 / (1 + rank)
                reasons.append(f"vektor sirasi {rank}")
            shared = set(query.entities) & set(record.entities)
            if shared:
                score += 0.4 * len(shared)
                reasons.append(f"varlik eslesmesi: {', '.join(sorted(shared))}")
            if record.is_valid_at(moment):
                score += 0.2
                reasons.append("zaman araliginda gecerli")

            if reasons:
                hits.append(MemoryHit(record=record, score=score, reasons=tuple(reasons)))

        hits.sort(key=lambda item: (-item.score, item.record.memory_id))
        return tuple(hits[: query.limit])

    def hygiene(
        self,
        records: Sequence[MemoryRecord],
        *,
        now: dt.datetime,
        source_revision: str | None = None,
    ) -> HygieneReport:
        """Salt okunur hijyen taramasi. Hicbir kayit silinmez."""

        findings: list[tuple[HygieneFinding, str, str]] = []
        by_content: dict[str, list[MemoryRecord]] = {}
        for record in records:
            if record.state is not MemoryState.ACTIVE:
                continue
            by_content.setdefault(record.content.strip().lower(), []).append(record)

        for group in by_content.values():
            for duplicate in group[1:]:
                findings.append(
                    (
                        HygieneFinding.DUPLICATE,
                        duplicate.memory_id,
                        f"{group[0].memory_id} ile ayni icerik",
                    )
                )

        active = [record for record in records if record.state is MemoryState.ACTIVE]
        for index, first in enumerate(active):
            for second in active[index + 1 :]:
                if first.key == second.key and _contradicts(first, second):
                    findings.append(
                        (
                            HygieneFinding.CONFLICT,
                            second.memory_id,
                            f"{first.memory_id} ile celisiyor",
                        )
                    )

        for record in active:
            if not record.is_valid_at(now):
                findings.append((HygieneFinding.STALE, record.memory_id, "gecerlilik suresi gecti"))
            if record.last_used_at is not None:
                idle = (now - record.last_used_at).days
                if idle >= UNUSED_AFTER_DAYS:
                    findings.append(
                        (HygieneFinding.UNUSED, record.memory_id, f"{idle} gundur kullanilmadi")
                    )
            if self.retention.needs_review(record, now=now):
                findings.append(
                    (HygieneFinding.RETENTION_REVIEW, record.memory_id, "saklama suresi doldu")
                )
            if source_revision is not None:
                stale_source = any(
                    item.kind == "citation" and item.reference != source_revision
                    for item in record.evidence
                )
                if stale_source:
                    findings.append(
                        (
                            HygieneFinding.SOURCE_VERSION_CONFLICT,
                            record.memory_id,
                            "kanit baska source revision'a isaret ediyor",
                        )
                    )

        return HygieneReport(findings=tuple(findings), scanned=len(records))


#: Turkce olumlu/olumsuz fiil ciftleri. Kok bulma yerine acik tablo kullanilir:
#: "kullanilmaz" kelimesinden olumsuzluk ekini ayirmak morfolojik analiz ister ve
#: kirilgandir. Bu tablo bir *sezgidir*; hijyen zaten otomatik silme yapmaz,
#: yalnizca insan review'una aday isaretler.
_POLARITY_PAIRS: tuple[tuple[str, str], ...] = (
    ("kullanilir", "kullanilmaz"),
    ("yapilir", "yapilmaz"),
    ("gerekir", "gerekmez"),
    ("calisir", "calismaz"),
    ("desteklenir", "desteklenmez"),
    ("gecerlidir", "gecersizdir"),
    ("vardir", "yoktur"),
    ("dogrudur", "yanlistir"),
    ("izinlidir", "yasaktir"),
)


def _contradicts(first: MemoryRecord, second: MemoryRecord) -> bool:
    """Ayni ifadenin olumlu ve olumsuz halini celiski adayi olarak isaretler."""

    left = " ".join(first.content.strip().lower().split())
    right = " ".join(second.content.strip().lower().split())
    if left == right:
        return False
    for positive, negative in _POLARITY_PAIRS:
        if positive in left and negative in right:
            return left.replace(positive, "@") == right.replace(negative, "@")
        if negative in left and positive in right:
            return left.replace(negative, "@") == right.replace(positive, "@")
    return False


# -- Mem0 adapteri ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Mem0Adapter:
    """Opsiyonel self-hosted Mem0 adapteri.

    Adapter yalnizca **kopya** tutar. Work, policy ve run durumu Mem0'ya
    devredilmez; senkron hatasi native kaydi etkilemez.
    """

    engine_ref: str
    push: Callable[[MemoryRecord], str] | None = None

    def sync(self, record: MemoryRecord) -> SyncStatus:
        """Kaydi harici motora gonderir; basarisizlik gorunur kalir."""

        if record.state is not MemoryState.ACTIVE:
            return SyncStatus(
                engine=self.engine_ref,
                state=SyncState.NOT_SYNCED,
                native_digest=record.record_digest,
                detail="yalniz aktif kayit senkronlanir",
            )
        if self.push is None:
            return SyncStatus(
                engine=self.engine_ref,
                state=SyncState.PENDING,
                native_digest=record.record_digest,
                detail="harici motor yapilandirilmamis",
            )
        try:
            external = self.push(record)
        except Exception as exc:
            return SyncStatus(
                engine=self.engine_ref,
                state=SyncState.FAILED,
                native_digest=record.record_digest,
                detail=f"senkron hatasi: {type(exc).__name__}",
            )
        if external != record.record_digest:
            return SyncStatus(
                engine=self.engine_ref,
                state=SyncState.DRIFTED,
                native_digest=record.record_digest,
                external_digest=external,
                detail="harici kayit farkli; native kayit gecerlidir",
            )
        return SyncStatus(
            engine=self.engine_ref,
            state=SyncState.SYNCED,
            native_digest=record.record_digest,
            external_digest=external,
        )

    def resolve(self, status: SyncStatus, native: MemoryRecord) -> MemoryRecord:
        """Catisma cozumu: native kayit her zaman kazanir."""

        if status.native_digest != native.record_digest:
            raise ValidationFailed("durum baska bir kayda ait")
        return native


def observation_digest(content: str, *, author_ref: str) -> str:
    """Ayni gozlemi iki kez saymamak icin kararli anahtar."""

    return digest({"content": content.strip().lower(), "author": author_ref})
