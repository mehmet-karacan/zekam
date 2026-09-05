"""PostgreSQL metadata persistence for encrypted Diagnostic Trace Plane."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from zekam.application.diagnostic_trace import TraceEventMetadata
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.diagnostic_trace import (
    DiagnosticTracePolicy,
    ReducedTrace,
    TraceBundle,
    TraceEventRecord,
    TraceEventType,
    TracePurgeCandidate,
    TraceVisibility,
)
from zekam.domain.errors import PolicyViolation


def _json(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def _policy_from_body(body: dict[str, Any]) -> DiagnosticTracePolicy:
    return DiagnosticTracePolicy(
        enabled=bool(body["enabled"]),
        retention_days=int(body["retention_days"]),
        max_payload_bytes=int(body["max_payload_bytes"]),
        max_events=int(body["max_events"]),
        max_total_bytes=int(body["max_total_bytes"]),
        encryption_key_ref=str(body["encryption_key_ref"]),
        export_allowed=bool(body["export_allowed"]),
        redaction_profile=str(body["redaction_profile"]),
    )


@dataclass(frozen=True, slots=True)
class PostgresDiagnosticTraceRepository:
    connection: Any
    realm_id: UUID

    def _audit(
        self,
        cursor: Any,
        *,
        trace_id: UUID,
        operation: str,
        authorization_ref: str | None = None,
    ) -> None:
        event_id = uuid4()
        occurred_at = dt.datetime.now(dt.UTC)
        body = {
            "schema": "zekam-diagnostic-access-event/v1",
            "id": str(event_id),
            "realm_id": str(self.realm_id),
            "trace_id": str(trace_id),
            "operation": operation,
            "actor_ref": "zekam-diagnostic-service",
            "authorization_ref": authorization_ref,
            "occurred_at": occurred_at,
        }
        cursor.execute(
            "insert into diagnostics.access_event"
            "(id,realm_id,trace_id,operation,actor_ref,authorization_ref,occurred_at,event_digest)"
            " values(%s,%s,%s,%s,'zekam-diagnostic-service',%s,%s,%s)",
            (
                event_id,
                self.realm_id,
                trace_id,
                operation,
                authorization_ref,
                occurred_at,
                digest(body),
            ),
        )

    def create_bundle(self, bundle: TraceBundle) -> tuple[UUID, bool]:
        if bundle.realm_id != self.realm_id:
            raise PolicyViolation("Diagnostic trace cross-realm yazma reddedildi")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into diagnostics.trace_bundle"
                "(id,realm_id,trace_ref,project_id,work_item_id,run_id,root_assignment_id,"
                "root_client_session_id,policy_digest,policy_body,manifest_digest,manifest_body,"
                "state,created_at,expires_at,grants_authority)"
                " values(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,'open',%s,%s,false)"
                " on conflict(realm_id,manifest_digest) do nothing returning id",
                (
                    bundle.id,
                    self.realm_id,
                    bundle.trace_ref,
                    bundle.project_id,
                    bundle.work_item_id,
                    bundle.run_id,
                    bundle.root_assignment_id,
                    bundle.root_client_session_id,
                    bundle.policy.policy_digest,
                    _json(bundle.policy.body()),
                    bundle.manifest_digest,
                    _json(bundle.manifest_body()),
                    bundle.created_at,
                    bundle.expires_at,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                "select id from diagnostics.trace_bundle where realm_id=%s and manifest_digest=%s",
                (self.realm_id, bundle.manifest_digest),
            )
            return UUID(str(cursor.fetchone()[0])), False

    def usage(self, bundle_id: UUID) -> tuple[int, int]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select count(*),coalesce(sum(payload.plain_size_bytes),0)"
                " from diagnostics.trace_event event join diagnostics.payload_ref payload"
                " on payload.realm_id=event.realm_id and payload.id=event.payload_ref_id"
                " where event.realm_id=%s and event.trace_id=%s",
                (self.realm_id, bundle_id),
            )
            row = cursor.fetchone()
            return int(row[0]), int(row[1])

    def append_event(self, bundle: TraceBundle, metadata: TraceEventMetadata) -> TraceEventRecord:
        if bundle.realm_id != self.realm_id:
            raise PolicyViolation("Diagnostic trace cross-realm event reddedildi")
        expected_receipt = {
            "schema": "zekam-trace-payload-durability-receipt/v2",
            "trace_id": str(bundle.id),
            "event_id": str(metadata.id),
            "object": {
                "digest": metadata.payload_ref,
                "size_bytes": metadata.durability_receipt["object"]["size_bytes"],
                "stored_at": metadata.durability_receipt["object"]["stored_at"],
                "media_type": "application/vnd.zekam.trace+ciphertext",
                "metadata": {"cipher": "aes-256-gcm", "purpose": "diagnostic-trace"},
            },
            "durable_before_event": True,
        }
        if (
            metadata.durability_receipt != expected_receipt
            or metadata.durability_receipt_digest != digest(expected_receipt)
        ):
            raise PolicyViolation("CAS-issued durability receipt exact binding mismatch")
        payload_id = uuid4()
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select out_sequence,out_event_digest from diagnostics.append_trace_event("
                "%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)",
                (
                    bundle.id,
                    metadata.id,
                    payload_id,
                    metadata.event_type.value,
                    metadata.visibility.value,
                    metadata.occurred_at,
                    _json(metadata.correlation),
                    metadata.payload_ref,
                    metadata.payload_cipher_digest,
                    metadata.payload_plain_digest,
                    metadata.payload_size_bytes,
                    metadata.durability_receipt["object"]["size_bytes"],
                    metadata.encryption_key_ref,
                    metadata.redaction_digest,
                    _json(metadata.durability_receipt),
                    metadata.durability_receipt_digest,
                ),
            )
            sequence, event_digest = cursor.fetchone()
            previous = None
            if int(sequence) > 1:
                cursor.execute(
                    "select event_digest from diagnostics.trace_event"
                    " where realm_id=%s and trace_id=%s and sequence=%s",
                    (self.realm_id, bundle.id, int(sequence) - 1),
                )
                previous = str(cursor.fetchone()[0])
            event = TraceEventRecord(
                id=metadata.id,
                realm_id=self.realm_id,
                bundle_id=bundle.id,
                sequence=int(sequence),
                event_type=metadata.event_type,
                visibility=metadata.visibility,
                occurred_at=metadata.occurred_at,
                correlation=metadata.correlation,
                payload_ref=metadata.payload_ref,
                payload_cipher_digest=metadata.payload_cipher_digest,
                payload_plain_digest=metadata.payload_plain_digest,
                payload_size_bytes=metadata.payload_size_bytes,
                encryption_key_ref=metadata.encryption_key_ref,
                redaction_digest=metadata.redaction_digest,
                previous_event_digest=previous,
                event_digest=str(event_digest),
            )
            return event

    def list_events(
        self, bundle_id: UUID, *, authorization_ref: str | None = None
    ) -> tuple[TraceEventRecord, ...]:
        with self.connection.transaction(), self.connection.cursor() as cursor:
            self._audit(
                cursor,
                trace_id=bundle_id,
                operation="read",
                authorization_ref=authorization_ref,
            )
            cursor.execute(
                "select event.id,event.sequence,event.event_type,event.visibility,"
                "event.occurred_at,"
                "event.correlation,payload.object_digest,payload.cipher_digest,"
                "payload.plain_digest,payload.plain_size_bytes,"
                "payload.encryption_key_ref,payload.redaction_digest,event.previous_event_digest,"
                "event.event_digest from diagnostics.trace_event event"
                " join diagnostics.payload_ref payload"
                " on payload.realm_id=event.realm_id and payload.id=event.payload_ref_id"
                " where event.realm_id=%s and event.trace_id=%s order by event.sequence",
                (self.realm_id, bundle_id),
            )
            return tuple(
                TraceEventRecord(
                    id=UUID(str(row[0])),
                    realm_id=self.realm_id,
                    bundle_id=bundle_id,
                    sequence=int(row[1]),
                    event_type=TraceEventType(str(row[2])),
                    visibility=TraceVisibility(str(row[3])),
                    occurred_at=row[4],
                    correlation=dict(row[5]),
                    payload_ref=str(row[6]),
                    payload_cipher_digest=str(row[7]),
                    payload_plain_digest=str(row[8]),
                    payload_size_bytes=int(row[9]),
                    encryption_key_ref=str(row[10]),
                    redaction_digest=str(row[11]),
                    previous_event_digest=None if row[12] is None else str(row[12]),
                    event_digest=str(row[13]),
                )
                for row in cursor.fetchall()
            )

    def store_reduction(
        self, reduced: ReducedTrace, *, authorization_ref: str | None = None
    ) -> tuple[UUID, bool]:
        reduction_id = uuid4()
        with self.connection.transaction(), self.connection.cursor() as cursor:
            self._audit(
                cursor,
                trace_id=reduced.bundle_id,
                operation="reduce",
                authorization_ref=authorization_ref,
            )
            cursor.execute(
                "select out_id,out_created from diagnostics.store_reduction("
                "%s,%s,%s,%s,%s,%s::jsonb,%s)",
                (
                    reduction_id,
                    reduced.bundle_id,
                    reduced.event_count,
                    reduced.last_event_digest,
                    reduced.output_digest,
                    _json(reduced.body()),
                    reduced.reduced_at,
                ),
            )
            row = cursor.fetchone()
            return UUID(str(row[0])), bool(row[1])

    def get_bundle(self, bundle_id: UUID) -> TraceBundle:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select trace_ref,project_id,work_item_id,run_id,root_assignment_id,"
                "root_client_session_id,policy_body,created_at,expires_at,state"
                " from diagnostics.trace_bundle where realm_id=%s and id=%s",
                (self.realm_id, bundle_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise PolicyViolation("Diagnostic trace bundle bulunamadi")
            body = row[6] if isinstance(row[6], dict) else json.loads(row[6])
            return TraceBundle(
                id=bundle_id,
                realm_id=self.realm_id,
                trace_ref=str(row[0]),
                project_id=None if row[1] is None else UUID(str(row[1])),
                work_item_id=None if row[2] is None else UUID(str(row[2])),
                run_id=None if row[3] is None else UUID(str(row[3])),
                root_assignment_id=None if row[4] is None else UUID(str(row[4])),
                root_client_session_id=str(row[5]),
                policy=_policy_from_body(dict(body)),
                created_at=row[7],
                expires_at=row[8],
                state=str(row[9]),
            )

    def close(self, bundle_id: UUID) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute("select diagnostics.close_trace(%s)", (bundle_id,))
            if cursor.fetchone()[0] is not True:
                raise PolicyViolation("Diagnostic trace open/current degil")

    def expired_candidates(
        self, *, now: dt.datetime, limit: int
    ) -> tuple[TracePurgeCandidate, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select bundle.id,bundle.expires_at,"
                "coalesce(array_agg(payload.object_digest order by payload.object_digest)"
                " filter(where payload.object_digest is not null),'{}'::text[])"
                " from diagnostics.trace_bundle bundle left join diagnostics.payload_ref payload"
                " on payload.realm_id=bundle.realm_id and payload.trace_id=bundle.id"
                " where bundle.realm_id=%s and bundle.state in ('open','closed','expired')"
                " and bundle.expires_at<=%s group by bundle.id,bundle.expires_at"
                " order by bundle.expires_at,bundle.id limit %s",
                (self.realm_id, now, limit),
            )
            return tuple(
                TracePurgeCandidate(
                    bundle_id=UUID(str(row[0])),
                    expires_at=row[1],
                    payload_refs=tuple(str(item) for item in row[2]),
                )
                for row in cursor.fetchall()
            )

    def mark_purged(
        self,
        bundle_id: UUID,
        *,
        purged_at: dt.datetime,
        purge_receipt_digest: str,
        authorization_ref: str,
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select diagnostics.purge_trace(%s,%s,%s,%s)",
                (bundle_id, purged_at, purge_receipt_digest, authorization_ref),
            )
            if cursor.fetchone()[0] is not True:
                raise PolicyViolation("Diagnostic trace expired/current purge adayi degil")
