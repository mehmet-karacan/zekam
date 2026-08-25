"""Realm secimi ve oturum baglama.

Uygulama her calismada tek bir realm kapsaminda calisir. Realm kimligi
`zekam.realm_id` oturum ayarina yazilir; bu ayar olmadan row-level security
hicbir satiri gostermez.

Realm arama ve olusturma dar kapsamli `SECURITY DEFINER` fonksiyonlari uzerinden
yapilir; uygulama rolu RLS'i baska hicbir yolla atlayamaz.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.application.diagnostic_trace import RuntimeTraceSink
from zekam.domain.errors import NotFound
from zekam.domain.identifiers import validate_slug
from zekam.domain.realm import DEFAULT_REALM_SLUG, LifecycleStatus, Realm
from zekam.infrastructure.postgres.connection import (
    APPLICATION_ROLE,
    configure_session,
    set_realm_setting,
)


def find_realm_id(connection: Any, slug: str) -> UUID | None:
    """Slug ile realm kimligini arar."""
    validate_slug(slug)
    with connection.cursor() as cursor:
        cursor.execute("select core.find_realm_id(%s)", (slug,))
        row = cursor.fetchone()
    return None if row is None or row[0] is None else UUID(str(row[0]))


def ensure_realm_id(connection: Any, slug: str, display_name: str | None = None) -> UUID:
    """Realm yoksa olusturur, varsa mevcut kimligi dondurur."""
    validate_slug(slug)
    with connection.cursor() as cursor:
        cursor.execute("select core.ensure_realm(%s, %s)", (slug, display_name or slug))
        row = cursor.fetchone()
    if row is None or row[0] is None:  # pragma: no cover - fonksiyon her zaman deger doner
        raise NotFound("Realm olusturulamadi")
    return UUID(str(row[0]))


def load_realm(connection: Any, realm_id: UUID) -> Realm:
    """Kapsam ayarlandiktan sonra realm kaydini okur."""
    with connection.cursor() as cursor:
        cursor.execute(
            "select id, slug, display_name, status, revision, created_at"
            " from core.realm where id = %s",
            (realm_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise NotFound("Realm bulunamadi")
    return Realm(
        id=row[0],
        slug=row[1],
        display_name=row[2],
        status=LifecycleStatus(row[3]),
        revision=row[4],
        created_at=row[5],
    )


@dataclass(frozen=True, slots=True)
class RealmContext:
    """Bir realm kapsamina baglanmis oturum."""

    realm: Realm
    connection: Any
    trace_sink: RuntimeTraceSink | None = None

    @property
    def realm_id(self) -> UUID:
        return self.realm.id


def attach_realm(
    connection: Any,
    *,
    slug: str = DEFAULT_REALM_SLUG,
    display_name: str | None = None,
    create_if_missing: bool = False,
    role: str | None = APPLICATION_ROLE,
) -> RealmContext:
    """Oturumu verilen realm kapsamina baglar.

    `create_if_missing` yalnizca acikca istendiginde realm olusturur; okuma
    komutlari bunu istemez.
    """
    # Once rol ayarlanir: boylece realm arama/olusturma da uygulama rolu altinda,
    # yalnizca SECURITY DEFINER fonksiyonlariyla yapilir.
    configure_session(connection, realm_id=None, role=role)
    realm_id = (
        ensure_realm_id(connection, slug, display_name)
        if create_if_missing
        else find_realm_id(connection, slug)
    )
    if realm_id is None:
        raise NotFound(f"Realm bulunamadi: {slug}")
    set_realm_setting(connection, realm_id)
    return RealmContext(realm=load_realm(connection, realm_id), connection=connection)


def bootstrap_realm(
    connection: Any,
    *,
    slug: str = DEFAULT_REALM_SLUG,
    display_name: str | None = None,
    now: dt.datetime | None = None,
) -> RealmContext:
    """Varsayilan realm'i olusturur ve oturumu ona baglar (idempotent)."""
    del now  # Kimlik veritabaninda uretilir; imza gelecekteki test saati icin korunur.
    return attach_realm(connection, slug=slug, display_name=display_name, create_if_missing=True)
