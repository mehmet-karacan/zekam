"""Atomic close requests and evidence-gated finalization in the existing local DB."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Callable
from typing import Any

from zekam.application.knowledge_file_plane import KnowledgeNoteManifest
from zekam.application.local_continuity import (
    ContinuityBinding,
    ContinuityTail,
    digest_text,
    logical,
)
from zekam.application.local_continuity_close import (
    CANDIDATE_RECIPE_DIGEST,
    COMPILE_OPERATION,
    CloseCandidateBundle,
    CloseSummary,
    FrozenClose,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.domain.identifiers import new_uuid7
from zekam.infrastructure.knowledge_files import KnowledgeFileStore
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.local_runtime import SQLiteLocalRuntimeStore
from zekam.infrastructure.sqlite.local_startup_checkpoint import SQLiteStartupCheckpointSource


class SQLiteCloseStore:
    def __init__(
        self,
        continuity: SQLiteContinuityStore,
        runtime: SQLiteLocalRuntimeStore,
        files: KnowledgeFileStore,
        *,
        source_probe: Callable[[ContinuityBinding], None],
    ) -> None:
        if continuity.path.resolve() != runtime.path.resolve() or not callable(source_probe):
            raise ValidationFailed("Close exact operational store and source probe required")
        self.continuity, self.runtime, self.files = continuity, runtime, files
        self.source_probe = source_probe

    def verify_projection(self, manifest: KnowledgeNoteManifest, payload: bytes) -> None:
        actual = self.files._read_optional(manifest.portable_ref, max_bytes=2 * 1024 * 1024)
        if actual != payload:
            raise PolicyViolation("Close projection missing, changed or user-file conflict")

    def bind_effect(self, binding: ContinuityBinding, claim_id: str) -> None:
        self.continuity.bind_effect(binding, claim_id)

    @staticmethod
    def _job_payload(binding: ContinuityBinding, request_digest: str) -> dict[str, Any]:
        return {
            "operation": COMPILE_OPERATION,
            "session_id": binding.session_id,
            "project_id": binding.project_id,
            "work_item_id": binding.work_item_id,
            "run_id": binding.run_id,
            "binding_digest": binding.binding_digest,
            "request_digest": request_digest,
        }

    @staticmethod
    def _outbox_payload(binding: ContinuityBinding, request_digest: str) -> dict[str, str]:
        return {
            "session_id": binding.session_id,
            "binding_digest": binding.binding_digest,
            "request_digest": request_digest,
        }

    def _checkpoint_evidence(
        self,
        db: sqlite3.Connection,
        binding: ContinuityBinding,
        checkpoint_digest: str,
        manifest_digest: str,
        expected_tail: ContinuityTail,
    ) -> list[sqlite3.Row]:
        rows = self.continuity._events(db, binding.session_id)
        if self.continuity._tail(rows) != expected_tail:
            raise ConcurrencyConflict("Close frozen event boundary drift")
        checkpoint = db.execute(
            "select * from continuity_checkpoint where checkpoint_digest=? and session_id=?",
            (checkpoint_digest, binding.session_id),
        ).fetchone()
        if (
            checkpoint is None
            or checkpoint["covered_sequence"] != expected_tail.sequence
            or checkpoint["covered_event_digest"] != expected_tail.event_digest
            or checkpoint["context_digest"] != manifest_digest
            or checkpoint["source_snapshot_id"] != binding.source_snapshot_id
            or digest(json.loads(checkpoint["body_json"])) != checkpoint_digest
        ):
            raise PolicyViolation("Close exact durable checkpoint required")
        self.continuity._checkpoint_body(checkpoint, binding)
        latest = db.execute(
            "select manifest_digest from hydration_receipt where session_id=?"
            " order by created_at desc,receipt_digest desc limit 1",
            (binding.session_id,),
        ).fetchone()
        if latest is None or latest[0] != manifest_digest:
            raise PolicyViolation("Close exact current context required")
        return rows

    def _evidence(
        self,
        db: sqlite3.Connection,
        binding: ContinuityBinding,
        checkpoint_digest: str,
        manifest_digest: str,
        expected_tail: ContinuityTail,
    ) -> tuple[dict[str, Any], list[sqlite3.Row]]:
        """Fresh freeze still requires live source resolution and open-session admission."""
        rows = self._checkpoint_evidence(
            db, binding, checkpoint_digest, manifest_digest, expected_tail
        )
        manifest = self.continuity._verified_manifest(db, binding, manifest_digest)
        return manifest, rows

    def _frozen_evidence(
        self,
        db: sqlite3.Connection,
        binding: ContinuityBinding,
        checkpoint_digest: str,
        manifest_digest: str,
        expected_tail: ContinuityTail,
    ) -> None:
        """Only _load of an exact existing close request uses this historical path.

        Closing creates its own pending job and is no longer a startup operation.
        Reopening immutable evidence must not rerender it through startup admission.
        Current environment/source admission is nevertheless repeated in the writer
        transaction, and no historical fragment confers execution authority.
        """
        self.source_probe(binding)
        self._checkpoint_evidence(db, binding, checkpoint_digest, manifest_digest, expected_tail)
        SQLiteStartupCheckpointSource._historical_manifest(db, binding, manifest_digest)

    def freeze(
        self,
        binding: ContinuityBinding,
        summary: CloseSummary,
        *,
        checkpoint_digest: str,
        manifest_digest: str,
        expected_tail: ContinuityTail,
    ) -> FrozenClose:
        return self._freeze(
            binding,
            summary,
            None,
            checkpoint_digest=checkpoint_digest,
            manifest_digest=manifest_digest,
            expected_tail=expected_tail,
        )

    def freeze_v2(
        self,
        binding: ContinuityBinding,
        summary: CloseSummary,
        candidates: CloseCandidateBundle,
        *,
        checkpoint_digest: str,
        manifest_digest: str,
        expected_tail: ContinuityTail,
    ) -> FrozenClose:
        if type(summary) is not CloseSummary or type(candidates) is not CloseCandidateBundle:
            raise ValidationFailed("Close v2 exact summary and candidate bundle required")
        candidates.__post_init__()
        return self._freeze(
            binding,
            summary,
            candidates,
            checkpoint_digest=checkpoint_digest,
            manifest_digest=manifest_digest,
            expected_tail=expected_tail,
        )

    def _freeze(
        self,
        binding: ContinuityBinding,
        summary: CloseSummary,
        candidates: CloseCandidateBundle | None,
        *,
        checkpoint_digest: str,
        manifest_digest: str,
        expected_tail: ContinuityTail,
    ) -> FrozenClose:
        """Caller holds the same spool barrier used for PRE_CLOSE/checkpoint."""
        if not isinstance(summary, CloseSummary) or not isinstance(expected_tail, ContinuityTail):
            raise ValidationFailed("Close typed summary/tail required")
        summary.__post_init__()
        expected_tail.__post_init__()
        digest_text(checkpoint_digest)
        digest_text(manifest_digest)
        self.source_probe(binding)
        with self.continuity._transaction() as db:
            session = self.continuity._assert_binding(db, binding)
            slug = db.execute(
                "select slug from project where id=?", (binding.project_id,)
            ).fetchone()[0]
            if candidates is None:
                semantic = {
                    "schema": "zekam-local-close/v1",
                    "binding_digest": binding.binding_digest,
                    "session_id": binding.session_id,
                    "checkpoint_digest": checkpoint_digest,
                    "manifest_digest": manifest_digest,
                    "covered_sequence": expected_tail.sequence,
                    "covered_event_digest": expected_tail.event_digest,
                    "project_slug": slug,
                    "summary": summary.body(),
                }
            else:
                semantic = {
                    "schema": "zekam-local-close/v2",
                    "binding_digest": binding.binding_digest,
                    "session_id": binding.session_id,
                    "checkpoint_digest": checkpoint_digest,
                    "manifest_digest": manifest_digest,
                    "covered_sequence": expected_tail.sequence,
                    "covered_event_digest": expected_tail.event_digest,
                    "project_slug": slug,
                    "summary": summary.body(),
                    "projection_recipe": "local-close-candidates/v2",
                    "candidate_recipe_digest": CANDIDATE_RECIPE_DIGEST,
                    "candidate_bundle": candidates.body(),
                }
            previous = db.execute(
                "select request_digest,input_json from continuity_close_request where session_id=?",
                (binding.session_id,),
            ).fetchone()
            if previous is not None:
                body = json.loads(previous["input_json"])
                old_semantic = {key: value for key, value in body.items() if key != "created_at"}
                if canonical_json(old_semantic) != canonical_json(semantic):
                    raise PolicyViolation("Close replay frozen input drift")
                return self._load(db, binding, previous["request_digest"])
            if session["status"] != "open":
                raise PolicyViolation("Close freeze requires open session")
            self.continuity._no_pending(db, binding)
            self.source_probe(binding)
            context, rows = self._evidence(
                db, binding, checkpoint_digest, manifest_digest, expected_tail
            )
            allowed_sources = {
                (item["source_ref"], item["content_digest"])
                for item in context["context"]["compiler"]["selected"]
            }
            allowed_evidence = {
                (f"checkpoint/{checkpoint_digest[7:]}", checkpoint_digest),
                (f"context/{manifest_digest[7:]}", manifest_digest),
            }
            allowed_evidence.update(
                (f"event/{row['event_digest'][7:]}", row["event_digest"]) for row in rows
            )
            if (
                not set(summary.sources) <= allowed_sources
                or not set(summary.evidence) <= allowed_evidence
            ):
                raise PolicyViolation(
                    "Close source/evidence references lack exact durable provenance"
                )
            if candidates is not None:
                for category in ("memory", "decision", "skill", "failure"):
                    for claim in getattr(candidates, category):
                        if not set(claim.source_refs) <= set(summary.sources) or not set(
                            claim.evidence_refs
                        ) <= set(summary.evidence):
                            raise PolicyViolation(
                                "Close candidate refs lack admitted summary provenance"
                            )
            now = dt.datetime.now(dt.UTC).isoformat()
            body = semantic | {"created_at": now}
            encoded = canonical_json(body)
            if len(encoded.encode()) > 65536:
                raise ValidationFailed("Close input byte bound exceeded")
            request_digest = digest(body)
            db.execute(
                "insert into continuity_close_request values(?,?,?,?,?,?)",
                (
                    request_digest,
                    binding.session_id,
                    checkpoint_digest,
                    expected_tail.sequence,
                    encoded,
                    now,
                ),
            )
            job_id = str(new_uuid7())
            self.runtime._insert_job(
                db,
                job_id=job_id,
                key=f"close:{request_digest}",
                payload_json=canonical_json(self._job_payload(binding, request_digest)),
                max_attempts=1,
                available_at=now,
                timeout_at=None,
                created_at=now,
            )
            outbox_key = f"close:{request_digest}:compile"
            self.runtime._emit_outbox(
                db,
                job_id=job_id,
                event_kind=COMPILE_OPERATION,
                payload=self._outbox_payload(binding, request_digest),
                idempotency_key=outbox_key,
                created_at=now,
            )
            outbox_id = db.execute(
                "select id from local_outbox where idempotency_key=?", (outbox_key,)
            ).fetchone()[0]
            db.execute(
                "insert into continuity_outbox_binding values(?,?,?,'close',?,?)",
                (outbox_id, binding.session_id, job_id, request_digest, request_digest),
            )
            db.execute(
                "update session set status='closing' where id=? and status='open'",
                (binding.session_id,),
            )
            return self._load(db, binding, request_digest)

    def _load(
        self, db: sqlite3.Connection, binding: ContinuityBinding, request_digest: str
    ) -> FrozenClose:
        self.continuity._assert_binding(db, binding)
        row = db.execute(
            "select c.*,b.job_id,b.outbox_id,b.input_digest,j.payload_json as job_payload,"
            "j.state as job_state,o.event_kind,o.payload_json as outbox_payload,"
            "o.payload_digest,d.state as delivery_state,s.status as session_state,"
            "s.close_receipt_digest as session_close_receipt"
            " from continuity_close_request c join continuity_outbox_binding b"
            " on b.close_request_digest=c.request_digest and b.session_id=c.session_id"
            " join local_job j on j.id=b.job_id join local_outbox o on o.id=b.outbox_id"
            " and o.job_id=j.id join local_outbox_delivery d on d.outbox_id=o.id"
            " join session s on s.id=c.session_id where c.request_digest=? and c.session_id=?",
            (request_digest, binding.session_id),
        ).fetchone()
        if row is None:
            raise PolicyViolation("Close request exact binding unavailable")
        if row["session_state"] not in {"closing", "closed"}:
            raise PolicyViolation("Frozen close requires closing or closed session")
        body = json.loads(row["input_json"])
        if (
            canonical_json(body) != row["input_json"]
            or digest(body) != request_digest
            or row["input_digest"] != request_digest
            or row["created_at"] != body["created_at"]
            or row["checkpoint_digest"] != body["checkpoint_digest"]
            or row["covered_sequence"] != body["covered_sequence"]
            or row["event_kind"] != COMPILE_OPERATION
            or row["job_payload"] != canonical_json(self._job_payload(binding, request_digest))
            or row["outbox_payload"]
            != canonical_json(self._outbox_payload(binding, request_digest))
            or row["payload_digest"] != digest(self._outbox_payload(binding, request_digest))
        ):
            raise PolicyViolation("Close persisted request/job/outbox integrity drift")
        self._frozen_evidence(
            db,
            binding,
            body["checkpoint_digest"],
            body["manifest_digest"],
            ContinuityTail(body["covered_sequence"], body["covered_event_digest"]),
        )
        state = "pending"
        if row["job_state"] in {"failed", "recovery-required", "quarantined"} or row[
            "delivery_state"
        ] in {"failed", "recovery-required"}:
            state = "recovery-required"
        if row["session_state"] == "closed":
            receipt = db.execute(
                "select receipt_digest from close_receipt where request_digest=?", (request_digest,)
            ).fetchone()
            if receipt is None or receipt[0] != row["session_close_receipt"]:
                raise PolicyViolation("Closed session lacks terminal close receipt")
            state = "complete"
        result = FrozenClose(request_digest, row["job_id"], row["outbox_id"], body, state)
        result.assert_integrity(binding)
        if state == "complete":
            self._compiled(db, binding, result)
            delivery_evidence = self._terminal_delivery(db, binding, result)
            terminal = db.execute(
                "select * from close_receipt where request_digest=?", (request_digest,)
            ).fetchone()
            projections = [item.evidence() for item in result.projections(binding)]
            expected_receipt = digest(
                {
                    "request_digest": request_digest,
                    "session_id": binding.session_id,
                    "checkpoint_digest": body["checkpoint_digest"],
                    "manifest_digest": body["manifest_digest"],
                    "outbox_id": result.outbox_id,
                    "projections": projections,
                    "delivery_evidence_digest": delivery_evidence,
                }
            )
            if (
                terminal["receipt_digest"] != expected_receipt
                or terminal["session_id"] != binding.session_id
                or terminal["checkpoint_digest"] != body["checkpoint_digest"]
                or terminal["manifest_digest"] != body["manifest_digest"]
                or terminal["outbox_id"] != result.outbox_id
                or terminal["projections_json"] != canonical_json(projections)
            ):
                raise PolicyViolation("Closed session terminal receipt integrity drift")
        return result

    def load(self, binding: ContinuityBinding, request_digest: str) -> FrozenClose:
        digest_text(request_digest)
        self.source_probe(binding)
        with self.continuity._transaction() as db:
            return self._load(db, binding, request_digest)

    def _compiled(
        self, db: sqlite3.Connection, binding: ContinuityBinding, request: FrozenClose
    ) -> None:
        self._terminal_effect(
            db, binding, request.job_id, request.effect_key, request.compile_evidence(binding)
        )
        self._notes(db, binding, request)

    @staticmethod
    def _terminal_effect(
        db: sqlite3.Connection,
        binding: ContinuityBinding,
        job_id: str,
        effect_key: str,
        evidence: str,
    ) -> None:
        rows = db.execute(
            "select c.*,r.status,r.evidence_digest,rc.state as recovery_state,"
            "rr.outcome,rr.evidence_digest as resolution_evidence from local_effect_claim c"
            " left join local_effect_receipt r on r.claim_id=c.id"
            " left join local_recovery_case rc on rc.effect_claim_id=c.id"
            " left join local_recovery_resolution rr on rr.recovery_case_id=rc.id where c.job_id=?",
            (job_id,),
        ).fetchall()
        job = db.execute(
            "select state,terminal_evidence_digest from local_job where id=?", (job_id,)
        ).fetchone()
        if (
            len(rows) != 1
            or job is None
            or job["state"] != "completed"
            or rows[0]["operation"] != COMPILE_OPERATION
            or rows[0]["idempotency_key"] != effect_key
            or rows[0]["effect_digest"] != evidence
        ):
            raise PolicyViolation("Close requires exact terminal job/effect receipt evidence")
        row = rows[0]
        direct = row["status"] == "completed" and row["evidence_digest"] == evidence
        resolved = (
            row["status"] in {None, "unknown"}
            and row["recovery_state"] == "resolved"
            and row["outcome"] == "completed"
            and row["resolution_evidence"] == evidence
        )
        terminal_digests = {
            evidence,
            digest([("completed", evidence)]),
            digest(
                [
                    (
                        row["status"],
                        row["evidence_digest"],
                        row["outcome"],
                        row["resolution_evidence"],
                    )
                ]
            ),
        }
        if not (direct or resolved) or job["terminal_evidence_digest"] not in terminal_digests:
            raise PolicyViolation("Close terminal effect/recovery evidence drift")
        linked = db.execute(
            "select 1 from continuity_effect_binding where claim_id=?"
            " and session_id=? and job_id=?",
            (rows[0]["id"], binding.session_id, job_id),
        ).fetchone()
        if linked is None:
            raise PolicyViolation("Close effect session binding missing")

    def _notes(
        self, db: sqlite3.Connection, binding: ContinuityBinding, request: FrozenClose
    ) -> None:
        for item in request.projections(binding):
            note = db.execute(
                "select * from knowledge_note where portable_ref=?", (item.manifest.portable_ref,)
            ).fetchone()
            if (
                note is None
                or note["realm_id"] != binding.realm_id
                or note["project_id"] != binding.project_id
                or note["owner_scope"] != item.manifest.owner_scope
                or note["note_kind"] != item.manifest.note_kind
                or note["classification"] != item.manifest.classification.value
                or note["project_slug"] != item.manifest.project_slug
                or note["authorship"] != "generated"
                or note["state"] != "inbox"
                or note["materialized"] != 1
                or note["content_digest"] != item.manifest.content_digest
            ):
                raise PolicyViolation("Close generated candidate manifest incomplete or drifted")
            self.verify_projection(item.manifest, item.payload)

    def prepare_repair(
        self, binding: ContinuityBinding, request_digest: str, repair_key: str
    ) -> str:
        logical(repair_key, "Close repair key")
        self.source_probe(binding)
        with self.continuity._transaction() as db:
            request = self._load(db, binding, request_digest)
            key = f"close-repair:{request_digest}:{digest(repair_key)}"
            payload = self._job_payload(binding, request_digest) | {
                "purpose": "repair-generated-candidates",
                "repair_key": repair_key,
                "original_job_id": request.job_id,
                "compile_evidence": request.compile_evidence(binding),
            }
            existing = db.execute(
                "select id,payload_json from local_job where idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != canonical_json(payload):
                    raise PolicyViolation("Close repair job replay drift")
                return str(existing["id"])
            original = db.execute(
                "select state from local_job where id=?", (request.job_id,)
            ).fetchone()
            cases = db.execute(
                "select c.id from local_recovery_case c join local_effect_claim e"
                " on e.id=c.effect_claim_id where c.job_id=? and c.state='open'"
                " and e.operation=? and e.idempotency_key=? and e.effect_digest=?",
                (
                    request.job_id,
                    COMPILE_OPERATION,
                    request.effect_key,
                    request.compile_evidence(binding),
                ),
            ).fetchall()
            if original["state"] != "recovery-required" or len(cases) != 1:
                raise PolicyViolation("Explicit close repair needs exact unknown original effect")
            prior = db.execute(
                "select 1 from local_job where"
                " json_extract(payload_json,'$.original_job_id')=? limit 1",
                (request.job_id,),
            ).fetchone()
            if prior is not None:
                raise PolicyViolation("Existing repair attempt must be reconciled, not duplicated")
            now = dt.datetime.now(dt.UTC).isoformat()
            job_id = str(new_uuid7())
            self.runtime._insert_job(
                db,
                job_id=job_id,
                key=key,
                payload_json=canonical_json(payload),
                max_attempts=1,
                available_at=now,
                timeout_at=None,
                created_at=now,
            )
            return job_id

    def complete_repair(
        self, binding: ContinuityBinding, request_digest: str, repair_job_id: str
    ) -> FrozenClose:
        self.source_probe(binding)
        with self.continuity._transaction() as db:
            request = self._load(db, binding, request_digest)
            repair = db.execute(
                "select payload_json from local_job where id=?", (repair_job_id,)
            ).fetchone()
            if repair is None:
                raise PolicyViolation("Close exact repair job missing")
            body = json.loads(repair[0])
            key = body.get("repair_key")
            logical(key, "Close repair key")
            expected = self._job_payload(binding, request_digest) | {
                "purpose": "repair-generated-candidates",
                "repair_key": key,
                "original_job_id": request.job_id,
                "compile_evidence": request.compile_evidence(binding),
            }
            if canonical_json(expected) != repair[0]:
                raise PolicyViolation("Close repair job owner/input drift")
            evidence = digest(
                {
                    "repair_job_id": repair_job_id,
                    "request_digest": request_digest,
                    "compile_evidence": request.compile_evidence(binding),
                }
            )
            self._terminal_effect(
                db, binding, repair_job_id, f"close-repair:{repair_job_id}", evidence
            )
            self._notes(db, binding, request)
            case = db.execute(
                "select c.id from local_recovery_case c join local_effect_claim e"
                " on e.id=c.effect_claim_id where c.job_id=? and e.idempotency_key=?"
                " and e.effect_digest=?",
                (request.job_id, request.effect_key, request.compile_evidence(binding)),
            ).fetchone()
            if case is None:
                raise PolicyViolation("Close original immutable recovery case missing")
            case_id = str(case[0])
        self.source_probe(binding)
        self.runtime.resolve_recovery(
            case_id, outcome="completed", evidence_digest=request.compile_evidence(binding)
        )
        with self.continuity._transaction() as db:
            job_state = db.execute(
                "select state from local_job where id=?", (request.job_id,)
            ).fetchone()[0]
        if job_state == "recovery-required":
            self.runtime.reconcile_recovery(request.job_id)
        return self.verify_compiled(binding, request_digest)

    def verify_compiled(self, binding: ContinuityBinding, request_digest: str) -> FrozenClose:
        digest_text(request_digest)
        self.source_probe(binding)
        with self.continuity._transaction() as db:
            request = self._load(db, binding, request_digest)
            self._compiled(db, binding, request)
            return request

    def reconcile_delivery(self, binding: ContinuityBinding, request_digest: str) -> FrozenClose:
        self.source_probe(binding)
        with self.continuity._transaction() as db:
            request = self._load(db, binding, request_digest)
            self._compiled(db, binding, request)
            case = db.execute(
                "select c.id,r.status from local_recovery_case c"
                " join local_outbox_receipt r on r.outbox_id=c.outbox_id"
                " where c.outbox_id=? and c.case_kind='outbox-delivery-unknown'",
                (request.outbox_id,),
            ).fetchone()
            if case is None or case["status"] != "unknown":
                raise PolicyViolation("Close delivery has no exact unknown recovery case")
            case_id = str(case["id"])
        self.source_probe(binding)
        self.runtime.resolve_recovery(
            case_id, outcome="delivered", evidence_digest=request.delivery_evidence(binding)
        )
        return self.load(binding, request_digest)

    @staticmethod
    def _terminal_delivery(
        db: sqlite3.Connection, binding: ContinuityBinding, request: FrozenClose
    ) -> str:
        expected = request.delivery_evidence(binding)
        delivery = db.execute(
            "select r.*,d.state as delivery_state,c.state as recovery_state,"
            "rr.outcome,rr.evidence_digest as resolution_evidence from local_outbox_receipt r"
            " join local_outbox_delivery d on d.outbox_id=r.outbox_id"
            " left join local_recovery_case c on c.outbox_id=r.outbox_id"
            " left join local_recovery_resolution rr on rr.recovery_case_id=c.id"
            " where r.outbox_id=?",
            (request.outbox_id,),
        ).fetchone()
        if delivery is None or delivery["delivery_state"] != "delivered":
            raise PolicyViolation("Close terminal delivery receipt missing or drifted")
        direct = delivery["status"] == "delivered" and delivery["evidence_digest"] == expected
        resolved = (
            delivery["status"] == "unknown"
            and delivery["recovery_state"] == "resolved"
            and delivery["outcome"] == "delivered"
            and delivery["resolution_evidence"] == expected
        )
        if not (direct or resolved):
            raise PolicyViolation("Close terminal delivery receipt missing or drifted")
        return expected

    def finalize(self, binding: ContinuityBinding, request_digest: str) -> str:
        digest_text(request_digest)
        self.source_probe(binding)
        with self.continuity._transaction() as db:
            request = self._load(db, binding, request_digest)
            self._compiled(db, binding, request)
            self.continuity._no_pending(db, binding)
            delivery_evidence = self._terminal_delivery(db, binding, request)
            projections = [item.evidence() for item in request.projections(binding)]
            receipt_body = {
                "request_digest": request_digest,
                "session_id": binding.session_id,
                "checkpoint_digest": request.input_body["checkpoint_digest"],
                "manifest_digest": request.input_body["manifest_digest"],
                "outbox_id": request.outbox_id,
                "projections": projections,
                "delivery_evidence_digest": delivery_evidence,
            }
            result = digest(receipt_body)
            existing = db.execute(
                "select * from close_receipt where request_digest=?", (request_digest,)
            ).fetchone()
            if existing is not None:
                if (
                    existing["receipt_digest"] != result
                    or existing["session_id"] != binding.session_id
                    or existing["checkpoint_digest"] != request.input_body["checkpoint_digest"]
                    or existing["manifest_digest"] != request.input_body["manifest_digest"]
                    or existing["outbox_id"] != request.outbox_id
                    or existing["projections_json"] != canonical_json(projections)
                ):
                    raise PolicyViolation("Close terminal receipt replay drift")
                return result
            now = dt.datetime.now(dt.UTC).isoformat()
            db.execute(
                "insert into close_receipt values(?,?,?,?,?,?,?,?)",
                (
                    result,
                    request_digest,
                    binding.session_id,
                    request.input_body["checkpoint_digest"],
                    request.input_body["manifest_digest"],
                    request.outbox_id,
                    canonical_json(projections),
                    now,
                ),
            )
            changed = db.execute(
                "update session set status='closed',closed_at=?,close_receipt_digest=?"
                " where id=? and status='closing'",
                (now, result, binding.session_id),
            )
            if changed.rowcount != 1:
                raise ConcurrencyConflict("Close finalizer session state drift")
            return result
