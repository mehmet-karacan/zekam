from __future__ import annotations

import datetime as dt
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.test_model_catalog import _entry, _snapshot

from zekam.application.model_catalog import catalog_refresh_plan_digest
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_catalog import (
    CatalogAvailability,
    CatalogFetchProvenance,
    CatalogFetchStatus,
    CatalogReceiptStatus,
    CatalogRefreshStrategy,
    CatalogSource,
    CatalogVisibility,
    ModelCatalogEntry,
    ModelCatalogSnapshot,
    catalog_fetch_response_digest,
)

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)


def _static(**changes: Any) -> ModelCatalogSnapshot:
    values: dict[str, Any] = {
        "id": uuid4(),
        "realm_id": uuid4(),
        "provider_id": "static-provider",
        "entries": (_entry(),),
        "etag": None,
        "fetched_at": NOW,
        "expires_at": NOW + dt.timedelta(hours=1),
        "client_version": "test/1",
        "source": CatalogSource.STATIC,
        "fetch_status": CatalogFetchStatus.FETCHED,
        "error_category": None,
    }
    values.update(changes)
    return ModelCatalogSnapshot(**values)


def _remote(
    *,
    fetch_status: CatalogFetchStatus = CatalogFetchStatus.FETCHED,
    status_code: int = 200,
    prior: bool = False,
    error_category: str | None = None,
) -> ModelCatalogSnapshot:
    entries = (_entry(),) if fetch_status is not CatalogFetchStatus.FAILED else ()
    prior_snapshot_id = uuid4() if prior else None
    prior_digest = digest("prior") if prior else None
    ttl = 3600
    strategy = CatalogRefreshStrategy.ONLINE
    plan = catalog_refresh_plan_digest(
        provider_id="remote-provider",
        strategy=strategy,
        client_version="test/1",
        ttl_seconds=ttl,
        prior_snapshot_digest=prior_digest,
    )
    raw_entries = entries if fetch_status is CatalogFetchStatus.FETCHED else ()
    response = catalog_fetch_response_digest(
        status_code=status_code,
        entries=raw_entries,
        etag="etag",
        error_category=error_category,
    )
    receipt_status = (
        CatalogReceiptStatus.COMPLETED if status_code in {200, 304} else CatalogReceiptStatus.FAILED
    )
    provenance = CatalogFetchProvenance(
        plan,
        strategy,
        ttl,
        prior_digest,
        uuid4(),
        digest("authorization"),
        uuid4(),
        digest("claim"),
        uuid4(),
        receipt_status,
        status_code,
        "etag",
        response,
        digest("adapter"),
    )
    return ModelCatalogSnapshot(
        uuid4(),
        uuid4(),
        "remote-provider",
        entries,
        "etag",
        NOW,
        NOW + dt.timedelta(seconds=ttl),
        "test/1",
        CatalogSource.REMOTE,
        fetch_status,
        error_category,
        prior_snapshot_id,
        provenance,
    )


@pytest.mark.parametrize("value", [" ", " padded", "x" * 257, "https://host", "bad\\path"])
def test_catalog_entry_rejects_nonportable_identity(value: str) -> None:
    with pytest.raises(ValidationFailed):
        ModelCatalogEntry(value, CatalogVisibility.PUBLIC, False, "endpoint")


def test_catalog_entry_rejects_noncanonical_capabilities() -> None:
    with pytest.raises(ValidationFailed):
        ModelCatalogEntry("model", CatalogVisibility.PUBLIC, False, "endpoint", ("z", "a"))


@pytest.mark.parametrize(
    "changes",
    [
        {"ttl_seconds": 59},
        {"ttl_seconds": 7 * 24 * 60 * 60 + 1},
        {"strategy": CatalogRefreshStrategy.OFFLINE},
        {"status_code": 99},
        {"status_code": 600},
        {"receipt_status": CatalogReceiptStatus.FAILED},
    ],
)
def test_fetch_provenance_rejects_invalid_ttl_strategy_status_and_receipt(
    changes: dict[str, Any],
) -> None:
    provenance = _snapshot().fetch_provenance
    assert provenance is not None
    with pytest.raises(ValidationFailed):
        replace(provenance, **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"fetched_at": NOW.replace(tzinfo=None)},
        {"expires_at": NOW.replace(tzinfo=None)},
        {"expires_at": NOW},
        {"entries": (_entry("z"), _entry("a"))},
        {"fetch_status": CatalogFetchStatus.FAILED},
        {"entries": ()},
        {"error_category": "unexpected"},
        {"fetch_status": CatalogFetchStatus.NOT_MODIFIED},
        {
            "fetch_status": CatalogFetchStatus.NOT_MODIFIED,
            "prior_snapshot_id": uuid4(),
        },
        {"etag": "etag"},
    ],
)
def test_snapshot_rejects_invalid_time_entries_terminal_and_static_metadata(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationFailed):
        _static(**changes)


def test_remote_snapshot_rejects_plan_ttl_prior_response_and_status_drift() -> None:
    snapshot = _remote()
    provenance = snapshot.fetch_provenance
    assert provenance is not None
    with pytest.raises(PolicyViolation, match="plan provenance"):
        replace(snapshot, fetch_provenance=replace(provenance, plan_digest=digest("forged")))
    with pytest.raises(ValidationFailed, match="TTL"):
        replace(snapshot, expires_at=snapshot.expires_at + dt.timedelta(seconds=1))
    with pytest.raises(ValidationFailed, match="prior snapshot"):
        replace(snapshot, prior_snapshot_id=uuid4())
    with pytest.raises(PolicyViolation, match="response provenance"):
        replace(
            snapshot,
            fetch_provenance=replace(provenance, response_digest=digest("forged")),
        )

    with pytest.raises(ValidationFailed, match="status/provenance"):
        _remote(status_code=304)


def test_failed_remote_snapshot_rejects_success_provider_status() -> None:
    with pytest.raises(ValidationFailed, match="basarili provider statusu"):
        _remote(
            fetch_status=CatalogFetchStatus.FAILED,
            status_code=200,
            error_category="provider-failed",
        )


def test_snapshot_digest_and_availability_fail_closed() -> None:
    snapshot = _static()
    snapshot.assert_digest(snapshot.snapshot_digest)
    with pytest.raises(ValidationFailed):
        snapshot.assert_digest("bad")
    with pytest.raises(PolicyViolation):
        snapshot.assert_digest(digest("forged"))
    with pytest.raises(ValidationFailed):
        CatalogAvailability("model", True, "unexpected", None, None)
    with pytest.raises(PolicyViolation):
        CatalogAvailability("model", True, None, None, None, grants_authority=True)
