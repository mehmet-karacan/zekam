"""Yedek manifesti.

Manifest, bir kurulumun geri yuklenebilir durumunu tarif eder:

- migration head'i ve uygulanan her migration'in checksum'i,
- nesne deposundaki her artifact'in digest ve boyutu,
- sanitize edilmis yapilandirma digest'i,
- urun surumu ve uretim zamani.

Manifest secret degeri, absolute path veya aktif lease tasimaz. Manifest'in kendi
digest'i icerikten hesaplanir; boylece yedek butunlugu bagimsiz dogrulanabilir.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zekam import __version__
from zekam.application.object_store import ObjectStore
from zekam.domain.canonical import digest
from zekam.domain.identity import PRODUCT

BACKUP_MANIFEST_SCHEMA = "zekam-backup-manifest/v1"


class VerificationOutcome(StrEnum):
    """Manifest dogrulamasinin sonucu."""

    VALID = "valid"
    ALTERED = "altered"
    INCOMPLETE = "incomplete"
    CORRUPT = "corrupt"


@dataclass(frozen=True, slots=True)
class SchemaState:
    """Manifest icindeki veritabani sema durumu."""

    head: int | None
    migrations: tuple[tuple[int, str, str], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "head": self.head,
            "migrations": [
                {"version": version, "name": name, "checksum": checksum}
                for version, name, checksum in self.migrations
            ],
        }


@dataclass(frozen=True, slots=True)
class ArtifactEntry:
    """Manifest icindeki tek bir artifact."""

    digest: str
    size_bytes: int
    media_type: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """Geri yuklenebilir yedek tanimi."""

    schema: str
    product: str
    product_version: str
    created_at: dt.datetime
    schema_state: SchemaState
    artifacts: tuple[ArtifactEntry, ...]
    configuration_digest: str
    manifest_digest: str

    @property
    def total_bytes(self) -> int:
        return sum(entry.size_bytes for entry in self.artifacts)

    def body(self) -> dict[str, Any]:
        """Digest hesaplanan govde (manifest_digest haric)."""
        return {
            "schema": self.schema,
            "product": self.product,
            "product_version": self.product_version,
            "created_at": self.created_at,
            "schema_state": self.schema_state.as_dict(),
            "artifacts": [entry.as_dict() for entry in self.artifacts],
            "configuration_digest": self.configuration_digest,
        }

    def as_dict(self) -> dict[str, Any]:
        return self.body() | {"manifest_digest": self.manifest_digest}


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """Manifest dogrulamasinin ayrintili sonucu."""

    outcome: VerificationOutcome
    missing: tuple[str, ...] = ()
    corrupt: tuple[str, ...] = ()
    detail: str = ""

    @property
    def is_valid(self) -> bool:
        return self.outcome is VerificationOutcome.VALID

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "missing": list(self.missing),
            "corrupt": list(self.corrupt),
            "detail": self.detail,
        }


def build_manifest(
    *,
    schema_state: SchemaState,
    store: ObjectStore,
    configuration: dict[str, Any],
    now: dt.datetime | None = None,
) -> BackupManifest:
    """Mevcut durumdan yedek manifesti uretir."""
    moment = now or dt.datetime.now(dt.UTC)
    artifacts = tuple(
        sorted(
            (
                ArtifactEntry(
                    digest=info.digest, size_bytes=info.size_bytes, media_type=info.media_type
                )
                for info in store.iter_objects()
            ),
            key=lambda entry: entry.digest,
        )
    )
    manifest = BackupManifest(
        schema=BACKUP_MANIFEST_SCHEMA,
        product=PRODUCT.name,
        product_version=__version__,
        created_at=moment,
        schema_state=schema_state,
        artifacts=artifacts,
        configuration_digest=digest(configuration),
        manifest_digest="",
    )
    return BackupManifest(
        schema=manifest.schema,
        product=manifest.product,
        product_version=manifest.product_version,
        created_at=manifest.created_at,
        schema_state=manifest.schema_state,
        artifacts=manifest.artifacts,
        configuration_digest=manifest.configuration_digest,
        manifest_digest=digest(manifest.body()),
    )


def verify_manifest(manifest: BackupManifest, store: ObjectStore) -> VerificationResult:
    """Manifest butunlugunu ve artifact varligini dogrular."""
    if digest(manifest.body()) != manifest.manifest_digest:
        return VerificationResult(
            outcome=VerificationOutcome.ALTERED,
            detail="Manifest govdesi manifest_digest ile uyusmuyor",
        )

    missing: list[str] = []
    corrupt: list[str] = []
    for entry in manifest.artifacts:
        if not store.exists(entry.digest):
            missing.append(entry.digest)
            continue
        try:
            payload = store.get(entry.digest)
        except Exception:
            corrupt.append(entry.digest)
            continue
        if len(payload) != entry.size_bytes:
            corrupt.append(entry.digest)

    if corrupt:
        return VerificationResult(
            outcome=VerificationOutcome.CORRUPT,
            missing=tuple(missing),
            corrupt=tuple(corrupt),
            detail=f"{len(corrupt)} artifact bozuk",
        )
    if missing:
        return VerificationResult(
            outcome=VerificationOutcome.INCOMPLETE,
            missing=tuple(missing),
            detail=f"{len(missing)} artifact eksik",
        )
    return VerificationResult(outcome=VerificationOutcome.VALID, detail="Yedek dogrulandi")


def schema_state_from_status(
    head: int | None, applied: Sequence[tuple[int, str, str]]
) -> SchemaState:
    """Migration durumundan manifest sema durumunu uretir."""
    return SchemaState(head=head, migrations=tuple(sorted(applied)))
