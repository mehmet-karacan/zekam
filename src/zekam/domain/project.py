"""Proje kayit defteri alan modeli.

Kurallar:

- Proje kimligi portable'dir: makineye ozel yol, kullanici adi veya surucu harfi
  tasimaz.
- Harici kaynak agaci Zekam'nin sahibi degildir; baglanti daima `read-only`'dir.
- Source revision gozlemi append-only'dir; guncelleme yerine yeni gozlem eklenir.
- Entegrasyon asamasi kanit olmadan ilerletilemez.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import parse_digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.identifiers import assert_portable, new_uuid7, normalize_slug, validate_slug
from zekam.domain.realm import LifecycleStatus, Realm


class SourceBindingKind(StrEnum):
    """Baglanan kaynagin turu."""

    GIT_REPOSITORY = "git-repository"
    DIRECTORY = "directory"


class BindingStatus(StrEnum):
    """Baglanti durumu."""

    BOUND = "bound"
    UNBOUND = "unbound"
    STALE = "stale"


class SourceRevisionKind(StrEnum):
    """Gozlemlenen surumun turu."""

    GIT_COMMIT = "git-commit"
    TREE_DIGEST = "tree-digest"


class IntegrationStage(StrEnum):
    """Entegrasyonun ilerleme asamasi.

    Asamalar sirayla ilerler; her ilerleme kendi kanitini ister.
    """

    REGISTERED = "registered"
    BOUND = "bound"
    DISCOVERED = "discovered"
    PROFILED = "profiled"
    CURRENT = "current"
    STALE = "stale"
    UNBOUND = "unbound"


#: Ilerleme sirasi. `stale` ve `unbound` bu siranin disinda, geri donus durumlaridir.
STAGE_ORDER: tuple[IntegrationStage, ...] = (
    IntegrationStage.REGISTERED,
    IntegrationStage.BOUND,
    IntegrationStage.DISCOVERED,
    IntegrationStage.PROFILED,
    IntegrationStage.CURRENT,
)


class ResolutionKind(StrEnum):
    """Dogal dil proje cozumlemesinin sonucu."""

    EXACT_ID = "exact-id"
    EXACT_SLUG = "exact-slug"
    EXACT_ALIAS = "exact-alias"
    FUZZY = "fuzzy"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not-found"


@dataclass(frozen=True, slots=True)
class Project:
    """Kayitli proje."""

    id: UUID
    realm_id: UUID
    slug: str
    display_name: str
    created_at: dt.datetime
    revision: int = 1
    status: LifecycleStatus = LifecycleStatus.ACTIVE

    def __post_init__(self) -> None:
        validate_slug(self.slug)
        if not self.display_name.strip():
            raise ValidationFailed("Proje gorunen adi bos olamaz")
        if self.revision < 1:
            raise ValidationFailed("Revision 1'den kucuk olamaz")
        _require_aware(self.created_at, "Project created_at")

    @classmethod
    def create(
        cls,
        *,
        realm: Realm,
        slug: str,
        display_name: str | None = None,
        now: dt.datetime | None = None,
    ) -> Project:
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=realm.id,
            slug=slug,
            display_name=display_name or slug,
            created_at=moment,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "slug": self.slug,
            "display_name": self.display_name,
            "status": self.status.value,
            "revision": self.revision,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class ProjectAlias:
    """Proje takma adi."""

    id: UUID
    realm_id: UUID
    project_id: UUID
    alias: str
    normalized: str
    is_primary: bool = False
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        if not self.alias.strip():
            raise ValidationFailed("Alias bos olamaz")
        validate_slug(self.normalized)

    @classmethod
    def create(
        cls,
        *,
        project: Project,
        alias: str,
        is_primary: bool = False,
        now: dt.datetime | None = None,
    ) -> ProjectAlias:
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=project.realm_id,
            project_id=project.id,
            alias=alias.strip(),
            normalized=normalize_slug(alias),
            is_primary=is_primary,
            created_at=moment,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "alias": self.alias,
            "normalized": self.normalized,
            "is_primary": self.is_primary,
        }


@dataclass(frozen=True, slots=True)
class SourceBinding:
    """Harici kaynak agacina salt okunur baglanti.

    `root_label` portable bir etikettir (ornegin dizin adi). Fiziksel yol kanonik
    kayitta tutulmaz; yalnizca makineye ozel yerel kayitta bulunur.
    """

    id: UUID
    realm_id: UUID
    project_id: UUID
    kind: SourceBindingKind
    root_label: str
    locator_digest: str
    status: BindingStatus = BindingStatus.BOUND
    access_mode: str = "read-only"
    created_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        assert_portable(self.root_label)
        parse_digest(self.locator_digest)
        if self.access_mode != "read-only":
            raise ValidationFailed("Harici kaynak baglantisi yalnizca read-only olabilir")

    @property
    def is_usable(self) -> bool:
        return self.status is BindingStatus.BOUND

    def unbound(self) -> SourceBinding:
        """Export icin fiziksel baglantisi kopmus kopyayi dondurur."""
        return SourceBinding(
            id=self.id,
            realm_id=self.realm_id,
            project_id=self.project_id,
            kind=self.kind,
            root_label=self.root_label,
            locator_digest=self.locator_digest,
            status=BindingStatus.UNBOUND,
            access_mode=self.access_mode,
            created_at=self.created_at,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "project_id": str(self.project_id),
            "kind": self.kind.value,
            "root_label": self.root_label,
            "locator_digest": self.locator_digest,
            "status": self.status.value,
            "access_mode": self.access_mode,
        }


@dataclass(frozen=True, slots=True)
class SourceRevision:
    """Bir anda gozlemlenen kaynak surumu."""

    id: UUID
    realm_id: UUID
    binding_id: UUID
    kind: SourceRevisionKind
    revision: str
    tree_digest: str
    branch: str | None = None
    is_dirty: bool = False
    file_count: int = 0
    observed_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        if not self.revision.strip():
            raise ValidationFailed("Revision bos olamaz")
        parse_digest(self.tree_digest)
        if self.file_count < 0:
            raise ValidationFailed("Dosya sayisi negatif olamaz")

    def matches(self, other: SourceRevision) -> bool:
        """Iki gozlemin ayni kaynak durumunu gosterdigini soyler."""
        return self.revision == other.revision and self.tree_digest == other.tree_digest

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "binding_id": str(self.binding_id),
            "kind": self.kind.value,
            "revision": self.revision,
            "tree_digest": self.tree_digest,
            "branch": self.branch,
            "is_dirty": self.is_dirty,
            "file_count": self.file_count,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class ProjectCandidate:
    """Cozumleme sirasinda bulunan aday."""

    project_id: UUID
    slug: str
    display_name: str
    matched_on: str
    score: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": str(self.project_id),
            "slug": self.slug,
            "display_name": self.display_name,
            "matched_on": self.matched_on,
            "score": round(self.score, 4),
        }


@dataclass(frozen=True, slots=True)
class ProjectResolution:
    """Dogal dil proje cozumlemesinin sonucu.

    `resolved` yalnizca tek ve yeterince guclu bir aday varsa doludur. Belirsizlikte
    mutation yapilmaz; kullanici secim yapar.
    """

    query: str
    kind: ResolutionKind
    resolved: ProjectCandidate | None = None
    candidates: tuple[ProjectCandidate, ...] = ()

    @property
    def is_resolved(self) -> bool:
        return self.resolved is not None

    @property
    def requires_user_choice(self) -> bool:
        return self.kind is ResolutionKind.AMBIGUOUS

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "kind": self.kind.value,
            "resolved": None if self.resolved is None else self.resolved.as_dict(),
            "candidates": [candidate.as_dict() for candidate in self.candidates],
        }


def _require_aware(value: dt.datetime, label: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValidationFailed(f"{label} timezone bilgisi tasimali")
