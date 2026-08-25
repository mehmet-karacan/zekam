"""TTL/ETag model catalog refresh orchestration."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_catalog import (
    CatalogFetchProvenance,
    CatalogFetchStatus,
    CatalogReceiptStatus,
    CatalogRefreshStrategy,
    CatalogSource,
    ModelCatalogEntry,
    ModelCatalogSnapshot,
    catalog_fetch_response_digest,
)
from zekam.domain.runtime import EffectClaim, EffectReceipt, ReceiptStatus
from zekam.domain.security import Authorization


class CatalogStore(Protocol):
    def store(self, snapshot: ModelCatalogSnapshot) -> tuple[UUID, bool]: ...

    def latest(self, provider_id: str) -> ModelCatalogSnapshot | None: ...


@dataclass(frozen=True, slots=True)
class CatalogFetchRequest:
    provider_id: str
    etag: str | None
    force_probe: bool
    client_version: str


@dataclass(frozen=True, slots=True)
class CatalogFetchResponse:
    status_code: int
    entries: tuple[ModelCatalogEntry, ...] = ()
    etag: str | None = None
    error_category: str | None = None
    receipt: EffectReceipt | None = None

    def __post_init__(self) -> None:
        if self.status_code not in {200, 304} and self.error_category is None:
            raise ValidationFailed("Catalog fetch failure hata kategorisi ister")
        if self.status_code == 200 and (not self.entries or self.error_category is not None):
            raise ValidationFailed("Catalog 200 entries ister")
        if self.status_code == 304 and (self.entries or self.error_category is not None):
            raise ValidationFailed("Catalog 304 entries/hata tasiyamaz")
        if self.status_code not in {200, 304} and self.entries:
            raise ValidationFailed("Catalog failure entries tasiyamaz")


class CatalogFetcher(Protocol):
    def fetch(
        self, request: CatalogFetchRequest, permit: CatalogFetchPermit
    ) -> CatalogFetchResponse: ...


_PERMIT_SEAL = object()


@dataclass(frozen=True, slots=True)
class CatalogFetchPermit:
    provider_id: str
    plan_digest: str
    claim_id: UUID
    claim_digest: str
    authorization_id: UUID
    authorization_digest: str
    _seal: object

    def __post_init__(self) -> None:
        if self._seal is not _PERMIT_SEAL:
            raise PolicyViolation("Catalog fetch permit yalniz claim/auth gateway'den uretilir")


def catalog_refresh_plan_digest(
    *,
    provider_id: str,
    strategy: CatalogRefreshStrategy,
    client_version: str,
    ttl_seconds: int,
    prior_snapshot_digest: str | None,
) -> str:
    return digest(
        {
            "schema": "zekam-model-catalog-refresh-plan/v1",
            "provider_id": provider_id,
            "strategy": strategy.value,
            "client_version": client_version,
            "ttl_seconds": ttl_seconds,
            "prior_snapshot_digest": prior_snapshot_digest,
            "grants_authority": False,
        }
    )


def authorize_catalog_fetch(
    *,
    provider_id: str,
    plan_digest: str,
    authorization: Authorization,
    claim: EffectClaim,
) -> CatalogFetchPermit:
    resource = f"provider.catalog:{provider_id}"
    if (
        claim.realm_id != authorization.realm_id
        or authorization.plan_digest != plan_digest
        or authorization.effect_digest != plan_digest
        or claim.operation != "model-catalog-refresh"
        or claim.effect_digest != plan_digest
        or claim.authorization_digest != authorization.authorization_digest
        or not authorization.scope.covers_effect("model-catalog-refresh")
        or not authorization.scope.covers_resource(resource)
        or provider_id not in authorization.scope.provider_refs
    ):
        raise PolicyViolation("Catalog fetch claim/authorization exact binding mismatch")
    return CatalogFetchPermit(
        provider_id,
        plan_digest,
        claim.id,
        claim.claim_digest,
        authorization.id,
        authorization.authorization_digest,
        _PERMIT_SEAL,
    )


@dataclass(frozen=True, slots=True)
class CatalogRefreshResult:
    snapshot: ModelCatalogSnapshot | None
    disposition: str
    provider_called: bool
    persisted: bool
    warning: str | None = None
    grants_authority: bool = False


@dataclass(frozen=True, slots=True)
class ModelCatalogService:
    store: CatalogStore

    def refresh(
        self,
        *,
        realm_id: UUID,
        provider_id: str,
        strategy: CatalogRefreshStrategy,
        client_version: str,
        ttl: dt.timedelta,
        now: dt.datetime,
        fetcher: CatalogFetcher | None = None,
        permit: CatalogFetchPermit | None = None,
    ) -> CatalogRefreshResult:
        if now.tzinfo is None or not dt.timedelta(minutes=1) <= ttl <= dt.timedelta(days=7):
            raise ValidationFailed("Catalog now/TTL gecersiz")
        latest = self.store.latest(provider_id)
        fresh = latest is not None and latest.is_fresh(now=now)
        if strategy is CatalogRefreshStrategy.OFFLINE:
            return CatalogRefreshResult(
                latest,
                "cache-hit" if fresh else "cache-missing-or-stale",
                False,
                False,
            )
        if strategy is CatalogRefreshStrategy.ONLINE_IF_UNCACHED and fresh:
            return CatalogRefreshResult(latest, "cache-hit", False, False)
        if fetcher is None or permit is None or permit.provider_id != provider_id:
            raise PolicyViolation("Online catalog refresh exact fetcher permit ister")
        expected_plan_digest = catalog_refresh_plan_digest(
            provider_id=provider_id,
            strategy=strategy,
            client_version=client_version,
            ttl_seconds=int(ttl.total_seconds()),
            prior_snapshot_digest=None if latest is None else latest.snapshot_digest,
        )
        if permit.plan_digest != expected_plan_digest:
            raise PolicyViolation("Catalog refresh permit exact plan ile eslesmiyor")
        response = fetcher.fetch(
            CatalogFetchRequest(
                provider_id=provider_id,
                etag=(
                    None
                    if strategy is CatalogRefreshStrategy.FORCE_PROBE or latest is None
                    else latest.etag
                ),
                force_probe=strategy is CatalogRefreshStrategy.FORCE_PROBE,
                client_version=client_version,
            ),
            permit,
        )
        receipt = response.receipt
        if (
            receipt is None
            or receipt.claim_id != permit.claim_id
            or receipt.adapter_evidence_digest is None
        ):
            raise PolicyViolation("Catalog fetch terminal receipt provenance ister")
        prior: UUID | None
        if response.status_code == 304:
            if latest is None or latest.fetch_status is CatalogFetchStatus.FAILED:
                raise PolicyViolation("Catalog 304 current basarili snapshot ister")
            entries = latest.entries
            etag = response.etag or latest.etag
            status = CatalogFetchStatus.NOT_MODIFIED
            error = None
            prior = latest.id
        elif response.status_code == 200:
            entries = tuple(sorted(response.entries, key=lambda item: item.model_id))
            etag = response.etag
            status = CatalogFetchStatus.FETCHED
            error = None
            prior = None if latest is None else latest.id
        else:
            entries = ()
            etag = response.etag
            status = CatalogFetchStatus.FAILED
            error = response.error_category or "provider-error"
            prior = None if latest is None else latest.id
        raw_response_entries = entries if response.status_code == 200 else ()
        response_digest = catalog_fetch_response_digest(
            status_code=response.status_code,
            entries=raw_response_entries,
            etag=response.etag,
            error_category=response.error_category,
        )
        if response.status_code in {200, 304}:
            if (
                receipt.status is not ReceiptStatus.COMPLETED
                or receipt.result_digest != response_digest
            ):
                raise PolicyViolation("Catalog success receipt response digest ile eslesmiyor")
            receipt_status = CatalogReceiptStatus.COMPLETED
        else:
            if (
                receipt.status is not ReceiptStatus.FAILED
                or receipt.failure_digest != response_digest
            ):
                raise PolicyViolation("Catalog failure receipt response digest ile eslesmiyor")
            receipt_status = CatalogReceiptStatus.FAILED
        provenance = CatalogFetchProvenance(
            plan_digest=permit.plan_digest,
            strategy=strategy,
            ttl_seconds=int(ttl.total_seconds()),
            prior_snapshot_digest=None if latest is None else latest.snapshot_digest,
            authorization_id=permit.authorization_id,
            authorization_digest=permit.authorization_digest,
            claim_id=permit.claim_id,
            claim_digest=permit.claim_digest,
            receipt_id=receipt.id,
            receipt_status=receipt_status,
            status_code=response.status_code,
            response_etag=response.etag,
            response_digest=response_digest,
            adapter_evidence_digest=receipt.adapter_evidence_digest,
        )
        snapshot = ModelCatalogSnapshot(
            id=uuid4(),
            realm_id=realm_id,
            provider_id=provider_id,
            entries=entries,
            etag=etag,
            fetched_at=now,
            expires_at=now + ttl,
            client_version=client_version,
            source=CatalogSource.REMOTE,
            fetch_status=status,
            error_category=error,
            prior_snapshot_id=prior,
            fetch_provenance=provenance,
        )
        try:
            _, created = self.store.store(snapshot)
        except Exception as exc:
            return CatalogRefreshResult(
                snapshot,
                "observed-cache-write-failed",
                True,
                False,
                warning=f"cache-write:{type(exc).__name__}",
            )
        return CatalogRefreshResult(snapshot, status.value, True, created)
