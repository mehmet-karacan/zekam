"""Nesne deposu portu.

Artifact'lar icerik adresli tutulur: adres, icerigin sha256 digest'idir. Bu sayede

- ayni icerik iki kez saklanmaz,
- kayit degistirilirse adres degisir (immutability),
- yedek ve restore dogrulamasi digest karsilastirmasina indirgenir.

Silme islemi port sozlesmesinde vardir fakat uygulama katmaninda exact authorization
olmadan cagrilmaz.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """Saklanan bir nesnenin sanitize edilmis kimligi."""

    digest: str
    size_bytes: int
    stored_at: dt.datetime
    media_type: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "stored_at": self.stored_at,
            "media_type": self.media_type,
            "metadata": dict(sorted(self.metadata.items())),
        }


class ObjectStore(Protocol):
    """Icerik adresli nesne deposu sozlesmesi."""

    def put(
        self,
        payload: bytes,
        *,
        media_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectInfo:
        """Icerigi saklar ve digest'ini dondurur. Ayni icerik icin idempotenttir."""
        ...

    def get(self, digest: str) -> bytes:
        """Icerigi okur ve digest'i dogrular."""
        ...

    def stat(self, digest: str) -> ObjectInfo:
        """Nesnenin kimlik bilgisini dondurur."""
        ...

    def exists(self, digest: str) -> bool:
        """Nesnenin var olup olmadigini soyler."""
        ...

    def iter_objects(self) -> Iterator[ObjectInfo]:
        """Depodaki butun nesneleri dolasir."""
        ...

    def delete(self, digest: str) -> bool:
        """Nesneyi siler. Cagiran taraf exact authorization saglamalidir."""
        ...
