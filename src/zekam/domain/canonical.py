"""Kanonik JSON gosterimi ve SHA-256 digest.

Zekam'nin butun kimlik, plan, checkpoint, claim, receipt ve kanit zinciri bu
gosterime dayanir. Sozlesme:

- Ayni mantiksal deger her zaman ayni bayt dizisini ve ayni digest'i uretir.
- Sozluk anahtarlari yalnizca `str` olabilir ve kod noktasi sirasina gore siralanir.
- Ayirici bosluk kullanilmaz: `,` ve `:`.
- Metin UTF-8'dir; kacis yalnizca JSON'un zorunlu kildigi yerlerde yapilir.
- `NaN`, `Infinity` ve `-Infinity` reddedilir.
- Naive (timezone'suz) `datetime` reddedilir.
- Desteklenmeyen tip sessizce `str()` ile donusturulmez; acik hata verir.
- Dongusel yapi reddedilir.

Bu kurallar degistirilirse butun mevcut digest'ler gecersiz olur; degisiklik ayri bir
gecis plani ve yeni `CANONICAL_PROFILE` surumu gerektirir.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from decimal import Decimal
from enum import Enum
from typing import Any, Final
from uuid import UUID

from zekam.domain.errors import ValidationFailed

#: Kanonik gosterim profilinin surumu. Kural degisirse artirilir.
CANONICAL_PROFILE: Final = "zekam-canonical-json/v1"

#: Digest algoritmasi.
DIGEST_ALGORITHM: Final = "sha256"

#: Digest degerlerinin onunde kullanilan gosterim oneki.
DIGEST_PREFIX: Final = "sha256:"


def _normalize(value: Any, seen: frozenset[int]) -> Any:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValidationFailed("Kanonik JSON sonlu olmayan float kabul etmez")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValidationFailed("Kanonik JSON sonlu olmayan Decimal kabul etmez")
        # Ondalik degerler kayipsiz olmasi icin metin olarak tasinir.
        return format(value.normalize(), "f")
    if isinstance(value, Enum):
        return _normalize(value.value, seen)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValidationFailed("Kanonik JSON timezone'suz datetime kabul etmez")
        return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, bytes | bytearray):
        raise ValidationFailed("Kanonik JSON ham bayt kabul etmez; once kodlayin")

    identity = id(value)
    if identity in seen:
        raise ValidationFailed("Kanonik JSON dongusel yapi kabul etmez")
    nested = seen | {identity}

    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValidationFailed("Kanonik JSON yalnizca metin anahtar kabul eder")
            if key in normalized:  # pragma: no cover - dict zaten tekil anahtar tutar
                raise ValidationFailed("Kanonik JSON yinelenen anahtar kabul etmez")
            normalized[key] = _normalize(item, nested)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, list | tuple):
        return [_normalize(item, nested) for item in value]
    if isinstance(value, set | frozenset):
        raise ValidationFailed("Kanonik JSON sirasiz kume kabul etmez; once listeye cevirin")

    raise ValidationFailed(f"Kanonik JSON desteklenmeyen tip: {type(value).__name__}")


def canonicalize(value: Any) -> Any:
    """Degeri kanonik JSON'a donusturulebilir hale getirir."""
    return _normalize(value, frozenset())


def canonical_json(value: Any) -> str:
    """Kanonik JSON metnini uretir."""
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    """Kanonik JSON'un UTF-8 baytlarini uretir."""
    return canonical_json(value).encode("utf-8")


def digest(value: Any) -> str:
    """Degerin `sha256:` onekli kanonik digest'ini uretir."""
    return DIGEST_PREFIX + hashlib.sha256(canonical_bytes(value)).hexdigest()


def digest_hex(value: Any) -> str:
    """Degerin oneksiz onaltilik digest'ini uretir."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def digest_of_bytes(payload: bytes) -> str:
    """Ham baytlarin `sha256:` onekli digest'ini uretir."""
    return DIGEST_PREFIX + hashlib.sha256(payload).hexdigest()


def parse_digest(value: str) -> str:
    """Digest metnini dogrular ve onaltilik kismini dondurur."""
    if not value.startswith(DIGEST_PREFIX):
        raise ValidationFailed(f"Digest {DIGEST_PREFIX} oneki tasimali")
    hexadecimal = value[len(DIGEST_PREFIX) :]
    if len(hexadecimal) != 64 or any(char not in "0123456789abcdef" for char in hexadecimal):
        raise ValidationFailed("Digest 64 karakterlik kucuk harf onaltilik olmali")
    return hexadecimal


def digests_match(left: str, right: str) -> bool:
    """Iki digest'i sabit zamanli olmayan fakat dogrulanmis sekilde karsilastirir."""
    return parse_digest(left) == parse_digest(right)
