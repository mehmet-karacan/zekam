from __future__ import annotations

import datetime as dt
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from zekam.application.model_catalog import (
    CatalogFetchPermit,
    CatalogFetchRequest,
    CatalogFetchResponse,
    ModelCatalogService,
    authorize_catalog_fetch,
    catalog_refresh_plan_digest,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_catalog import (
    CatalogFetchProvenance,
    CatalogFetchStatus,
    CatalogReceiptStatus,
    CatalogRefreshStrategy,
    CatalogSource,
    CatalogVisibility,
    ModelCatalogEntry,
    ModelCatalogSnapshot,
    catalog_fetch_response_digest,
    observe_availability,
)
from zekam.domain.runtime import EffectClaim, EffectReceipt, FailureCategory
from zekam.domain.security import Authorization, AuthorizationScope

NOW = dt.datetime(2026, 8, 25, 9, tzinfo=dt.UTC)
REALM = uuid4()
PROVIDER = "aihub"


def _entry(model_id: str = "model-a") -> ModelCatalogEntry:
    return ModelCatalogEntry(
        model_id=model_id,
        visibility=CatalogVisibility.AUTHENTICATED,
        authentication_required=True,
        endpoint_class="chat-completions",
        capabilities=("text",),
    )


def _snapshot(*, fetched_at: dt.datetime = NOW) -> ModelCatalogSnapshot:
    plan_digest = catalog_refresh_plan_digest(
        provider_id=PROVIDER,
        strategy=CatalogRefreshStrategy.ONLINE,
        client_version="zekam-test/1",
        ttl_seconds=3600,
        prior_snapshot_digest=None,
    )
    response_digest = catalog_fetch_response_digest(
        status_code=200,
        entries=(_entry(),),
        etag="etag-v1",
        error_category=None,
    )
    return ModelCatalogSnapshot(
        id=uuid4(),
        realm_id=REALM,
        provider_id=PROVIDER,
        entries=(_entry(),),
        etag="etag-v1",
        fetched_at=fetched_at,
        expires_at=fetched_at + dt.timedelta(hours=1),
        client_version="zekam-test/1",
        source=CatalogSource.REMOTE,
        fetch_status=CatalogFetchStatus.FETCHED,
        error_category=None,
        fetch_provenance=CatalogFetchProvenance(
            plan_digest=plan_digest,
            strategy=CatalogRefreshStrategy.ONLINE,
            ttl_seconds=3600,
            prior_snapshot_digest=None,
            authorization_id=uuid4(),
            authorization_digest=digest("catalog-authorization"),
            claim_id=uuid4(),
            claim_digest=digest("catalog-claim"),
            receipt_id=uuid4(),
            receipt_status=CatalogReceiptStatus.COMPLETED,
            status_code=200,
            response_etag="etag-v1",
            response_digest=response_digest,
            adapter_evidence_digest=digest("catalog-adapter-evidence"),
        ),
    )


class Store:
    def __init__(
        self, snapshot: ModelCatalogSnapshot | None = None, *, fail_write: bool = False
    ) -> None:
        self.snapshot = snapshot
        self.fail_write = fail_write
        self.stored: list[ModelCatalogSnapshot] = []

    def latest(self, provider_id: str) -> ModelCatalogSnapshot | None:
        assert provider_id == PROVIDER
        return self.snapshot

    def store(self, snapshot: ModelCatalogSnapshot) -> tuple[UUID, bool]:
        if self.fail_write:
            raise OSError("cache unavailable")
        self.snapshot = snapshot
        self.stored.append(snapshot)
        return snapshot.id, True


class Fetcher:
    def __init__(self, response: CatalogFetchResponse) -> None:
        self.response = response
        self.requests: list[CatalogFetchRequest] = []

    def fetch(self, request, permit):  # type: ignore[no-untyped-def]
        assert permit.provider_id == PROVIDER
        self.requests.append(request)
        return self.response


def _permit(strategy: CatalogRefreshStrategy, prior: str | None = None):  # type: ignore[no-untyped-def]
    plan = catalog_refresh_plan_digest(
        provider_id=PROVIDER,
        strategy=strategy,
        client_version="zekam-test/1",
        ttl_seconds=3600,
        prior_snapshot_digest=prior,
    )
    authorization = Authorization.issue(
        realm_id=REALM,
        actor_id=uuid4(),
        plan_digest=plan,
        effect_digest=plan,
        scope=AuthorizationScope(
            allowed_resources=(f"provider.catalog:{PROVIDER}",),
            allowed_effects=("model-catalog-refresh",),
            provider_refs=(PROVIDER,),
        ),
        risk="low",
        lifetime=dt.timedelta(minutes=10),
        now=NOW,
    )
    claim = EffectClaim.create(
        realm_id=REALM,
        job_id=uuid4(),
        attempt_id=uuid4(),
        operation="model-catalog-refresh",
        effect_digest=plan,
        authorization_digest=authorization.authorization_digest,
        idempotency_key=plan,
        resources=(),
        execution_identity="catalog-worker",
        fencing_token=1,
        adapter_digest=digest("catalog-adapter"),
        now=NOW,
    )
    permit = authorize_catalog_fetch(
        provider_id=PROVIDER,
        plan_digest=plan,
        authorization=authorization,
        claim=claim,
    )
    return permit, claim


def _response(
    status_code: int,
    claim: EffectClaim,
    *,
    entries: tuple[ModelCatalogEntry, ...] = (),
    etag: str | None = None,
    error_category: str | None = None,
) -> CatalogFetchResponse:
    response_digest = catalog_fetch_response_digest(
        status_code=status_code,
        entries=entries if status_code == 200 else (),
        etag=etag,
        error_category=error_category,
    )
    receipt = (
        EffectReceipt.completed(
            realm_id=REALM,
            claim=claim,
            result_digest=response_digest,
            adapter_evidence_digest=digest("catalog-adapter-evidence"),
            now=NOW,
        )
        if status_code in {200, 304}
        else EffectReceipt.failed(
            realm_id=REALM,
            claim=claim,
            category=FailureCategory.PROVIDER,
            failure_digest=response_digest,
            adapter_evidence_digest=digest("catalog-adapter-evidence"),
            now=NOW,
        )
    )
    return CatalogFetchResponse(status_code, entries, etag, error_category, receipt)


def test_catalog_is_availability_only_and_stale_fails_closed() -> None:
    snapshot = _snapshot()
    current = observe_availability(snapshot, "model-a", now=NOW)
    assert current.available and current.grants_authority is False
    assert current.catalog_digest == snapshot.catalog_digest
    assert observe_availability(snapshot, "model-b", now=NOW).reason == "availability-missing"
    assert (
        observe_availability(snapshot, "model-a", now=NOW + dt.timedelta(hours=2)).reason
        == "catalog-stale"
    )
    assert observe_availability(None, "model-a", now=NOW).reason == "catalog-missing"


def test_offline_and_online_if_uncached_do_not_make_unneeded_provider_calls() -> None:
    snapshot = _snapshot()
    store = Store(snapshot)
    service = ModelCatalogService(store)
    offline = service.refresh(
        realm_id=REALM,
        provider_id=PROVIDER,
        strategy=CatalogRefreshStrategy.OFFLINE,
        client_version="zekam-test/1",
        ttl=dt.timedelta(hours=1),
        now=NOW,
    )
    cached = service.refresh(
        realm_id=REALM,
        provider_id=PROVIDER,
        strategy=CatalogRefreshStrategy.ONLINE_IF_UNCACHED,
        client_version="zekam-test/1",
        ttl=dt.timedelta(hours=1),
        now=NOW,
    )
    assert offline.provider_called is False and cached.provider_called is False
    assert store.stored == []


def test_online_uses_etag_and_304_renews_exact_catalog() -> None:
    prior = _snapshot()
    store = Store(prior)
    permit, claim = _permit(CatalogRefreshStrategy.ONLINE, prior.snapshot_digest)
    fetcher = Fetcher(_response(304, claim, etag="etag-v1"))
    result = ModelCatalogService(store).refresh(
        realm_id=REALM,
        provider_id=PROVIDER,
        strategy=CatalogRefreshStrategy.ONLINE,
        client_version="zekam-test/1",
        ttl=dt.timedelta(hours=1),
        now=NOW + dt.timedelta(minutes=30),
        fetcher=fetcher,
        permit=permit,
    )
    assert fetcher.requests[0].etag == "etag-v1"
    assert result.snapshot is not None
    assert result.snapshot.fetch_status is CatalogFetchStatus.NOT_MODIFIED
    assert result.snapshot.catalog_digest == prior.catalog_digest
    assert result.snapshot.prior_snapshot_id == prior.id


def test_force_probe_skips_etag_and_failed_fetch_never_reuses_prior_entries() -> None:
    prior = _snapshot()
    store = Store(prior)
    permit, claim = _permit(CatalogRefreshStrategy.FORCE_PROBE, prior.snapshot_digest)
    fetcher = Fetcher(_response(503, claim, error_category="provider-unavailable"))
    result = ModelCatalogService(store).refresh(
        realm_id=REALM,
        provider_id=PROVIDER,
        strategy=CatalogRefreshStrategy.FORCE_PROBE,
        client_version="zekam-test/1",
        ttl=dt.timedelta(hours=1),
        now=NOW + dt.timedelta(minutes=1),
        fetcher=fetcher,
        permit=permit,
    )
    assert fetcher.requests[0].etag is None and fetcher.requests[0].force_probe
    assert result.snapshot is not None and result.snapshot.entries == ()
    assert observe_availability(result.snapshot, "model-a", now=NOW).reason == (
        "catalog-fetch-failed"
    )


def test_online_if_uncached_fetches_and_cache_write_failure_is_visible() -> None:
    store = Store(fail_write=True)
    permit, claim = _permit(CatalogRefreshStrategy.ONLINE_IF_UNCACHED)
    fetcher = Fetcher(_response(200, claim, entries=(_entry(),), etag="etag-v2"))
    result = ModelCatalogService(store).refresh(
        realm_id=REALM,
        provider_id=PROVIDER,
        strategy=CatalogRefreshStrategy.ONLINE_IF_UNCACHED,
        client_version="zekam-test/1",
        ttl=dt.timedelta(hours=1),
        now=NOW,
        fetcher=fetcher,
        permit=permit,
    )
    assert result.provider_called is True and result.persisted is False
    assert result.disposition == "observed-cache-write-failed"
    assert result.warning == "cache-write:OSError"
    assert result.snapshot is not None and result.snapshot.catalog_digest


def test_catalog_schema_and_permit_fail_closed() -> None:
    with pytest.raises(ValidationFailed):
        ModelCatalogEntry("model-a", CatalogVisibility.RESTRICTED, False, "chat-completions")
    snapshot = _snapshot()
    with pytest.raises(PolicyViolation):
        replace(snapshot, grants_authority=True)
    with pytest.raises(PolicyViolation):
        CatalogFetchPermit(
            PROVIDER,
            digest("plan"),
            uuid4(),
            digest("claim"),
            uuid4(),
            digest("authorization"),
            object(),
        )


def test_refresh_rejects_permit_from_another_strategy() -> None:
    permit, claim = _permit(CatalogRefreshStrategy.ONLINE)
    with pytest.raises(PolicyViolation, match="exact plan"):
        ModelCatalogService(Store()).refresh(
            realm_id=REALM,
            provider_id=PROVIDER,
            strategy=CatalogRefreshStrategy.FORCE_PROBE,
            client_version="zekam-test/1",
            ttl=dt.timedelta(hours=1),
            now=NOW,
            fetcher=Fetcher(_response(200, claim, entries=(_entry(),))),
            permit=permit,
        )


def test_remote_refresh_rejects_missing_or_drifted_terminal_receipt() -> None:
    permit, claim = _permit(CatalogRefreshStrategy.ONLINE)
    service = ModelCatalogService(Store())
    with pytest.raises(PolicyViolation, match="terminal receipt"):
        service.refresh(
            realm_id=REALM,
            provider_id=PROVIDER,
            strategy=CatalogRefreshStrategy.ONLINE,
            client_version="zekam-test/1",
            ttl=dt.timedelta(hours=1),
            now=NOW,
            fetcher=Fetcher(CatalogFetchResponse(200, (_entry(),))),
            permit=permit,
        )
    drifted_receipt = EffectReceipt.completed(
        realm_id=REALM,
        claim=claim,
        result_digest=digest("wrong-response"),
        adapter_evidence_digest=digest("catalog-adapter-evidence"),
        now=NOW,
    )
    with pytest.raises(PolicyViolation, match="response digest"):
        service.refresh(
            realm_id=REALM,
            provider_id=PROVIDER,
            strategy=CatalogRefreshStrategy.ONLINE,
            client_version="zekam-test/1",
            ttl=dt.timedelta(hours=1),
            now=NOW,
            fetcher=Fetcher(CatalogFetchResponse(200, (_entry(),), receipt=drifted_receipt)),
            permit=permit,
        )


def test_remote_snapshot_and_provider_scope_fail_closed() -> None:
    with pytest.raises(ValidationFailed, match="fetch provenance"):
        replace(_snapshot(), fetch_provenance=None)
    plan = catalog_refresh_plan_digest(
        provider_id=PROVIDER,
        strategy=CatalogRefreshStrategy.ONLINE,
        client_version="zekam-test/1",
        ttl_seconds=3600,
        prior_snapshot_digest=None,
    )
    authorization = Authorization.issue(
        realm_id=REALM,
        actor_id=uuid4(),
        plan_digest=plan,
        effect_digest=plan,
        scope=AuthorizationScope(
            allowed_resources=(f"provider.catalog:{PROVIDER}",),
            allowed_effects=("model-catalog-refresh",),
        ),
        risk="low",
        lifetime=dt.timedelta(minutes=10),
        now=NOW,
    )
    claim = EffectClaim.create(
        realm_id=REALM,
        job_id=uuid4(),
        attempt_id=uuid4(),
        operation="model-catalog-refresh",
        effect_digest=plan,
        authorization_digest=authorization.authorization_digest,
        idempotency_key="missing-provider-scope",
        resources=(),
        execution_identity="catalog-worker",
        fencing_token=1,
        adapter_digest=digest("catalog-adapter"),
        now=NOW,
    )
    with pytest.raises(PolicyViolation, match="exact binding"):
        authorize_catalog_fetch(
            provider_id=PROVIDER,
            plan_digest=plan,
            authorization=authorization,
            claim=claim,
        )
