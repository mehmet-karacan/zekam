"""Realm, actor, revision ve event icin PostgreSQL adapterleri.

Butun sorgular realm kapsamlidir. Kapsam disina cikma girisimi hem uygulama
katmaninda hem row-level security tarafindan reddedilir.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest
from zekam.domain.errors import ConcurrencyConflict, NotFound, PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.realm import Actor, ActorKind, LifecycleStatus, Realm
from zekam.infrastructure.postgres.connection import REALM_SETTING


def _set_realm(connection: Any, realm_id: UUID) -> None:
    with connection.cursor() as cursor:
        cursor.execute("select set_config(%s, %s, false)", (REALM_SETTING, str(realm_id)))


@dataclass(frozen=True, slots=True)
class RealmRepository:
    """Realm kayitlarini yonetir."""

    connection: Any

    def create(self, realm: Realm) -> Realm:
        """Yeni realm ekler. Oturum kapsamini bu realm'e ayarlar."""
        _set_realm(self.connection, realm.id)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into core.realm (id, slug, display_name, status, revision, created_at)"
                " values (%s, %s, %s, %s, %s, %s)",
                (
                    realm.id,
                    realm.slug,
                    realm.display_name,
                    realm.status.value,
                    realm.revision,
                    realm.created_at,
                ),
            )
        return realm

    def get(self, realm_id: UUID) -> Realm:
        """Realm'i kimlige gore okur."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, slug, display_name, status, revision, created_at"
                " from core.realm where id = %s",
                (realm_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Realm bulunamadi")
        return _realm_from_row(row)

    def find_by_slug(self, slug: str) -> Realm | None:
        """Realm'i slug ile arar."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, slug, display_name, status, revision, created_at"
                " from core.realm where slug = %s",
                (slug,),
            )
            row = cursor.fetchone()
        return None if row is None else _realm_from_row(row)

    def count_visible(self) -> int:
        """Oturum kapsaminda gorunen realm sayisi."""
        with self.connection.cursor() as cursor:
            cursor.execute("select count(*) from core.realm")
            return int(cursor.fetchone()[0])


@dataclass(frozen=True, slots=True)
class ActorRepository:
    """Actor kayitlarini yonetir."""

    connection: Any
    realm_id: UUID

    def add(self, actor: Actor) -> Actor:
        """Actor ekler. Cross-realm ekleme reddedilir."""
        if actor.realm_id != self.realm_id:
            raise PolicyViolation("Cross-realm actor ekleme reddedildi")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into core.actor"
                " (id, realm_id, kind, slug, display_name, status, revision, created_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    actor.id,
                    actor.realm_id,
                    actor.kind.value,
                    actor.slug,
                    actor.display_name,
                    actor.status.value,
                    actor.revision,
                    actor.created_at,
                ),
            )
        return actor

    def get(self, actor_id: UUID) -> Actor:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, kind, slug, display_name, status, revision, created_at"
                " from core.actor where id = %s",
                (actor_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Actor bulunamadi")
        return _actor_from_row(row)

    def find_by_slug(self, slug: str) -> Actor | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, kind, slug, display_name, status, revision, created_at"
                " from core.actor where slug = %s",
                (slug,),
            )
            row = cursor.fetchone()
        return None if row is None else _actor_from_row(row)

    def list_all(self) -> tuple[Actor, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, realm_id, kind, slug, display_name, status, revision, created_at"
                " from core.actor order by created_at, id"
            )
            rows = cursor.fetchall()
        return tuple(_actor_from_row(row) for row in rows)


@dataclass(frozen=True, slots=True)
class RevisionRecord:
    """Yazilmis bir revision kaydi."""

    id: UUID
    entity_type: str
    entity_id: UUID
    revision: int
    payload: dict[str, Any]
    payload_digest: str
    previous_digest: str | None
    reason: str
    actor_id: UUID | None
    recorded_at: dt.datetime


@dataclass(frozen=True, slots=True)
class RevisionStore:
    """Append-only revision zincirini yonetir."""

    connection: Any
    realm_id: UUID

    def append(
        self,
        *,
        entity_type: str,
        entity_id: UUID,
        payload: dict[str, Any],
        reason: str,
        actor_id: UUID | None = None,
        expected_revision: int | None = None,
        now: dt.datetime | None = None,
    ) -> RevisionRecord:
        """Yeni revision ekler.

        `expected_revision` verilirse optimistic concurrency uygulanir: baska bir
        yazar araya girmisse `ConcurrencyConflict` yukselir.
        """
        moment = now or dt.datetime.now(dt.UTC)
        latest = self.latest(entity_type=entity_type, entity_id=entity_id)
        current_revision = 0 if latest is None else latest.revision
        if expected_revision is not None and expected_revision != current_revision:
            raise ConcurrencyConflict(
                f"Beklenen revision {expected_revision}, mevcut {current_revision}"
            )
        next_revision = current_revision + 1
        payload_digest = digest(payload)
        previous_digest = None if latest is None else latest.payload_digest
        record_id = new_uuid7(now=moment)

        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into core.revision"
                " (id, realm_id, entity_type, entity_id, revision, payload, payload_digest,"
                "  previous_digest, reason, actor_id, recorded_at)"
                " values (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)",
                (
                    record_id,
                    self.realm_id,
                    entity_type,
                    entity_id,
                    next_revision,
                    _json(payload),
                    payload_digest,
                    previous_digest,
                    reason,
                    actor_id,
                    moment,
                ),
            )
        return RevisionRecord(
            id=record_id,
            entity_type=entity_type,
            entity_id=entity_id,
            revision=next_revision,
            payload=payload,
            payload_digest=payload_digest,
            previous_digest=previous_digest,
            reason=reason,
            actor_id=actor_id,
            recorded_at=moment,
        )

    def latest(self, *, entity_type: str, entity_id: UUID) -> RevisionRecord | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, entity_type, entity_id, revision, payload, payload_digest,"
                " previous_digest, reason, actor_id, recorded_at"
                " from core.revision"
                " where entity_type = %s and entity_id = %s"
                " order by revision desc limit 1",
                (entity_type, entity_id),
            )
            row = cursor.fetchone()
        return None if row is None else _revision_from_row(row)

    def history(self, *, entity_type: str, entity_id: UUID) -> tuple[RevisionRecord, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, entity_type, entity_id, revision, payload, payload_digest,"
                " previous_digest, reason, actor_id, recorded_at"
                " from core.revision"
                " where entity_type = %s and entity_id = %s"
                " order by revision",
                (entity_type, entity_id),
            )
            rows = cursor.fetchall()
        return tuple(_revision_from_row(row) for row in rows)

    def verify_chain(self, *, entity_type: str, entity_id: UUID) -> bool:
        """Zincirin kopuk olmadigini bagimsiz olarak dogrular."""
        records = self.history(entity_type=entity_type, entity_id=entity_id)
        previous: RevisionRecord | None = None
        for index, record in enumerate(records, start=1):
            if record.revision != index:
                return False
            if digest(record.payload) != record.payload_digest:
                return False
            expected_previous = None if previous is None else previous.payload_digest
            if record.previous_digest != expected_previous:
                return False
            previous = record
        return True


@dataclass(frozen=True, slots=True)
class EventRecord:
    """Yazilmis bir olay kaydi."""

    id: UUID
    sequence: int
    event_type: str
    entity_type: str
    entity_id: UUID
    revision_id: UUID | None
    payload: dict[str, Any]
    payload_digest: str
    correlation_id: UUID | None
    causation_id: UUID | None
    actor_id: UUID | None
    occurred_at: dt.datetime
    recorded_at: dt.datetime


@dataclass(frozen=True, slots=True)
class EventStore:
    """Append-only olay kaydini yonetir."""

    connection: Any
    realm_id: UUID

    def append(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: UUID,
        payload: dict[str, Any] | None = None,
        revision_id: UUID | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        actor_id: UUID | None = None,
        occurred_at: dt.datetime | None = None,
    ) -> EventRecord:
        """Yeni olay ekler."""
        moment = occurred_at or dt.datetime.now(dt.UTC)
        body = payload or {}
        payload_digest = digest(body)
        record_id = new_uuid7(now=moment)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into core.event"
                " (id, realm_id, event_type, entity_type, entity_id, revision_id, payload,"
                "  payload_digest, correlation_id, causation_id, actor_id, occurred_at)"
                " values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)"
                " returning sequence, recorded_at",
                (
                    record_id,
                    self.realm_id,
                    event_type,
                    entity_type,
                    entity_id,
                    revision_id,
                    _json(body),
                    payload_digest,
                    correlation_id,
                    causation_id,
                    actor_id,
                    moment,
                ),
            )
            sequence, recorded_at = cursor.fetchone()
        return EventRecord(
            id=record_id,
            sequence=int(sequence),
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            revision_id=revision_id,
            payload=body,
            payload_digest=payload_digest,
            correlation_id=correlation_id,
            causation_id=causation_id,
            actor_id=actor_id,
            occurred_at=moment,
            recorded_at=recorded_at,
        )

    def stream(self, *, after_sequence: int = 0, limit: int = 100) -> tuple[EventRecord, ...]:
        """Sirali olay akisini okur."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, sequence, event_type, entity_type, entity_id, revision_id, payload,"
                " payload_digest, correlation_id, causation_id, actor_id, occurred_at, recorded_at"
                " from core.event where sequence > %s order by sequence limit %s",
                (after_sequence, limit),
            )
            rows = cursor.fetchall()
        return tuple(_event_from_row(row) for row in rows)

    def for_entity(self, *, entity_type: str, entity_id: UUID) -> tuple[EventRecord, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, sequence, event_type, entity_type, entity_id, revision_id, payload,"
                " payload_digest, correlation_id, causation_id, actor_id, occurred_at, recorded_at"
                " from core.event where entity_type = %s and entity_id = %s order by sequence",
                (entity_type, entity_id),
            )
            rows = cursor.fetchall()
        return tuple(_event_from_row(row) for row in rows)


def _json(payload: dict[str, Any]) -> str:
    from zekam.domain.canonical import canonical_json

    return canonical_json(payload)


def _realm_from_row(row: tuple[Any, ...]) -> Realm:
    return Realm(
        id=row[0],
        slug=row[1],
        display_name=row[2],
        status=LifecycleStatus(row[3]),
        revision=row[4],
        created_at=row[5],
    )


def _actor_from_row(row: tuple[Any, ...]) -> Actor:
    return Actor(
        id=row[0],
        realm_id=row[1],
        kind=ActorKind(row[2]),
        slug=row[3],
        display_name=row[4],
        status=LifecycleStatus(row[5]),
        revision=row[6],
        created_at=row[7],
    )


def _revision_from_row(row: tuple[Any, ...]) -> RevisionRecord:
    return RevisionRecord(
        id=row[0],
        entity_type=row[1],
        entity_id=row[2],
        revision=row[3],
        payload=row[4],
        payload_digest=row[5],
        previous_digest=row[6],
        reason=row[7],
        actor_id=row[8],
        recorded_at=row[9],
    )


def _event_from_row(row: tuple[Any, ...]) -> EventRecord:
    return EventRecord(
        id=row[0],
        sequence=int(row[1]),
        event_type=row[2],
        entity_type=row[3],
        entity_id=row[4],
        revision_id=row[5],
        payload=row[6],
        payload_digest=row[7],
        correlation_id=row[8],
        causation_id=row[9],
        actor_id=row[10],
        occurred_at=row[11],
        recorded_at=row[12],
    )
