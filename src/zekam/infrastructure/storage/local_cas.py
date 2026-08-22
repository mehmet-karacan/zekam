"""Yerel icerik adresli nesne deposu (CAS).

Yerlesim:

```text
<kok>/
  sha256/
    ab/
      cd/
        abcd... .bin     -> icerik
        abcd... .json    -> sanitize edilmis metadata
```

Garantiler:

- Yazma atomiktir: gecici dosyaya yazilir, fsync edilir, sonra `os.replace` ile
  hedefe tasinir. Yarim dosya gorunmez.
- Okuma sirasinda digest yeniden hesaplanir; bozulma sessizce gecmez.
- Ayni icerigin ikinci kez yazilmasi mevcut kaydi degistirmez.
- Metadata secret degeri tasiyamaz.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from zekam.application.object_store import ObjectInfo
from zekam.domain.canonical import DIGEST_PREFIX, parse_digest
from zekam.domain.errors import NotFound, PolicyViolation, ValidationFailed

#: Metadata anahtarlarinda gorunmesi yasak ifadeler.
FORBIDDEN_METADATA_KEYS: frozenset[str] = frozenset(
    {"password", "passwd", "secret", "token", "api_key", "apikey", "private_key", "credential"}
)

ALGORITHM_DIRECTORY = "sha256"
CONTENT_SUFFIX = ".bin"
METADATA_SUFFIX = ".json"


class IntegrityError(ValidationFailed):
    """Depodaki icerik beklenen digest ile uyusmuyor."""

    code = "object-store-integrity-error"


@dataclass(frozen=True, slots=True)
class LocalContentAddressedStore:
    """Dosya sistemi tabanli icerik adresli depo."""

    root: Path

    def ensure(self) -> LocalContentAddressedStore:
        """Kok dizini olusturur (idempotent)."""
        (self.root / ALGORITHM_DIRECTORY).mkdir(parents=True, exist_ok=True)
        return self

    # -- yol hesaplamasi ---------------------------------------------------------

    def _paths(self, digest: str) -> tuple[Path, Path]:
        hexadecimal = parse_digest(digest)
        directory = self.root / ALGORITHM_DIRECTORY / hexadecimal[:2] / hexadecimal[2:4]
        return (
            directory / f"{hexadecimal}{CONTENT_SUFFIX}",
            directory / f"{hexadecimal}{METADATA_SUFFIX}",
        )

    # -- yazma -------------------------------------------------------------------

    def put(
        self,
        payload: bytes,
        *,
        media_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectInfo:
        """Icerigi saklar. Ayni icerik icin idempotenttir."""
        safe_metadata = _validate_metadata(metadata or {})
        digest = DIGEST_PREFIX + hashlib.sha256(payload).hexdigest()
        content_path, metadata_path = self._paths(digest)

        if content_path.exists():
            return self.stat(digest)

        content_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(content_path, payload)

        info = ObjectInfo(
            digest=digest,
            size_bytes=len(payload),
            stored_at=dt.datetime.now(dt.UTC),
            media_type=media_type,
            metadata=safe_metadata,
        )
        _atomic_write(
            metadata_path,
            (
                json.dumps(
                    {
                        "digest": info.digest,
                        "size_bytes": info.size_bytes,
                        "stored_at": info.stored_at.isoformat(),
                        "media_type": info.media_type,
                        "metadata": info.metadata,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            ).encode("utf-8"),
        )
        return info

    def put_file(
        self,
        source: Path,
        *,
        media_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ObjectInfo:
        """Dosya icerigini saklar."""
        return self.put(source.read_bytes(), media_type=media_type, metadata=metadata)

    # -- okuma -------------------------------------------------------------------

    def get(self, digest: str) -> bytes:
        """Icerigi okur ve digest'i yeniden dogrular."""
        content_path, _ = self._paths(digest)
        if not content_path.is_file():
            raise NotFound("Nesne bulunamadi")
        payload = content_path.read_bytes()
        actual = DIGEST_PREFIX + hashlib.sha256(payload).hexdigest()
        if actual != digest:
            raise IntegrityError("Depodaki icerik beklenen digest ile uyusmuyor")
        return payload

    def stat(self, digest: str) -> ObjectInfo:
        """Nesnenin metadata kaydini dondurur."""
        content_path, metadata_path = self._paths(digest)
        if not content_path.is_file():
            raise NotFound("Nesne bulunamadi")
        if metadata_path.is_file():
            document = json.loads(metadata_path.read_text(encoding="utf-8"))
            return ObjectInfo(
                digest=document["digest"],
                size_bytes=int(document["size_bytes"]),
                stored_at=dt.datetime.fromisoformat(document["stored_at"]),
                media_type=document.get("media_type"),
                metadata=dict(document.get("metadata") or {}),
            )
        stats = content_path.stat()
        return ObjectInfo(
            digest=digest,
            size_bytes=stats.st_size,
            stored_at=dt.datetime.fromtimestamp(stats.st_mtime, tz=dt.UTC),
        )

    def exists(self, digest: str) -> bool:
        content_path, _ = self._paths(digest)
        return content_path.is_file()

    def iter_objects(self) -> Iterator[ObjectInfo]:
        """Depodaki butun nesneleri digest sirasiyla dolasir."""
        base = self.root / ALGORITHM_DIRECTORY
        if not base.is_dir():
            return
        for path in sorted(base.rglob(f"*{CONTENT_SUFFIX}")):
            yield self.stat(DIGEST_PREFIX + path.stem)

    def verify_all(self) -> tuple[str, ...]:
        """Butun nesneleri dogrular ve bozuk olanlarin digest'lerini dondurur."""
        broken: list[str] = []
        for info in self.iter_objects():
            try:
                self.get(info.digest)
            except IntegrityError:
                broken.append(info.digest)
        return tuple(broken)

    # -- silme -------------------------------------------------------------------

    def delete(self, digest: str) -> bool:
        """Nesneyi siler.

        Bu islem geri alinamaz ve yalnizca exact authorization ile cagrilmalidir.
        """
        content_path, metadata_path = self._paths(digest)
        if not content_path.is_file():
            return False
        content_path.unlink()
        metadata_path.unlink(missing_ok=True)
        return True


def _validate_metadata(metadata: dict[str, str]) -> dict[str, str]:
    for key, value in metadata.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValidationFailed("Metadata yalnizca metin anahtar ve deger kabul eder")
        if key.lower() in FORBIDDEN_METADATA_KEYS:
            raise PolicyViolation(f"Metadata secret alani tasiyamaz: {key}")
    return dict(sorted(metadata.items()))


def _atomic_write(target: Path, payload: bytes) -> None:
    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
