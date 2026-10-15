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
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from zekam.application.bounded_file import read_bounded_bytes, read_bounded_text
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
MAX_OBJECTS = 100_000
MAX_DIRECTORY_ENTRIES = 100_000
MAX_CAS_OBJECT_BYTES = 256 * 1024 * 1024
MAX_CAS_METADATA_BYTES = 64 * 1024


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
        try:
            payload = read_bounded_bytes(source, max_bytes=MAX_CAS_OBJECT_BYTES)
        except OSError as exc:
            raise PolicyViolation("CAS kaynak dosyasi bounded regular olmali") from exc
        return self.put(payload, media_type=media_type, metadata=metadata)

    # -- okuma -------------------------------------------------------------------

    def get(self, digest: str) -> bytes:
        """Icerigi okur ve digest'i yeniden dogrular."""
        content_path, _ = self._paths(digest)
        if not content_path.is_file():
            raise NotFound("Nesne bulunamadi")
        try:
            payload = read_bounded_bytes(content_path, max_bytes=MAX_CAS_OBJECT_BYTES)
        except OSError as exc:
            raise IntegrityError("Depodaki icerik bounded regular dosya olmali") from exc
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
            try:
                document = json.loads(
                    read_bounded_text(metadata_path, max_bytes=MAX_CAS_METADATA_BYTES)
                )
            except (OSError, UnicodeError, ValueError) as exc:
                raise IntegrityError("CAS metadata bounded JSON olmali") from exc
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
        for path in _iter_content_paths(base):
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


def _iter_content_paths(base: Path) -> Iterator[Path]:
    """Walk the fixed sha256/prefix/object layout with a bounded stream."""

    observed = 0
    first_entries = _sorted_entries(
        base,
        limit=MAX_DIRECTORY_ENTRIES,
        message="CAS kok girdisi siniri asildi",
    )
    for first_entry in first_entries:
        first = Path(first_entry.path)
        if _is_link_or_reparse(first):
            raise ValidationFailed("CAS prefix link veya reparse olamaz")
        try:
            if not first_entry.is_dir(follow_symlinks=False):
                continue
        except OSError as exc:
            raise ValidationFailed("CAS prefix okunamadi") from exc
        second_entries = _sorted_entries(
            first,
            limit=MAX_DIRECTORY_ENTRIES,
            message="CAS shard girdisi siniri asildi",
        )
        for second_entry in second_entries:
            second = Path(second_entry.path)
            if _is_link_or_reparse(second):
                raise ValidationFailed("CAS shard link veya reparse olamaz")
            try:
                if not second_entry.is_dir(follow_symlinks=False):
                    continue
            except OSError as exc:
                raise ValidationFailed("CAS shard okunamadi") from exc
            object_entries = _sorted_entries(
                second,
                limit=MAX_OBJECTS,
                message="CAS nesne sayisi siniri asildi",
                suffix=CONTENT_SUFFIX,
            )
            for object_entry in object_entries:
                target = Path(object_entry.path)
                if _is_link_or_reparse(target):
                    raise ValidationFailed("CAS nesnesi link veya reparse olamaz")
                try:
                    if not object_entry.is_file(follow_symlinks=False):
                        raise ValidationFailed("CAS nesnesi regular dosya olmali")
                except OSError as exc:
                    raise ValidationFailed("CAS nesnesi okunamadi") from exc
                observed += 1
                if observed > MAX_OBJECTS:
                    raise PolicyViolation("CAS nesne sayisi siniri asildi")
                yield target


def _sorted_entries(
    directory: Path,
    *,
    limit: int,
    message: str,
    suffix: str | None = None,
) -> list[os.DirEntry[str]]:
    entries: list[os.DirEntry[str]] = []
    observed = 0
    try:
        with os.scandir(directory) as scan:
            for entry in scan:
                observed += 1
                if observed > limit:
                    raise PolicyViolation(message)
                if suffix is not None and not entry.name.endswith(suffix):
                    continue
                entries.append(entry)
    except OSError as exc:
        raise ValidationFailed("CAS dizini okunamadi") from exc
    entries.sort(key=lambda entry: entry.name)
    return entries


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValidationFailed("CAS metadata okunamadi") from exc
    return path.is_symlink() or bool(
        int(getattr(info, "st_file_attributes", 0))
        & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    )


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
