"""Realm, actor ve calisma kimligi modelleri.

Realm en dis yalitim sinriridir: bir realm icindeki kayit baska bir realm'in kaydina
referans veremez. Bu kural hem alan modelinde hem de veritabani constraint ve RLS
politikasinda ayri ayri uygulanir.

Actor, bir eylemi kimin baslattigini soyler; authority vermez. Yetki her zaman ayri
authorization kaydindan gelir.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.identifiers import new_uuid7, validate_slug

#: Varsayilan realm slug'i. Tek kullanicili kurulumda bu realm olusturulur.
DEFAULT_REALM_SLUG = "yerel"


class LifecycleStatus(StrEnum):
    """Kimlik kayitlarinin yasam dongusu."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


class ActorKind(StrEnum):
    """Eylemi baslatan tarafin turu."""

    HUMAN = "human"
    AGENT = "agent"
    SERVICE = "service"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class Realm:
    """En dis yalitim sinri."""

    id: UUID
    slug: str
    display_name: str
    created_at: dt.datetime
    revision: int = 1
    status: LifecycleStatus = LifecycleStatus.ACTIVE

    def __post_init__(self) -> None:
        validate_slug(self.slug)
        if not self.display_name.strip():
            raise ValidationFailed("Realm gorunen adi bos olamaz")
        if self.revision < 1:
            raise ValidationFailed("Revision 1'den kucuk olamaz")
        _require_aware(self.created_at, "Realm created_at")

    @classmethod
    def create(
        cls,
        *,
        slug: str = DEFAULT_REALM_SLUG,
        display_name: str | None = None,
        now: dt.datetime | None = None,
    ) -> Realm:
        """Yeni realm uretir."""
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            slug=slug,
            display_name=display_name or slug,
            created_at=moment,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "slug": self.slug,
            "display_name": self.display_name,
            "created_at": self.created_at,
            "revision": self.revision,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class Actor:
    """Realm icinde bir eylemi baslatabilen taraf."""

    id: UUID
    realm_id: UUID
    kind: ActorKind
    slug: str
    display_name: str
    created_at: dt.datetime
    revision: int = 1
    status: LifecycleStatus = LifecycleStatus.ACTIVE

    def __post_init__(self) -> None:
        validate_slug(self.slug)
        if not self.display_name.strip():
            raise ValidationFailed("Actor gorunen adi bos olamaz")
        if self.revision < 1:
            raise ValidationFailed("Revision 1'den kucuk olamaz")
        _require_aware(self.created_at, "Actor created_at")

    @classmethod
    def create(
        cls,
        *,
        realm: Realm,
        kind: ActorKind,
        slug: str,
        display_name: str | None = None,
        now: dt.datetime | None = None,
    ) -> Actor:
        """Verilen realm icinde yeni actor uretir."""
        moment = now or dt.datetime.now(dt.UTC)
        return cls(
            id=new_uuid7(now=moment),
            realm_id=realm.id,
            kind=kind,
            slug=slug,
            display_name=display_name or slug,
            created_at=moment,
        )

    @property
    def is_active(self) -> bool:
        return self.status is LifecycleStatus.ACTIVE

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "kind": self.kind.value,
            "slug": self.slug,
            "display_name": self.display_name,
            "created_at": self.created_at,
            "revision": self.revision,
            "status": self.status.value,
        }


@dataclass(frozen=True, slots=True)
class ClientIdentity:
    """Isteği tasiyan istemci (CLI, API, Codex, Claude Code, OpenCode ...).

    Istemci yetenegi beyan eder; bu beyan authority degildir ve dogrulanmadan
    yetenek varsayilmaz.
    """

    name: str
    version: str
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        validate_slug(self.name)
        if not self.version.strip():
            raise ValidationFailed("Istemci surumu bos olamaz")

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "capabilities": sorted(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class ExecutionIdentity:
    """Bir run/step'i fiilen yuruten kimlik.

    Lease, fencing ve receipt eslesmeleri bu kimlige baglanir. Kimlik yetki tasimaz.
    """

    realm_id: UUID
    actor_id: UUID
    client: ClientIdentity
    process_label: str
    started_at: dt.datetime
    attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.process_label.strip():
            raise ValidationFailed("Process etiketi bos olamaz")
        _require_aware(self.started_at, "ExecutionIdentity started_at")

    def as_dict(self) -> dict[str, Any]:
        return {
            "realm_id": str(self.realm_id),
            "actor_id": str(self.actor_id),
            "client": self.client.as_dict(),
            "process_label": self.process_label,
            "started_at": self.started_at,
            "attributes": dict(sorted(self.attributes.items())),
        }


def _require_aware(value: dt.datetime, label: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValidationFailed(f"{label} timezone bilgisi tasimali")


def realm_of(entity: Realm | Actor | ExecutionIdentity) -> UUID:
    """Verilen kaydin realm kimligini dondurur."""
    if isinstance(entity, Realm):
        return entity.id
    return entity.realm_id


def assert_same_realm(*entities: Realm | Actor | ExecutionIdentity) -> UUID:
    """Butun kayitlarin ayni realm'e ait oldugunu dogrular."""
    if not entities:
        raise ValidationFailed("En az bir kayit gerekir")
    identifiers = {realm_of(entity) for entity in entities}
    if len(identifiers) > 1:
        raise PolicyViolation("Cross-realm iliski reddedildi")
    return identifiers.pop()


def active_actors(actors: Iterable[Actor]) -> tuple[Actor, ...]:
    """Yalnizca aktif actor'lari dondurur."""
    return tuple(actor for actor in actors if actor.is_active)
