"""MemoryEngine portu ve bellek sozlesmesi.

Bellek **authority degildir**: Work, policy ve run durumunu sahiplenemez. Ham
model ciktisi dogrudan aktif bilgi olamaz; aday olusur, bagimsiz review'dan
gecer ve ancak sonra aktiflesir. Mevcut bilgi sessizce ezilmez; supersession
iliskisi kurulur.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

MAX_MEMORY_CHARS = 4000
DEFAULT_RETENTION_DAYS = 365

_SENSITIVE = re.compile(
    r"(?:secret|credential|password|parola|api[-_ ]?key|private[-_ ]?key|"
    r"owner[-_ ]?token|bearer\s+[A-Za-z0-9._-]{8,})",
    re.IGNORECASE,
)


class MemoryClass(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    PREFERENCE = "preference"
    FAILURE = "failure"


#: Bu siniflar ham model cumlesinden dogrudan uretilemez; review sarttir.
REVIEW_REQUIRED_CLASSES = frozenset(
    {MemoryClass.SEMANTIC, MemoryClass.PROCEDURAL, MemoryClass.FAILURE}
)


class MemoryScope(StrEnum):
    GLOBAL_USER = "global-user"
    PROJECT = "project"
    WORK_ITEM = "work-item"
    RUN = "run"
    AGENT = "agent"


#: Agent scratchpad kalici bellek degildir; retrieval'a girmez.
EPHEMERAL_SCOPES = frozenset({MemoryScope.AGENT, MemoryScope.RUN})


class MemoryState(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    ARCHIVED = "archived"


class HygieneFinding(StrEnum):
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    STALE = "stale"
    UNUSED = "unused"
    RETENTION_REVIEW = "retention-review"
    SOURCE_VERSION_CONFLICT = "source-version-conflict"


class SyncState(StrEnum):
    NOT_SYNCED = "not-synced"
    PENDING = "pending"
    SYNCED = "synced"
    DRIFTED = "drifted"
    FAILED = "failed"


def _reject_sensitive(value: str, label: str) -> None:
    if _SENSITIVE.search(value):
        raise PolicyViolation(f"{label} secret benzeri deger tasiyamaz")


@dataclass(frozen=True, slots=True)
class MemoryEvidence:
    """Bir bellek kaydinin dayandigi kanit referansi."""

    kind: str
    reference: str
    digest_value: str

    def __post_init__(self) -> None:
        parse_digest(self.digest_value)
        if self.kind not in {"run", "receipt", "test", "citation", "work", "observation"}:
            raise ValidationFailed("kanit turu taninmiyor")
        if not self.reference.strip():
            raise ValidationFailed("kanit referansi bos olamaz")

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "reference": self.reference, "digest": self.digest_value}


@dataclass(frozen=True, slots=True)
class MemoryKey:
    """Bellek kaydinin kapsam kimligi. Scope disi erisim yoktur."""

    scope: MemoryScope
    realm_ref: str
    project_ref: str | None = None
    work_ref: str | None = None
    run_ref: str | None = None
    agent_ref: str | None = None

    def __post_init__(self) -> None:
        required: dict[MemoryScope, str | None] = {
            MemoryScope.PROJECT: self.project_ref,
            MemoryScope.WORK_ITEM: self.work_ref,
            MemoryScope.RUN: self.run_ref,
            MemoryScope.AGENT: self.agent_ref,
        }
        needed = required.get(self.scope)
        if self.scope is not MemoryScope.GLOBAL_USER and not needed:
            raise ValidationFailed(f"{self.scope} kapsami kendi referansini ister")
        if not self.realm_ref.strip():
            raise ValidationFailed("realm referansi bos olamaz")

    @property
    def is_ephemeral(self) -> bool:
        return self.scope in EPHEMERAL_SCOPES

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": str(self.scope),
            "realm_ref": self.realm_ref,
            "project_ref": self.project_ref,
            "work_ref": self.work_ref,
            "run_ref": self.run_ref,
            "agent_ref": self.agent_ref,
        }


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """Tek bellek kaydi. Authority tasimaz ve secret icermez."""

    memory_id: str
    key: MemoryKey
    memory_class: MemoryClass
    content: str
    state: MemoryState
    revision: int
    created_at: dt.datetime
    evidence: tuple[MemoryEvidence, ...] = ()
    entities: tuple[str, ...] = ()
    valid_from: dt.datetime | None = None
    valid_until: dt.datetime | None = None
    reviewed_by: str | None = None
    author_ref: str | None = None
    superseded_by: str | None = None
    last_used_at: dt.datetime | None = None
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if self.grants_authority:
            raise PolicyViolation("bellek kaydi authority veremez")
        if not self.content.strip():
            raise ValidationFailed("bellek icerigi bos olamaz")
        if len(self.content) > MAX_MEMORY_CHARS:
            raise ValidationFailed("bellek icerigi bounded sinirini asiyor")
        _reject_sensitive(self.content, "bellek icerigi")
        if self.revision < 1:
            raise ValidationFailed("revision 1'den kucuk olamaz")
        if self.created_at.tzinfo is None:
            raise ValidationFailed("zaman damgasi timezone-aware olmali")
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValidationFailed("gecerlilik araligi gecersiz")
        if self.state is MemoryState.SUPERSEDED and self.superseded_by is None:
            raise ValidationFailed("superseded kayit halefini bildirmeli")
        if self.state is MemoryState.ACTIVE:
            self._assert_promotable()

    def _assert_promotable(self) -> None:
        """Aktif kayit icin kanit ve gerekiyorsa bagimsiz review sarttir."""

        if not self.evidence:
            raise PolicyViolation("kanitsiz bellek aktif olamaz")
        if self.memory_class in REVIEW_REQUIRED_CLASSES:
            if self.reviewed_by is None:
                raise PolicyViolation("bu bellek sinifi bagimsiz review ister")
            if self.reviewed_by == self.author_ref:
                raise PolicyViolation("review yazarla ayni kimlik olamaz")

    def is_valid_at(self, moment: dt.datetime) -> bool:
        if self.valid_from is not None and moment < self.valid_from:
            return False
        return not (self.valid_until is not None and moment >= self.valid_until)

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-memory-record/v1",
            "memory_id": self.memory_id,
            "key": self.key.as_dict(),
            "memory_class": str(self.memory_class),
            "content": self.content,
            "state": str(self.state),
            "revision": self.revision,
            "evidence": [item.as_dict() for item in self.evidence],
            "entities": list(self.entities),
            "reviewed_by": self.reviewed_by,
            "author_ref": self.author_ref,
            "superseded_by": self.superseded_by,
            "grants_authority": False,
        }

    @property
    def record_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """Ham gozlemden turetilen aday. Dogrudan aktif olamaz."""

    candidate_id: str
    key: MemoryKey
    memory_class: MemoryClass
    content: str
    author_ref: str
    observed_at: dt.datetime
    evidence: tuple[MemoryEvidence, ...] = ()
    occurrence_key: str | None = None
    observation_count: int = 1

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValidationFailed("aday icerigi bos olamaz")
        _reject_sensitive(self.content, "aday icerigi")
        if self.observation_count < 1:
            raise ValidationFailed("gozlem sayisi pozitif olmali")
        if self.memory_class is MemoryClass.FAILURE and self.occurrence_key is None:
            raise ValidationFailed("failure adayi occurrence key ister")

    def promote(
        self,
        *,
        memory_id: str,
        reviewed_by: str | None,
        now: dt.datetime,
        revision: int = 1,
    ) -> MemoryRecord:
        """Adayi aktif kayda yukseltir. Kapilari `MemoryRecord` zorlar."""

        if self.key.is_ephemeral:
            raise PolicyViolation("gecici kapsam kalici bellek uretemez")
        return MemoryRecord(
            memory_id=memory_id,
            key=self.key,
            memory_class=self.memory_class,
            content=self.content,
            state=MemoryState.ACTIVE,
            revision=revision,
            created_at=now,
            evidence=self.evidence,
            reviewed_by=reviewed_by,
            author_ref=self.author_ref,
            valid_from=now,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "key": self.key.as_dict(),
            "memory_class": str(self.memory_class),
            "content": self.content,
            "author_ref": self.author_ref,
            "evidence": [item.as_dict() for item in self.evidence],
            "occurrence_key": self.occurrence_key,
            "observation_count": self.observation_count,
        }


def supersede(
    current: MemoryRecord, replacement_content: str, *, memory_id: str, now: dt.datetime
) -> tuple[MemoryRecord, MemoryRecord]:
    """Mevcut bilgiyi ezmeden yeni revision ve supersession iliskisi kurar."""

    if current.state is not MemoryState.ACTIVE:
        raise PolicyViolation("yalniz aktif kayit superseded olur")
    if current.valid_from is not None and now <= current.valid_from:
        # Sifir uzunlukta gecerlilik araligi anlamsizdir ve temporal sorguyu bozar.
        raise ValidationFailed("supersede ani kaydin gecerlilik baslangicindan sonra olmali")
    successor = MemoryRecord(
        memory_id=memory_id,
        key=current.key,
        memory_class=current.memory_class,
        content=replacement_content,
        state=MemoryState.ACTIVE,
        revision=current.revision + 1,
        created_at=now,
        evidence=current.evidence,
        entities=current.entities,
        reviewed_by=current.reviewed_by,
        author_ref=current.author_ref,
        valid_from=now,
    )
    retired = MemoryRecord(
        memory_id=current.memory_id,
        key=current.key,
        memory_class=current.memory_class,
        content=current.content,
        state=MemoryState.SUPERSEDED,
        revision=current.revision,
        created_at=current.created_at,
        evidence=current.evidence,
        entities=current.entities,
        reviewed_by=current.reviewed_by,
        author_ref=current.author_ref,
        valid_from=current.valid_from,
        valid_until=now,
        superseded_by=memory_id,
    )
    return retired, successor


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """Bellek aramasi. Cross-project sonuc acik izin olmadan gelmez."""

    text: str
    key: MemoryKey
    classes: frozenset[MemoryClass] = frozenset()
    entities: tuple[str, ...] = ()
    at: dt.datetime | None = None
    allow_cross_project: bool = False
    limit: int = 10

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValidationFailed("limit pozitif olmali")

    def permits(self, record: MemoryRecord) -> bool:
        """Kapsam izolasyonu: baska proje acik izin olmadan gorunmez."""

        if record.key.realm_ref != self.key.realm_ref:
            return False
        if self.classes and record.memory_class not in self.classes:
            return False
        if record.key.is_ephemeral:
            return False
        if record.key.scope is MemoryScope.PROJECT:
            return self.allow_cross_project or record.key.project_ref == self.key.project_ref
        if record.key.scope is MemoryScope.WORK_ITEM:
            # Cross-project izni proje bellegini genisletebilir; baska bir isin
            # ozel bellegine gecis yetkisi vermez.
            return (
                self.key.scope is MemoryScope.WORK_ITEM
                and record.key.project_ref == self.key.project_ref
                and record.key.work_ref == self.key.work_ref
            )
        return True


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """Aramada donen kayit ve secim gerekcesi."""

    record: MemoryRecord
    score: float
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.reasons:
            raise ValidationFailed("secim gerekcesiz sonuc dondurulemez")

    def as_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.record.memory_id,
            "score": round(self.score, 6),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class HygieneReport:
    """Salt okunur hijyen raporu. Otomatik silme yapmaz."""

    findings: tuple[tuple[HygieneFinding, str, str], ...]
    scanned: int
    deleted: int = 0

    def __post_init__(self) -> None:
        if self.deleted:
            raise PolicyViolation("hijyen otomatik silme yapamaz")

    def of_kind(self, kind: HygieneFinding) -> tuple[str, ...]:
        return tuple(memory_id for finding, memory_id, _ in self.findings if finding is kind)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "deleted": 0,
            "findings": [
                {"kind": str(kind), "memory_id": memory_id, "detail": detail}
                for kind, memory_id, detail in self.findings
            ],
        }


@dataclass(frozen=True, slots=True)
class SyncStatus:
    """Harici bellek adaptorunun durumu. Native kayit her zaman otoritedir."""

    engine: str
    state: SyncState
    native_digest: str
    external_digest: str | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        parse_digest(self.native_digest)
        if self.state is SyncState.SYNCED and self.external_digest != self.native_digest:
            raise ValidationFailed("synced durum digest esitligi ister")
        if self.state is SyncState.DRIFTED and self.external_digest == self.native_digest:
            raise ValidationFailed("drift durumu farkli digest ister")

    @property
    def authority(self) -> str:
        """Harici motor ne derse desin otorite native kayittir."""

        return "native"

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "state": str(self.state),
            "native_digest": self.native_digest,
            "external_digest": self.external_digest,
            "authority": "native",
            "detail": self.detail,
        }


class MemoryEngine(Protocol):
    """Bellek portu. Native uygulama kanoniktir; Mem0 opsiyonel adapterdir."""

    def write(self, candidate: MemoryCandidate, *, now: dt.datetime) -> MemoryRecord: ...

    def search(self, query: MemoryQuery) -> tuple[MemoryHit, ...]: ...

    def hygiene(self, key: MemoryKey, *, now: dt.datetime) -> HygieneReport: ...


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Saklama suresi. Sure dolunca silinmez, review'a alinir."""

    days: int = DEFAULT_RETENTION_DAYS

    def __post_init__(self) -> None:
        if self.days <= 0:
            raise ValidationFailed("saklama suresi pozitif olmali")

    def needs_review(self, record: MemoryRecord, *, now: dt.datetime) -> bool:
        age = (now - record.created_at).days
        return age >= self.days
