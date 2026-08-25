"""PostgreSQL persistence for append-only model catalog observations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_bytes
from zekam.domain.errors import PolicyViolation
from zekam.domain.model_catalog import (
    CatalogFetchProvenance,
    CatalogFetchStatus,
    CatalogReceiptStatus,
    CatalogRefreshStrategy,
    CatalogSource,
    CatalogVisibility,
    ModelCatalogEntry,
    ModelCatalogSnapshot,
)


def _json(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def _snapshot(row: Any) -> ModelCatalogSnapshot:
    entries_doc = row[8] if isinstance(row[8], list) else json.loads(row[8])
    entries = tuple(
        ModelCatalogEntry(
            model_id=str(item["model_id"]),
            visibility=CatalogVisibility(str(item["visibility"])),
            authentication_required=bool(item["authentication_required"]),
            endpoint_class=str(item["endpoint_class"]),
            capabilities=tuple(str(value) for value in item["capabilities"]),
        )
        for item in entries_doc
    )
    provenance_doc = (
        row[13] if isinstance(row[13], dict) else (None if row[13] is None else json.loads(row[13]))
    )
    provenance = (
        None
        if provenance_doc is None
        else CatalogFetchProvenance(
            plan_digest=str(provenance_doc["plan_digest"]),
            strategy=CatalogRefreshStrategy(str(provenance_doc["strategy"])),
            ttl_seconds=int(provenance_doc["ttl_seconds"]),
            prior_snapshot_digest=(
                None
                if provenance_doc["prior_snapshot_digest"] is None
                else str(provenance_doc["prior_snapshot_digest"])
            ),
            authorization_id=UUID(str(provenance_doc["authorization_id"])),
            authorization_digest=str(provenance_doc["authorization_digest"]),
            claim_id=UUID(str(provenance_doc["claim_id"])),
            claim_digest=str(provenance_doc["claim_digest"]),
            receipt_id=UUID(str(provenance_doc["receipt_id"])),
            receipt_status=CatalogReceiptStatus(str(provenance_doc["receipt_status"])),
            status_code=int(provenance_doc["status_code"]),
            response_etag=(
                None
                if provenance_doc["response_etag"] is None
                else str(provenance_doc["response_etag"])
            ),
            response_digest=str(provenance_doc["response_digest"]),
            adapter_evidence_digest=str(provenance_doc["adapter_evidence_digest"]),
        )
    )
    snapshot = ModelCatalogSnapshot(
        id=UUID(str(row[0])),
        realm_id=UUID(str(row[1])),
        provider_id=str(row[2]),
        entries=entries,
        etag=None if row[4] is None else str(row[4]),
        fetched_at=row[5],
        expires_at=row[6],
        client_version=str(row[7]),
        source=CatalogSource(str(row[9])),
        fetch_status=CatalogFetchStatus(str(row[10])),
        error_category=None if row[11] is None else str(row[11]),
        prior_snapshot_id=None if row[12] is None else UUID(str(row[12])),
        fetch_provenance=provenance,
    )
    snapshot.assert_digest(str(row[3]))
    return snapshot


_COLUMNS = (
    "id,realm_id,provider_id,snapshot_digest,etag,fetched_at,expires_at,client_version,"
    "entries,source,fetch_status,error_category,prior_snapshot_id"
    ",fetch_provenance"
)


@dataclass(frozen=True, slots=True)
class ModelCatalogRepository:
    connection: Any
    realm_id: UUID

    def store(self, snapshot: ModelCatalogSnapshot) -> tuple[UUID, bool]:
        if snapshot.realm_id != self.realm_id:
            raise PolicyViolation("Catalog snapshot cross-realm reddedildi")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.catalog_snapshot"
                "(id,realm_id,provider_id,catalog_digest,snapshot_digest,etag,fetched_at,"
                "expires_at,client_version,source,entries,fetch_status,error_category,"
                "prior_snapshot_id,fetch_provenance,manifest_body,grants_authority)"
                " values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,"
                "%s::jsonb,%s::jsonb,false)"
                " on conflict(realm_id,snapshot_digest) do nothing returning id",
                (
                    snapshot.id,
                    self.realm_id,
                    snapshot.provider_id,
                    snapshot.catalog_digest,
                    snapshot.snapshot_digest,
                    snapshot.etag,
                    snapshot.fetched_at,
                    snapshot.expires_at,
                    snapshot.client_version,
                    snapshot.source.value,
                    _json([item.body() for item in snapshot.entries]),
                    snapshot.fetch_status.value,
                    snapshot.error_category,
                    snapshot.prior_snapshot_id,
                    None
                    if snapshot.fetch_provenance is None
                    else _json(snapshot.fetch_provenance.body()),
                    _json(snapshot.manifest_body()),
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                "select id from models.catalog_snapshot where realm_id=%s and snapshot_digest=%s",
                (self.realm_id, snapshot.snapshot_digest),
            )
            return UUID(str(cursor.fetchone()[0])), False

    def get(self, snapshot_id: UUID) -> ModelCatalogSnapshot:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_COLUMNS} from models.catalog_snapshot where realm_id=%s and id=%s",
                (self.realm_id, snapshot_id),
            )
            row = cursor.fetchone()
        if row is None:
            from zekam.domain.errors import NotFound

            raise NotFound("Model catalog snapshot bulunamadi")
        return _snapshot(row)

    def latest(self, provider_id: str) -> ModelCatalogSnapshot | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_COLUMNS} from models.catalog_snapshot"
                " where realm_id=%s and provider_id=%s"
                " order by fetched_at desc,id desc limit 1",
                (self.realm_id, provider_id),
            )
            row = cursor.fetchone()
        return None if row is None else _snapshot(row)

    def history(self, provider_id: str, *, limit: int = 50) -> tuple[ModelCatalogSnapshot, ...]:
        if not 1 <= limit <= 1000:
            raise PolicyViolation("Catalog history limit 1..1000 olmali")
        with self.connection.cursor() as cursor:
            cursor.execute(
                f"select {_COLUMNS} from models.catalog_snapshot"
                " where realm_id=%s and provider_id=%s"
                " order by fetched_at desc,id desc limit %s",
                (self.realm_id, provider_id, limit),
            )
            return tuple(_snapshot(row) for row in cursor.fetchall())
