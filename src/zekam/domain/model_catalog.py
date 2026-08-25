"""Provider model availability observations; routing qualification degildir."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class CatalogSource(StrEnum):
    REMOTE = "remote"
    STATIC = "static"
    PACKAGE = "package"


class CatalogFetchStatus(StrEnum):
    FETCHED = "fetched"
    NOT_MODIFIED = "not-modified"
    FAILED = "failed"


class CatalogRefreshStrategy(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    ONLINE_IF_UNCACHED = "online-if-uncached"
    FORCE_PROBE = "force-probe"


class CatalogVisibility(StrEnum):
    PUBLIC = "public"
    AUTHENTICATED = "authenticated"
    RESTRICTED = "restricted"


class CatalogReceiptStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


def _portable(value: str, label: str, *, maximum: int = 256) -> None:
    if (
        not value.strip()
        or value != value.strip()
        or len(value) > maximum
        or "://" in value
        or "\\" in value
    ):
        raise ValidationFailed(f"{label} portable ve bos olmayan metadata olmali")


def _aware(value: dt.datetime, label: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValidationFailed(f"{label} timezone tasimali")


@dataclass(frozen=True, slots=True)
class ModelCatalogEntry:
    model_id: str
    visibility: CatalogVisibility
    authentication_required: bool
    endpoint_class: str
    capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _portable(self.model_id, "Catalog model id")
        _portable(self.endpoint_class, "Catalog endpoint class")
        if tuple(sorted(set(self.capabilities))) != self.capabilities:
            raise ValidationFailed("Catalog capability listesi unique ve sirali olmali")
        for capability in self.capabilities:
            _portable(capability, "Catalog capability", maximum=128)
        if self.visibility is not CatalogVisibility.PUBLIC and not self.authentication_required:
            raise ValidationFailed("Public olmayan catalog modeli authentication ister")

    def body(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "visibility": self.visibility.value,
            "authentication_required": self.authentication_required,
            "endpoint_class": self.endpoint_class,
            "capabilities": list(self.capabilities),
        }


def catalog_fetch_response_digest(
    *,
    status_code: int,
    entries: tuple[ModelCatalogEntry, ...],
    etag: str | None,
    error_category: str | None,
) -> str:
    return digest(
        {
            "schema": "zekam-model-catalog-fetch-response/v1",
            "status_code": status_code,
            "entries": [item.body() for item in entries],
            "etag": etag,
            "error_category": error_category,
        }
    )


@dataclass(frozen=True, slots=True)
class CatalogFetchProvenance:
    plan_digest: str
    strategy: CatalogRefreshStrategy
    ttl_seconds: int
    prior_snapshot_digest: str | None
    authorization_id: UUID
    authorization_digest: str
    claim_id: UUID
    claim_digest: str
    receipt_id: UUID
    receipt_status: CatalogReceiptStatus
    status_code: int
    response_etag: str | None
    response_digest: str
    adapter_evidence_digest: str

    def __post_init__(self) -> None:
        for value in (
            self.plan_digest,
            self.authorization_digest,
            self.claim_digest,
            self.response_digest,
            self.adapter_evidence_digest,
        ):
            parse_digest(value)
        if self.prior_snapshot_digest is not None:
            parse_digest(self.prior_snapshot_digest)
        if not 60 <= self.ttl_seconds <= 7 * 24 * 60 * 60:
            raise ValidationFailed("Catalog provenance TTL gecersiz")
        if self.strategy is CatalogRefreshStrategy.OFFLINE:
            raise ValidationFailed("Remote catalog offline fetch provenance tasiyamaz")
        if self.status_code < 100 or self.status_code > 599:
            raise ValidationFailed("Catalog provider status code gecersiz")
        if self.response_etag is not None:
            _portable(self.response_etag, "Catalog response ETag", maximum=512)
        if (self.status_code in {200, 304}) != (
            self.receipt_status is CatalogReceiptStatus.COMPLETED
        ):
            raise ValidationFailed("Catalog response/receipt terminal durumu tutarsiz")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-model-catalog-fetch-provenance/v1",
            "plan_digest": self.plan_digest,
            "strategy": self.strategy.value,
            "ttl_seconds": self.ttl_seconds,
            "prior_snapshot_digest": self.prior_snapshot_digest,
            "authorization_id": str(self.authorization_id),
            "authorization_digest": self.authorization_digest,
            "claim_id": str(self.claim_id),
            "claim_digest": self.claim_digest,
            "receipt_id": str(self.receipt_id),
            "receipt_status": self.receipt_status.value,
            "status_code": self.status_code,
            "response_etag": self.response_etag,
            "response_digest": self.response_digest,
            "adapter_evidence_digest": self.adapter_evidence_digest,
        }


@dataclass(frozen=True, slots=True)
class ModelCatalogSnapshot:
    id: UUID
    realm_id: UUID
    provider_id: str
    entries: tuple[ModelCatalogEntry, ...]
    etag: str | None
    fetched_at: dt.datetime
    expires_at: dt.datetime
    client_version: str
    source: CatalogSource
    fetch_status: CatalogFetchStatus
    error_category: str | None
    prior_snapshot_id: UUID | None = None
    fetch_provenance: CatalogFetchProvenance | None = None
    grants_authority: bool = False

    def __post_init__(self) -> None:
        _portable(self.provider_id, "Catalog provider")
        _portable(self.client_version, "Catalog client version", maximum=128)
        _aware(self.fetched_at, "Catalog fetched_at")
        _aware(self.expires_at, "Catalog expires_at")
        if self.expires_at <= self.fetched_at:
            raise ValidationFailed("Catalog expiry fetch sonrasinda olmali")
        model_ids = tuple(item.model_id for item in self.entries)
        if model_ids != tuple(sorted(set(model_ids))):
            raise ValidationFailed("Catalog entries model id ile unique ve sirali olmali")
        if self.etag is not None:
            _portable(self.etag, "Catalog ETag", maximum=512)
        if self.fetch_status is CatalogFetchStatus.FAILED:
            if self.entries or not (self.error_category or "").strip():
                raise ValidationFailed("Failed catalog entries tasiyamaz ve hata kategorisi ister")
            _portable(self.error_category or "", "Catalog hata kategorisi", maximum=128)
        elif not self.entries or self.error_category is not None:
            raise ValidationFailed("Basarili catalog entry ister ve hata kategorisi tasiyamaz")
        if self.fetch_status is CatalogFetchStatus.NOT_MODIFIED and self.prior_snapshot_id is None:
            raise ValidationFailed("Not-modified catalog prior snapshot ister")
        if (
            self.fetch_status is CatalogFetchStatus.NOT_MODIFIED
            and self.source is not CatalogSource.REMOTE
        ):
            raise ValidationFailed("Not-modified yalniz remote catalog icindir")
        if self.source is not CatalogSource.REMOTE and self.etag is not None:
            raise ValidationFailed("Yalniz remote catalog ETag tasiyabilir")
        if (self.source is CatalogSource.REMOTE) != (self.fetch_provenance is not None):
            raise ValidationFailed("Remote catalog exact fetch provenance ister")
        if self.fetch_provenance is not None:
            expected_plan = digest(
                {
                    "schema": "zekam-model-catalog-refresh-plan/v1",
                    "provider_id": self.provider_id,
                    "strategy": self.fetch_provenance.strategy.value,
                    "client_version": self.client_version,
                    "ttl_seconds": self.fetch_provenance.ttl_seconds,
                    "prior_snapshot_digest": self.fetch_provenance.prior_snapshot_digest,
                    "grants_authority": False,
                }
            )
            if self.fetch_provenance.plan_digest != expected_plan:
                raise PolicyViolation("Catalog fetch plan provenance drift")
            if self.fetch_provenance.ttl_seconds != int(
                (self.expires_at - self.fetched_at).total_seconds()
            ):
                raise ValidationFailed("Catalog snapshot TTL plan ile eslesmiyor")
            if (self.prior_snapshot_id is None) != (
                self.fetch_provenance.prior_snapshot_digest is None
            ):
                raise ValidationFailed("Catalog prior snapshot provenance eksik")
            raw_entries = self.entries if self.fetch_status is CatalogFetchStatus.FETCHED else ()
            expected_response = catalog_fetch_response_digest(
                status_code=self.fetch_provenance.status_code,
                entries=raw_entries,
                etag=self.fetch_provenance.response_etag,
                error_category=self.error_category,
            )
            if self.fetch_provenance.response_digest != expected_response:
                raise PolicyViolation("Catalog fetch response provenance drift")
            expected_status = {
                CatalogFetchStatus.FETCHED: 200,
                CatalogFetchStatus.NOT_MODIFIED: 304,
            }.get(self.fetch_status)
            if expected_status is not None and self.fetch_provenance.status_code != expected_status:
                raise ValidationFailed("Catalog fetch status/provenance tutarsiz")
            if self.fetch_status is CatalogFetchStatus.FAILED and (
                self.fetch_provenance.status_code in {200, 304}
            ):
                raise ValidationFailed("Failed catalog basarili provider statusu tasiyamaz")
        if self.grants_authority:
            raise PolicyViolation("Catalog availability routing authority veremez")

    def catalog_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-model-catalog/v1",
            "provider_id": self.provider_id,
            "source": self.source.value,
            "entries": [item.body() for item in self.entries],
            "grants_authority": False,
        }

    @property
    def catalog_digest(self) -> str:
        return digest(self.catalog_body())

    def manifest_body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-model-catalog-snapshot/v1",
            "id": str(self.id),
            "realm_id": str(self.realm_id),
            "provider_id": self.provider_id,
            "catalog_digest": self.catalog_digest,
            "etag": self.etag,
            "fetched_at": self.fetched_at,
            "expires_at": self.expires_at,
            "client_version": self.client_version,
            "source": self.source.value,
            "fetch_status": self.fetch_status.value,
            "error_category": self.error_category,
            "prior_snapshot_id": (
                None if self.prior_snapshot_id is None else str(self.prior_snapshot_id)
            ),
            "fetch_provenance": (
                None if self.fetch_provenance is None else self.fetch_provenance.body()
            ),
            "grants_authority": False,
        }

    @property
    def snapshot_digest(self) -> str:
        return digest(self.manifest_body())

    def is_fresh(self, *, now: dt.datetime) -> bool:
        _aware(now, "Catalog freshness now")
        return self.fetch_status is not CatalogFetchStatus.FAILED and now < self.expires_at

    def includes(self, model_id: str, *, now: dt.datetime) -> bool:
        return self.is_fresh(now=now) and any(item.model_id == model_id for item in self.entries)

    def assert_digest(self, expected: str) -> None:
        parse_digest(expected)
        if self.snapshot_digest != expected:
            raise PolicyViolation("Catalog snapshot digest drift")


@dataclass(frozen=True, slots=True)
class CatalogAvailability:
    model_id: str
    available: bool
    reason: str | None
    catalog_digest: str | None
    snapshot_digest: str | None
    grants_authority: bool = False

    def __post_init__(self) -> None:
        _portable(self.model_id, "Availability model id")
        if self.available != (self.reason is None):
            raise ValidationFailed("Catalog availability reason/status tutarsiz")
        for value in (self.catalog_digest, self.snapshot_digest):
            if value is not None:
                parse_digest(value)
        if self.grants_authority:
            raise PolicyViolation("Catalog availability authority veremez")


def observe_availability(
    snapshot: ModelCatalogSnapshot | None,
    model_id: str,
    *,
    now: dt.datetime,
) -> CatalogAvailability:
    _portable(model_id, "Availability model id")
    _aware(now, "Availability now")
    if snapshot is None:
        return CatalogAvailability(model_id, False, "catalog-missing", None, None)
    if not snapshot.is_fresh(now=now):
        return CatalogAvailability(
            model_id,
            False,
            "catalog-fetch-failed"
            if snapshot.fetch_status is CatalogFetchStatus.FAILED
            else "catalog-stale",
            snapshot.catalog_digest,
            snapshot.snapshot_digest,
        )
    if not snapshot.includes(model_id, now=now):
        return CatalogAvailability(
            model_id,
            False,
            "availability-missing",
            snapshot.catalog_digest,
            snapshot.snapshot_digest,
        )
    return CatalogAvailability(
        model_id, True, None, snapshot.catalog_digest, snapshot.snapshot_digest
    )
