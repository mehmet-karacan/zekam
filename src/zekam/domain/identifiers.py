"""Kimlik uretimi ve dogrulama.

Zekam kimlikleri iki sinifa ayrilir:

- **Teknik kimlik**: zaman siralamali UUIDv7. Kayitlar arasi referans icin kullanilir.
- **Portable kimlik**: insan okunur, makineye bagli olmayan slug. Proje ve alias
  cozumlemesi bunun uzerinden yapilir.

Portable kayitlarda absolute path, kullanici adi veya makine adi bulunamaz.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import secrets
from typing import Final
from uuid import UUID

from zekam.domain.errors import ValidationFailed

#: Portable slug bicimi: kucuk harf, rakam ve tek tire.
SLUG_PATTERN: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

SLUG_MIN_LENGTH: Final = 2
SLUG_MAX_LENGTH: Final = 64

#: Slug icinde gorunmesi yasak, makineye ozel oldugunu gosteren desenler.
_NON_PORTABLE_PATTERNS: Final = (
    re.compile(r"^[a-z]:"),  # surucu harfi
    re.compile(r"^/"),  # mutlak posix yolu
    re.compile(r"\\"),  # ters bolu
)


def new_uuid7(*, now: dt.datetime | None = None) -> UUID:
    """RFC 9562 UUIDv7 uretir (48 bit unix milisaniye + rastgele).

    Zaman siralamali oldugu icin PostgreSQL indekslerinde b-tree parcalanmasini
    azaltir.
    """
    moment = now or dt.datetime.now(dt.UTC)
    if moment.tzinfo is None:
        raise ValidationFailed("UUIDv7 icin timezone'lu zaman gerekir")
    milliseconds = int(moment.timestamp() * 1000)
    if not 0 <= milliseconds < 1 << 48:
        raise ValidationFailed("Zaman damgasi UUIDv7 araligi disinda")

    random_bytes = secrets.token_bytes(10)
    value = bytearray(milliseconds.to_bytes(6, "big") + random_bytes)
    value[6] = (value[6] & 0x0F) | 0x70  # surum 7
    value[8] = (value[8] & 0x3F) | 0x80  # varyant RFC 4122
    return UUID(bytes=bytes(value))


def uuid7_timestamp(value: UUID) -> dt.datetime:
    """UUIDv7 icindeki zaman damgasini cozer."""
    if value.version != 7:
        raise ValidationFailed("Yalnizca UUIDv7 zaman damgasi tasir")
    milliseconds = int.from_bytes(value.bytes[:6], "big")
    return dt.datetime.fromtimestamp(milliseconds / 1000, tz=dt.UTC)


def normalize_slug(value: str) -> str:
    """Serbest metinden portable slug uretir."""
    lowered = value.strip().lower()
    replaced = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    collapsed = re.sub(r"-{2,}", "-", replaced)
    if not collapsed:
        raise ValidationFailed("Slug uretilemedi")
    return collapsed[:SLUG_MAX_LENGTH].rstrip("-")


def validate_slug(value: str) -> str:
    """Slug bicimini ve portable olmasini dogrular."""
    if not SLUG_MIN_LENGTH <= len(value) <= SLUG_MAX_LENGTH:
        raise ValidationFailed(
            f"Slug uzunlugu {SLUG_MIN_LENGTH}-{SLUG_MAX_LENGTH} araliginda olmali"
        )
    if not SLUG_PATTERN.match(value):
        raise ValidationFailed("Slug yalnizca kucuk harf, rakam ve tek tire icerebilir")
    for pattern in _NON_PORTABLE_PATTERNS:
        if pattern.search(value):
            raise ValidationFailed("Slug makineye ozel bir yol parcasi icseremez")
    return value


def assert_portable(value: str) -> str:
    """Portable kayitlara yazilacak metni dogrular.

    Absolute path, surucu harfi, kullanici ana dizini veya makine adi iceren
    degerler reddedilir.
    """
    candidate = value.strip()
    if not candidate:
        raise ValidationFailed("Portable deger bos olamaz")
    lowered = candidate.lower().replace("\\", "/")
    if re.match(r"^[a-z]:/", lowered) or lowered.startswith("/") or lowered.startswith("~"):
        raise ValidationFailed("Portable kayit mutlak yol tasiyamaz")
    home = os.path.expanduser("~").lower().replace("\\", "/")
    if home and home in lowered:
        raise ValidationFailed("Portable kayit kullanici ana dizinini tasiyamaz")
    return candidate
