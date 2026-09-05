"""Durable historical control evidence without extending ordinary checkpoint coverage."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool, LifecycleSpoolEntry
from zekam.application.local_continuity import ContinuityBinding, ContinuityEvent, timestamp
from zekam.application.local_continuity_close import COMPILE_OPERATION, FrozenClose
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.local_continuity_decoder import validate_reviewed_control_entry
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_continuity_close import SQLiteCloseStore

_KINDS = {
    "session_start": "SESSION_START",
    "pre_compaction": "PRE_COMPACTION",
    "post_compaction": "POST_COMPACTION",
    "pre_close": "PRE_CLOSE",
    "post_close": "SESSION_CLOSED",
}


class SQLiteContinuityControlStore:
    def __init__(
        self,
        continuity: SQLiteContinuityStore,
        spool: ClientLifecycleSpool,
        *,
        entry_validator: Callable[[LifecycleSpoolEntry], None] = validate_reviewed_control_entry,
    ) -> None:
        if not isinstance(spool, ClientLifecycleSpool) or not callable(entry_validator):
            raise ValidationFailed("Control requires trusted disk spool and reviewed decoder")
        self.continuity, self.spool, self.entry_validator = continuity, spool, entry_validator

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(f"{self.continuity.path.resolve().as_uri()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("begin")
            yield db
        finally:
            db.close()

    @staticmethod
    def _historical_binding(db: sqlite3.Connection, binding: ContinuityBinding) -> sqlite3.Row:
        if not isinstance(binding, ContinuityBinding):
            raise ValidationFailed("Control typed historical binding required")
        binding.__post_init__()
        row: sqlite3.Row | None = db.execute(
            "select b.*,s.status,s.project_id as owner_project,"
            "s.work_item_id as owner_work,s.client_id as owner_client,s.device_id as owner_device,"
            "s.close_receipt_digest,s.closed_at from continuity_session_binding b"
            " join session s on s.id=b.session_id where b.session_id=?",
            (binding.session_id,),
        ).fetchone()
        if (
            row is None
            or row["binding_digest"] != binding.binding_digest
            or any(row[key] != value for key, value in asdict(binding).items())
            or (row["owner_project"], row["owner_work"], row["owner_client"], row["owner_device"])
            != (binding.project_id, binding.work_item_id, binding.client_id, binding.device_id)
        ):
            raise PolicyViolation("Control exact historical session binding drift")
        owner = db.execute(
            "select 1 from source_snapshot ss"
            " join source_binding sb on sb.id=ss.source_binding_id"
            " join project_knowledge_realm pr on pr.project_id=sb.project_id"
            " where ss.id=? and sb.project_id=? and pr.realm_id=?",
            (binding.source_snapshot_id, binding.project_id, binding.realm_id),
        ).fetchone()
        if owner is None:
            raise PolicyViolation("Control historical source/project/realm owner drift")
        if binding.run_id is not None:
            run = db.execute(
                "select 1 from run r join work_item w on w.id=r.work_item_id"
                " join config_revision c on c.id=r.config_revision_id where r.id=?"
                " and w.id=? and w.project_id=? and r.source_snapshot_id=?"
                " and r.plan_digest=? and c.task_digest=? and c.config_digest=?",
                (
                    binding.run_id,
                    binding.work_item_id,
                    binding.project_id,
                    binding.source_snapshot_id,
                    binding.plan_digest,
                    binding.task_digest,
                    binding.policy_digest,
                ),
            ).fetchone()
            if run is None:
                raise PolicyViolation("Control historical work/run/config owner drift")
        return row

    def is_frozen(self, binding: ContinuityBinding) -> bool:
        with self._read() as db:
            return self._historical_binding(db, binding)["status"] in {"closing", "closed"}

    def _frozen(
        self, db: sqlite3.Connection, binding: ContinuityBinding
    ) -> tuple[sqlite3.Row, sqlite3.Row, list[sqlite3.Row]]:
        session = self._historical_binding(db, binding)
        if session["status"] not in {"closing", "closed"}:
            raise PolicyViolation("Control requires an actually frozen session")
        request = db.execute(
            "select * from continuity_close_request where session_id=?", (binding.session_id,)
        ).fetchone()
        if request is None:
            raise PolicyViolation("Control frozen close request missing")
        checkpoint = db.execute(
            "select * from continuity_checkpoint where checkpoint_digest=?",
            (request["checkpoint_digest"],),
        ).fetchone()
        if checkpoint is None:
            raise PolicyViolation("Control frozen checkpoint missing")
        checkpoint_body = self.continuity._checkpoint_body(checkpoint, binding)
        rows = self.continuity._events(db, binding.session_id)
        try:
            body = json.loads(request["input_json"])
            if (
                canonical_json(body) != request["input_json"]
                or digest(body) != request["request_digest"]
                or body["binding_digest"] != binding.binding_digest
                or body["session_id"] != binding.session_id
                or body["checkpoint_digest"] != request["checkpoint_digest"]
                or body["manifest_digest"] != checkpoint_body["context_digest"]
                or body["covered_sequence"] != request["covered_sequence"]
                or body["covered_sequence"] != checkpoint_body["covered_sequence"]
                or body["created_at"] != request["created_at"]
                or body["covered_event_digest"] != checkpoint_body["covered_event_digest"]
                or len(rows) != body["covered_sequence"]
                or not rows
                or rows[-1]["event_digest"] != body["covered_event_digest"]
            ):
                raise PolicyViolation("Control frozen boundary/body integrity drift")
        except (ValueError, TypeError, KeyError) as exc:
            raise PolicyViolation("Control malformed frozen evidence") from exc
        link = db.execute(
            "select b.*,j.payload_json as job_payload,o.event_kind,"
            "o.payload_json as outbox_payload,o.payload_digest"
            " from continuity_outbox_binding b join local_job j on j.id=b.job_id"
            " join local_outbox o on o.id=b.outbox_id and o.job_id=j.id"
            " where b.close_request_digest=? and b.session_id=?",
            (request["request_digest"], binding.session_id),
        ).fetchone()
        if (
            link is None
            or link["input_digest"] != request["request_digest"]
            or link["event_kind"] != COMPILE_OPERATION
            or link["job_payload"]
            != canonical_json(SQLiteCloseStore._job_payload(binding, request["request_digest"]))
            or link["outbox_payload"]
            != canonical_json(SQLiteCloseStore._outbox_payload(binding, request["request_digest"]))
            or link["payload_digest"]
            != digest(SQLiteCloseStore._outbox_payload(binding, request["request_digest"]))
        ):
            raise PolicyViolation("Control frozen job/outbox binding drift")
        frozen = FrozenClose(
            request["request_digest"],
            link["job_id"],
            link["outbox_id"],
            body,
            "complete" if session["status"] == "closed" else "pending",
        )
        frozen.assert_integrity(binding)
        if session["status"] == "closed":
            receipt = db.execute(
                "select * from close_receipt where request_digest=? and session_id=?",
                (request["request_digest"], binding.session_id),
            ).fetchone()
            # Verify historical terminal evidence, not current sources or regenerated files.
            SQLiteCloseStore._terminal_effect(
                db, binding, frozen.job_id, frozen.effect_key, frozen.compile_evidence(binding)
            )
            delivery = SQLiteCloseStore._terminal_delivery(db, binding, frozen)
            projections = [item.evidence() for item in frozen.projections(binding)]
            receipt_body = {
                "request_digest": frozen.request_digest,
                "session_id": binding.session_id,
                "checkpoint_digest": body["checkpoint_digest"],
                "manifest_digest": body["manifest_digest"],
                "outbox_id": frozen.outbox_id,
                "projections": projections,
                "delivery_evidence_digest": delivery,
            }
            if (
                receipt is None
                or receipt["receipt_digest"] != session["close_receipt_digest"]
                or receipt["receipt_digest"] != digest(receipt_body)
                or any(
                    receipt[key] != receipt_body[key]
                    for key in (
                        "request_digest",
                        "session_id",
                        "checkpoint_digest",
                        "manifest_digest",
                        "outbox_id",
                    )
                )
                or receipt["projections_json"] != canonical_json(projections)
                or receipt["created_at"] != session["closed_at"]
            ):
                raise PolicyViolation("Control terminal session receipt integrity drift")
            timestamp(receipt["created_at"])
        return session, request, rows

    @staticmethod
    def _body(
        binding: ContinuityBinding, request_digest: str, entry: LifecycleSpoolEntry, created_at: str
    ) -> dict[str, Any]:
        timestamp(created_at)
        return {
            "schema": "zekam-continuity-control-event/v1",
            "session_id": binding.session_id,
            "binding_digest": binding.binding_digest,
            "request_digest": request_digest,
            "client_id": binding.client_id,
            "device_id": binding.device_id,
            "external_session_id": binding.external_session_id,
            "spool_digest": entry.entry_digest,
            "observation_digest": entry.observation_digest,
            "delivery_id": entry.delivery_id,
            "spool_sequence": entry.sequence,
            "previous_spool_digest": entry.previous_entry_digest,
            "external_event_type": entry.external_event_type,
            "internal_event_type": entry.internal_event_type,
            "disposition": (
                "advisory-post-close"
                if entry.internal_event_type == "post_close"
                else "rejected-after-freeze"
            ),
            "created_at": created_at,
            "grants_authority": False,
            "approval_inherited": False,
        }

    def _progress(
        self,
        db: sqlite3.Connection,
        binding: ContinuityBinding,
        entries: tuple[LifecycleSpoolEntry, ...],
    ) -> dict[str, Any]:
        session, request, rows = self._frozen(db, binding)
        ordinary = [row for row in rows if row["spool_digest"] is not None]
        if not ordinary or len(ordinary) > len(entries):
            raise PolicyViolation("Control required original spool prefix missing")
        for sequence, entry in enumerate(entries, 1):
            self.entry_validator(entry)
            if (
                entry.client_id != binding.client_id
                or entry.session_id != binding.external_session_id
                or entry.sequence != sequence
                or entry.previous_entry_digest
                != (entries[sequence - 2].entry_digest if sequence > 1 else None)
            ):
                raise PolicyViolation("Control original spool identity/sequence drift")
        if entries[0].internal_event_type != "session_start":
            raise PolicyViolation("Control required SESSION_START source missing")
        for row, entry in zip(ordinary, entries, strict=False):
            expected = ContinuityEvent(
                _KINDS.get(entry.internal_event_type, entry.internal_event_type),
                entry.delivery_id,
                entry.occurred_at.isoformat(),
                (),
                (entry.observation_digest,),
                entry.entry_digest,
            )
            if row["spool_digest"] != entry.entry_digest or canonical_json(
                json.loads(row["body_json"])["event"]
            ) != canonical_json(expected.body()):
                raise PolicyViolation("Control ordinary/source observation parity drift")
        cp = db.execute(
            "select spool_digest from continuity_checkpoint where checkpoint_digest=?",
            (request["checkpoint_digest"],),
        ).fetchone()
        if cp[0] != digest(tuple(row["spool_digest"] for row in ordinary)):
            raise PolicyViolation("Control frozen spool prefix digest drift")
        controls = db.execute(
            "select * from continuity_control_event where session_id=? order by spool_sequence",
            (binding.session_id,),
        ).fetchall()
        if len(ordinary) + len(controls) > len(entries):
            raise PolicyViolation("Control recorded original spool evidence missing")
        rejected = 0
        for offset, row in enumerate(controls, len(ordinary)):
            entry = entries[offset]
            expected_body = self._body(binding, request["request_digest"], entry, row["created_at"])
            if (
                canonical_json(expected_body) != row["body_json"]
                or digest(expected_body) != row["control_digest"]
                or any(
                    row[key] != value
                    for key, value in expected_body.items()
                    if key not in {"schema", "grants_authority", "approval_inherited"}
                )
            ):
                raise PolicyViolation("Control immutable body/column/source parity drift")
            rejected += row["disposition"] == "rejected-after-freeze"
        issues = []
        if len(ordinary) + len(controls) != len(entries):
            issues.append("unpersisted-spool-delta")
        if rejected:
            issues.append("rejected-after-freeze")
        if session["status"] != "closed":
            issues.append("pending-close")
        return {
            "schema": "zekam-local-control-health/v1",
            "session_id": binding.session_id,
            "session_state": session["status"],
            "close_request_digest": request["request_digest"],
            "close_receipt_digest": session["close_receipt_digest"],
            "event_count": len(rows),
            "ordinary_spool_count": len(ordinary),
            "control_event_count": len(controls),
            "rejected_count": rejected,
            "persisted_spool_count": len(ordinary) + len(controls),
            "spool_event_count": len(entries),
            "issues": issues,
            "state": "attention-required" if issues else "healthy",
            "read_only": True,
            "verification_scope": "historical-binding-and-actual-spool",
            "current_source_verified": False,
            "grants_authority": False,
            "generic_ack_created": False,
        }

    def inspect(self, binding: ContinuityBinding) -> dict[str, Any]:
        if not isinstance(binding, ContinuityBinding):
            raise ValidationFailed("Control typed historical binding required")
        binding.__post_init__()
        entries = self.spool.read_session_entries(
            client_id=binding.client_id, session_id=binding.external_session_id
        )
        with self._read() as db:
            return self._progress(db, binding, entries)

    def drain(self, binding: ContinuityBinding) -> int:
        if not isinstance(binding, ContinuityBinding):
            raise ValidationFailed("Control typed historical binding required")
        binding.__post_init__()
        # Own the actual disk barrier: callers cannot supply a self-consistent fake tuple.
        with (
            self.spool.frozen_session_entries(
                client_id=binding.client_id, session_id=binding.external_session_id
            ) as entries,
            self.continuity._transaction() as db,
        ):
            progress = self._progress(db, binding, entries)
            for entry in entries[progress["persisted_spool_count"] :]:
                body = self._body(
                    binding,
                    progress["close_request_digest"],
                    entry,
                    dt.datetime.now(dt.UTC).isoformat(),
                )
                values = {
                    "control_digest": digest(body),
                    **{
                        key: value
                        for key, value in body.items()
                        if key not in {"schema", "grants_authority", "approval_inherited"}
                    },
                    "body_json": canonical_json(body),
                }
                columns = ",".join(values)
                placeholders = ",".join("?" for _ in values)
                db.execute(
                    f"insert into continuity_control_event({columns}) values({placeholders})",
                    tuple(values.values()),
                )
            progress = self._progress(db, binding, entries)
        # Report failure AFTER durable commit, and after accounting for later advisory entries.
        if progress["rejected_count"]:
            raise PolicyViolation(
                "Continuity new ordinary event rejected after freeze; evidence retained"
            )
        return int(progress["persisted_spool_count"])
