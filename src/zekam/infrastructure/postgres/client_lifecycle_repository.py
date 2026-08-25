"""Canonical OpenCode lifecycle ingest and acknowledgement repository."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.clients import ClientKind
from zekam.domain.errors import ConcurrencyConflict, ValidationFailed
from zekam.domain.identifiers import new_uuid7


@dataclass(frozen=True, slots=True)
class LifecycleAck:
    event_id: UUID
    local_event_digest: str
    canonical_digest: str
    acknowledged_at: dt.datetime
    compaction_outbox_id: UUID | None = None
    compaction_payload_digest: str | None = None

    def as_dict(self) -> dict[str, str]:
        result = {
            "event_id": str(self.event_id),
            "local_event_digest": self.local_event_digest,
            "canonical_digest": self.canonical_digest,
            "acknowledged_at": self.acknowledged_at.isoformat(),
        }
        if self.compaction_outbox_id is not None:
            result["compaction_outbox_id"] = str(self.compaction_outbox_id)
            result["compaction_payload_digest"] = str(self.compaction_payload_digest)
        return result


@dataclass(frozen=True, slots=True)
class ClientLifecycleRepository:
    connection: Any
    realm_id: UUID

    def ingest(
        self,
        document: Mapping[str, Any],
        *,
        client_instance_id: str,
        client_kind: ClientKind | None = None,
        now: dt.datetime | None = None,
    ) -> LifecycleAck:
        local_digest = str(document.get("event_digest", ""))
        parse_digest(local_digest)
        body = {key: value for key, value in document.items() if key != "event_digest"}
        if digest(body) != local_digest:
            raise ValidationFailed("Lifecycle supplied digest canonical body ile uyusmuyor")
        schema = body.get("schema")
        if schema == "zekam-opencode-lifecycle-event/v2":
            observed_kind = ClientKind.OPENCODE
        elif schema == "zekam-client-lifecycle-event/v1":
            expected_fields = {
                "schema",
                "client_id",
                "client_kind",
                "session_id",
                "sequence",
                "previous_digest",
                "event_type",
                "payload_digest",
                "occurred_at",
                "transcript_included",
                "grants_authority",
            }
            if set(body) != expected_fields:
                raise ValidationFailed("Canonical lifecycle schema disi alan tasiyor")
            try:
                observed_kind = ClientKind(str(body.get("client_kind")))
            except ValueError as exc:
                raise ValidationFailed("Canonical lifecycle client kind gecersiz") from exc
            if (
                body.get("transcript_included") is not False
                or body.get("grants_authority") is not False
            ):
                raise ValidationFailed("Canonical lifecycle transcript/authority tasiyamaz")
            if body.get("client_id") != client_instance_id:
                raise ValidationFailed("Canonical lifecycle client identity binding uyusmuyor")
        else:
            raise ValidationFailed("Canonical ingest desteklenen lifecycle schema ister")
        if client_kind is not None and client_kind is not observed_kind:
            raise ValidationFailed("Lifecycle client kind binding uyusmuyor")
        sequence = int(body["sequence"])
        previous = body.get("previous_digest")
        session_id = str(body["session_id"])
        occurred_at = dt.datetime.fromisoformat(str(body["occurred_at"]))
        if occurred_at.tzinfo is None:
            raise ValidationFailed("Canonical lifecycle zamani timezone-aware olmali")
        acknowledged_at = now or dt.datetime.now(dt.UTC)

        # connect() autocommit kullanir; stream head, event ve ACK tek transaction olmadan
        # crash sonrasi ayrisabilir ve replay kalici head mismatch'e dusebilir.
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "select e.id,a.canonical_digest,a.acknowledged_at,o.id,o.payload_digest"
                " from client.lifecycle_event e join client.lifecycle_ack a"
                " on a.realm_id=e.realm_id and a.event_id=e.id"
                " left join work.compaction_checkpoint_outbox o"
                " on o.realm_id=e.realm_id and o.lifecycle_event_id=e.id"
                " where e.realm_id=%s and e.event_digest=%s",
                (self.realm_id, local_digest),
            )
            replay = cursor.fetchone()
            if replay is not None:
                return LifecycleAck(
                    UUID(str(replay[0])),
                    local_digest,
                    str(replay[1]),
                    replay[2],
                    None if replay[3] is None else UUID(str(replay[3])),
                    None if replay[4] is None else str(replay[4]),
                )

            cursor.execute(
                "select id,head_sequence,head_digest from client.lifecycle_stream"
                " where realm_id=%s and client_instance_id=%s and session_id=%s for update",
                (self.realm_id, client_instance_id, session_id),
            )
            stream = cursor.fetchone()
            if stream is None:
                if sequence != 1 or previous is not None:
                    raise ConcurrencyConflict("Lifecycle stream ilk sequence/previous gecersiz")
                stream_id = new_uuid7(now=acknowledged_at)
                cursor.execute(
                    "insert into client.lifecycle_stream"
                    " (id,realm_id,client_kind,client_instance_id,session_id,head_sequence,"
                    " head_digest,created_at,updated_at)"
                    " values (%s,%s,%s,%s,%s,0,null,%s,%s)",
                    (
                        stream_id,
                        self.realm_id,
                        observed_kind.value,
                        client_instance_id,
                        session_id,
                        acknowledged_at,
                        acknowledged_at,
                    ),
                )
                head_sequence, head_digest = 0, None
            else:
                stream_id = UUID(str(stream[0]))
                head_sequence, head_digest = int(stream[1]), stream[2]
            if sequence != head_sequence + 1 or previous != head_digest:
                raise ConcurrencyConflict("Lifecycle stream head/previous mismatch")

            event_id = new_uuid7(now=acknowledged_at)
            cursor.execute(
                "insert into client.lifecycle_event"
                " (id,realm_id,stream_id,sequence,previous_digest,event_digest,payload,"
                " occurred_at,ingested_at,grants_authority)"
                " values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,false)",
                (
                    event_id,
                    self.realm_id,
                    stream_id,
                    sequence,
                    previous,
                    local_digest,
                    canonical_json(body),
                    occurred_at,
                    acknowledged_at,
                ),
            )
            cursor.execute(
                "update client.lifecycle_stream set head_sequence=%s,head_digest=%s,updated_at=%s"
                " where realm_id=%s and id=%s",
                (sequence, local_digest, acknowledged_at, self.realm_id, stream_id),
            )
            canonical_digest = digest(
                {
                    "realm_id": self.realm_id,
                    "stream_id": stream_id,
                    "event_id": event_id,
                    "local_event_digest": local_digest,
                }
            )
            cursor.execute(
                "insert into client.lifecycle_ack"
                " (id,realm_id,event_id,local_event_digest,canonical_digest,acknowledged_at)"
                " values (%s,%s,%s,%s,%s,%s)",
                (
                    new_uuid7(now=acknowledged_at),
                    self.realm_id,
                    event_id,
                    local_digest,
                    canonical_digest,
                    acknowledged_at,
                ),
            )
            cursor.execute(
                "select id,payload_digest from work.compaction_checkpoint_outbox"
                " where realm_id=%s and lifecycle_event_id=%s",
                (self.realm_id, event_id),
            )
            compaction = cursor.fetchone()
        return LifecycleAck(
            event_id,
            local_digest,
            canonical_digest,
            acknowledged_at,
            None if compaction is None else UUID(str(compaction[0])),
            None if compaction is None else str(compaction[1]),
        )
