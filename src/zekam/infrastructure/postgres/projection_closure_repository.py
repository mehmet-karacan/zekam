"""PostgreSQL adapter for one-transaction projection-aware Work closure."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from zekam.application.continuity_projection import ACTIVE_WORK_PROJECTION_REF
from zekam.application.projection_closure import (
    PROJECTION_CLOSURE_ADAPTER_DIGEST,
    PROJECTION_CLOSURE_GENERATOR,
    PROJECTION_CLOSURE_OPERATION,
    ProjectionClosureApplyReceipt,
    ProjectionClosurePlan,
    ProjectionClosureSnapshot,
)
from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ConcurrencyConflict, NotFound, PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.security import Authorization, AuthorizationState
from zekam.domain.session_continuity import SessionCloseReceipt
from zekam.domain.work import WORK_ENTITY_TYPE, WorkState
from zekam.infrastructure.postgres.core_repository import EventStore, RevisionStore
from zekam.infrastructure.postgres.memory_continuity_repository import (
    MemoryContinuityRepository,
)
from zekam.infrastructure.postgres.work_repository import WorkItemRepository

_EFFECT_NAMESPACE = UUID("f46e7fe2-a802-5ec0-bb27-f747d3a5cda4")


@dataclass(frozen=True, slots=True)
class ProjectionClosureRepository:
    connection: Any
    realm_id: UUID

    def has_terminal_effect_receipt(self, claim_id: UUID) -> bool:
        """Read-only replay gate; existence never authorizes or repeats an effect."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select exists(select 1 from runtime.effect_receipt"
                " where realm_id=%s and claim_id=%s)",
                (self.realm_id, claim_id),
            )
            return bool(cursor.fetchone()[0])

    def read_closure_snapshot(
        self,
        receipt: SessionCloseReceipt,
        *,
        lock: bool = False,
    ) -> ProjectionClosureSnapshot:
        if lock:
            # The migration-owner function has a fixed table allowlist and
            # validates the exact live fence with PostgreSQL's statement clock.
            # The application role never receives LOCK TABLE privileges.
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "select continuity.lock_projection_closure_scope(%s,%s,%s,%s,%s,%s)",
                    (
                        self.realm_id,
                        receipt.project_id,
                        receipt.work_item_id,
                        receipt.run_id,
                        receipt.job_id,
                        receipt.attempt_id,
                    ),
                )
                if cursor.fetchone() is None:
                    raise ConcurrencyConflict("Projection closure DB lock zamani bulunamadi")
        work = (
            WorkItemRepository(self.connection, self.realm_id).get_for_update(
                receipt.work_item_id
            )
            if lock
            else WorkItemRepository(self.connection, self.realm_id).get(receipt.work_item_id)
        )
        if (
            receipt.realm_id != self.realm_id
            or work.project_id != receipt.project_id
            or work.id != receipt.work_item_id
        ):
            raise PolicyViolation("Projection closure realm/project/work binding drift")
        suffix = " for update" if lock else ""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select project_id,work_item_id,plan_id,run_id,state,fencing_token"
                " from runtime.job where realm_id=%s and id=%s" + suffix,
                (self.realm_id, receipt.job_id),
            )
            job = cursor.fetchone()
            if job is None:
                raise NotFound("Projection closure exact job bulunamadi")
            if (
                UUID(str(job[0])) != receipt.project_id
                or UUID(str(job[1])) != receipt.work_item_id
                or UUID(str(job[3])) != receipt.run_id
                or str(job[4]) != "running"
                or int(job[5]) != receipt.fencing_token
            ):
                raise PolicyViolation("Projection closure job/run/fence current degil")
            if job[2] is None:
                raise PolicyViolation("Projection closure job exact Task Plan tasimiyor")
            task_plan_id = UUID(str(job[2]))

            cursor.execute(
                "select id,revision,source_revision,policy_digest,plan_digest"
                " from work.task_plan where realm_id=%s and project_id=%s"
                " and work_item_id=%s order by revision desc,id desc limit 1",
                (self.realm_id, receipt.project_id, receipt.work_item_id),
            )
            task_plan = cursor.fetchone()
            if task_plan is None or UUID(str(task_plan[0])) != task_plan_id:
                raise PolicyViolation("Projection closure runtime Plan current revision degil")

            cursor.execute(
                "select outcome,fencing_token from runtime.job_attempt"
                " where realm_id=%s and id=%s and job_id=%s" + suffix,
                (self.realm_id, receipt.attempt_id, receipt.job_id),
            )
            attempt = cursor.fetchone()
            if (
                attempt is None
                or attempt[0] is not None
                or int(attempt[1]) != receipt.fencing_token
            ):
                raise PolicyViolation("Projection closure attempt/fence current degil")

            cursor.execute(
                "select id,expires_at,fencing_token,worker_label from runtime.lease"
                " where realm_id=%s and job_id=%s and attempt_id=%s"
                " and expires_at>statement_timestamp()"
                + suffix,
                (self.realm_id, receipt.job_id, receipt.attempt_id),
            )
            lease = cursor.fetchone()
            if lease is None or int(lease[2]) != receipt.fencing_token:
                raise PolicyViolation("Projection closure exact lease/fence bulunamadi")
            lease_id = UUID(str(lease[0]))

            cursor.execute(
                "select envelope_digest,checkpoint_id,checkpoint_digest"
                " from runtime.execution_envelope"
                " where realm_id=%s and run_id=%s and job_id=%s and attempt_id=%s"
                " and lease_id=%s and fencing_token=%s"
                " order by request_ordinal desc,id desc limit 1",
                (
                    self.realm_id,
                    receipt.run_id,
                    receipt.job_id,
                    receipt.attempt_id,
                    lease_id,
                    receipt.fencing_token,
                ),
            )
            envelope = cursor.fetchone()
            if (
                envelope is None
                or str(envelope[0]) != receipt.envelope_digest
                or envelope[1] is None
                or str(envelope[2]) != receipt.checkpoint_ref.digest_value
            ):
                raise PolicyViolation("Projection closure execution envelope drift")

            cursor.execute(
                "select id,checkpoint_key,checkpoint_digest from work.checkpoint"
                " where realm_id=%s and project_id=%s and work_item_id=%s"
                " and task_plan_id=%s and job_id=%s and id=%s",
                (
                    self.realm_id,
                    receipt.project_id,
                    receipt.work_item_id,
                    task_plan_id,
                    receipt.job_id,
                    envelope[1],
                ),
            )
            checkpoint = cursor.fetchone()
            if (
                checkpoint is None
                or str(checkpoint[2]) != receipt.checkpoint_ref.digest_value
                or receipt.checkpoint_ref.ref
                not in {
                    str(checkpoint[1]),
                    f"db:work.checkpoint/{checkpoint[0]}",
                }
            ):
                raise PolicyViolation("Projection closure checkpoint binding drift")

            cursor.execute(
                "select resource,mode from runtime.resource_lock"
                " where realm_id=%s and job_id=%s and lease_id=%s"
                " order by resource,mode" + suffix,
                (self.realm_id, receipt.job_id, lease_id),
            )
            locks = tuple((str(row[0]), str(row[1])) for row in cursor.fetchall())
            expected_lock = (
                (
                    f"work:{receipt.project_id}:{receipt.work_item_id}:"
                    f"projection-close:{receipt.run_id}",
                    "write",
                ),
            )
            if locks != expected_lock:
                raise PolicyViolation("Projection closure logical lock exact degil")
            lock_digest = digest(
                [{"resource": resource, "mode": mode} for resource, mode in locks]
            )

            identity = (
                self.realm_id,
                receipt.project_id,
                receipt.work_item_id,
                receipt.run_id,
                receipt.session_id,
                receipt.client_id,
            )
            cursor.execute(
                "select event.id,event.event_digest,event.sequence,event.previous_digest,"
                " outbox.id,outbox.plan_digest,outbox.payload_digest,outbox.state,"
                " outbox.terminal_receipt_digest,event.event_body"
                " from continuity.session_lifecycle_event event"
                " join continuity.lifecycle_delivery_outbox outbox"
                " on outbox.realm_id=event.realm_id and outbox.event_id=event.id"
                " where event.realm_id=%s and event.project_id=%s"
                " and event.work_item_id=%s and event.run_id=%s"
                " and event.session_id=%s and event.client_id=%s"
                " and event.event_type='pre_close'"
                " order by event.sequence desc,event.id desc limit 1"
                + (" for update of outbox" if lock else ""),
                identity,
            )
            pre_close = cursor.fetchone()
            if pre_close is None:
                raise NotFound("Projection closure current pre_close outbox bulunamadi")
            if str(pre_close[7]) not in {"pending", "processing"} or pre_close[8] is not None:
                raise PolicyViolation("Projection closure pre_close outbox terminal veya stale")
            pre_close_body = pre_close[9]
            if (
                not isinstance(pre_close_body, dict)
                or pre_close_body.get("checkpoint_ref") != receipt.checkpoint_ref.ref
                or pre_close_body.get("plan_ref") != f"work-plan:{task_plan_id}"
            ):
                raise PolicyViolation("Projection closure pre_close Plan/checkpoint binding drift")
            expected_payload = digest(
                {
                    "event_digest": str(pre_close[1]),
                    "plan_digest": str(pre_close[5]),
                }
            )
            if str(pre_close[6]) != expected_payload:
                raise PolicyViolation("Projection closure pre_close outbox payload drift")

            cursor.execute(
                "select state,plan_id,client_id,session_id,policy_digest"
                " from runtime.execution_run"
                " where realm_id=%s and project_id=%s and work_item_id=%s and id=%s" + suffix,
                (self.realm_id, receipt.project_id, receipt.work_item_id, receipt.run_id),
            )
            run = cursor.fetchone()
            if (
                run is None
                or str(run[0]) != "active"
                or UUID(str(run[1])) != task_plan_id
                or str(run[2]) != receipt.client_id
                or str(run[3]) != receipt.session_id
                or str(run[4]) != receipt.policy_digest
            ):
                raise PolicyViolation("Projection closure execution run identity drift")

            cursor.execute(
                "select receipt_body->>'policy_digest',"
                " receipt_body->>'migration_digest',receipt_body->>'context_digest'"
                " from continuity.session_hydration_receipt"
                " where realm_id=%s and project_id=%s and work_item_id=%s and run_id=%s"
                " and session_id=%s and client_id=%s and fresh and complete"
                " order by created_at desc,id desc limit 1",
                identity,
            )
            hydration = cursor.fetchone()
            if hydration is None or tuple(str(item) for item in hydration) != (
                receipt.policy_digest,
                receipt.migration_digest,
                receipt.context_digest,
            ):
                raise PolicyViolation("Projection closure hydration policy/migration/context drift")

            cursor.execute(
                "select count(*) from runtime.job where realm_id=%s and run_id=%s"
                " and id<>%s and state in"
                " ('ready','running','blocked','recovery-required')",
                (self.realm_id, receipt.run_id, receipt.job_id),
            )
            other_open_job_count = int(cursor.fetchone()[0])
            cursor.execute(
                "select count(*) from runtime.claim_without_receipt pending"
                " join runtime.job job on job.realm_id=pending.realm_id and job.id=pending.job_id"
                " where pending.realm_id=%s and job.run_id=%s and pending.job_id<>%s",
                (self.realm_id, receipt.run_id, receipt.job_id),
            )
            other_receiptless_claim_count = int(cursor.fetchone()[0])

        release = MemoryContinuityRepository(
            self.connection, self.realm_id
        ).read_projection_release_snapshot(
            project_id=receipt.project_id,
            work_item_id=receipt.work_item_id,
            run_id=receipt.run_id,
            session_id=receipt.session_id,
            client_id=receipt.client_id,
        )
        if str(pre_close_body.get("source_revision")) != release.source_head:
            raise PolicyViolation("Projection closure pre_close/source revision drift")
        if (
            str(task_plan[2]) != release.source_head
            or str(task_plan[3]) != receipt.policy_digest
        ):
            raise PolicyViolation("Projection closure current Plan source/policy drift")
        return ProjectionClosureSnapshot(
            work_item=work,
            release=release,
            task_plan_id=task_plan_id,
            task_plan_revision=int(task_plan[1]),
            task_plan_digest=str(task_plan[4]),
            task_plan_source_revision=str(task_plan[2]),
            task_plan_policy_digest=str(task_plan[3]),
            job_id=receipt.job_id,
            attempt_id=receipt.attempt_id,
            run_id=receipt.run_id,
            lease_id=lease_id,
            lease_worker_label=str(lease[3]),
            fencing_token=receipt.fencing_token,
            lease_expires_at=lease[1],
            envelope_digest=str(envelope[0]),
            checkpoint_digest=str(checkpoint[2]),
            lock_digest=lock_digest,
            pre_close_event_id=UUID(str(pre_close[0])),
            pre_close_event_digest=str(pre_close[1]),
            pre_close_sequence=int(pre_close[2]),
            pre_close_previous_digest=(
                None if pre_close[3] is None else str(pre_close[3])
            ),
            pre_close_outbox_id=UUID(str(pre_close[4])),
            pre_close_outbox_plan_digest=str(pre_close[5]),
            pre_close_outbox_payload_digest=str(pre_close[6]),
            other_open_job_count=other_open_job_count,
            other_receiptless_claim_count=other_receiptless_claim_count,
        )

    def replay_completed_closure(
        self,
        receipt: SessionCloseReceipt,
        *,
        idempotency_key: str,
        plan_digest: str,
        authorization: Authorization,
        claim_id: UUID,
    ) -> ProjectionClosureApplyReceipt | None:
        """Verify and return one already-committed closure without a second effect."""
        parse_digest(plan_digest)
        if receipt.realm_id != self.realm_id:
            raise PolicyViolation("Projection closure replay realm binding drift")
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select job_id,attempt_id,operation,effect_digest,authorization_digest,"
                " authorization_id,idempotency_key,resources,execution_identity,"
                " fencing_token,adapter_digest,claim_digest"
                " from runtime.effect_claim where realm_id=%s and id=%s",
                (self.realm_id, claim_id),
            )
            claim = cursor.fetchone()
            if claim is None:
                return None
            cursor.execute(
                "select id,status,result_digest,failure_category,adapter_evidence_digest,"
                " completed_at from runtime.effect_receipt"
                " where realm_id=%s and claim_id=%s",
                (self.realm_id, claim_id),
            )
            effect = cursor.fetchone()
            if effect is None:
                return None
        if str(effect[1]) != "completed" or effect[2] is None or effect[3] is not None:
            raise PolicyViolation("Projection closure replay completed effect tasimiyor")

        resource = (
            f"work:{receipt.project_id}:{receipt.work_item_id}:"
            f"projection-close:{receipt.run_id}"
        )
        result_digest = str(effect[2])
        expected_effect_digest = digest(
            {
                "effect": "database-write",
                "operation": PROJECTION_CLOSURE_OPERATION,
                "resource": resource,
                "result_digest": result_digest,
            }
        )
        expected_claim_idempotency = digest(
            {
                "operation": PROJECTION_CLOSURE_OPERATION,
                "job_id": str(receipt.job_id),
                "effect_digest": expected_effect_digest,
                "idempotency_key": idempotency_key,
            }
        )
        resources = tuple(dict(item) for item in (claim[7] or ()))
        if (
            authorization.state is not AuthorizationState.CONSUMED
            or authorization.consumed_by != "projection-aware-close/v1"
            or authorization.consumed_at is None
            or authorization.consumed_at != effect[5]
            or authorization.realm_id != receipt.realm_id
            or authorization.work_item_id != receipt.work_item_id
            or authorization.plan_id is None
            or authorization.plan_digest != plan_digest
            or authorization.effect_digest != expected_effect_digest
            or tuple(authorization.scope.allowed_resources) != (resource,)
            or tuple(authorization.scope.allowed_effects) != ("database-write",)
            or authorization.scope.provider_refs
            or authorization.scope.secret_ref_ids
            or authorization.scope.data_classifications
            or UUID(str(claim[0])) != receipt.job_id
            or UUID(str(claim[1])) != receipt.attempt_id
            or str(claim[2]) != PROJECTION_CLOSURE_OPERATION
            or str(claim[3]) != expected_effect_digest
            or str(claim[4]) != authorization.authorization_digest
            or claim[5] is None
            or UUID(str(claim[5])) != authorization.id
            or str(claim[6]) != expected_claim_idempotency
            or resources != ({"resource": resource, "mode": "write"},)
            or int(claim[9]) != receipt.fencing_token
            or str(claim[10]) != PROJECTION_CLOSURE_ADAPTER_DIGEST
        ):
            raise PolicyViolation("Projection closure terminal authorization/claim drift")

        claim_body = {
            "job_id": str(receipt.job_id),
            "operation": str(claim[2]),
            "effect_digest": str(claim[3]),
            "authorization_digest": str(claim[4]),
            "idempotency_key": str(claim[6]),
            "resources": list(resources),
            "execution_identity": str(claim[8]),
            "fencing_token": int(claim[9]),
            "adapter_digest": str(claim[10]),
        }
        if str(claim[11]) != digest(claim_body):
            raise PolicyViolation("Projection closure terminal immutable claim drift")

        work = WorkItemRepository(self.connection, self.realm_id).get(receipt.work_item_id)
        closure_evidence = tuple(
            item
            for item in work.acceptance_evidence
            if item.kind == "closure-checkpoint"
            and item.reference == receipt.checkpoint_ref.ref
            and item.digest_value == receipt.checkpoint_ref.digest_value
        )
        if (
            work.realm_id != receipt.realm_id
            or work.project_id != receipt.project_id
            or work.state is not WorkState.COMPLETED
            or work.updated_at != effect[5]
            or len(closure_evidence) != 1
            or any(not item.verified for item in work.acceptance_criteria)
        ):
            raise PolicyViolation("Projection closure terminal Work record drift")

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,payload,payload_digest,reason,actor_id,recorded_at"
                " from core.revision where realm_id=%s and entity_type=%s"
                " and entity_id=%s and revision=%s",
                (self.realm_id, WORK_ENTITY_TYPE, work.id, work.revision),
            )
            revision = cursor.fetchone()
            if (
                revision is None
                or canonical_json(revision[1]) != canonical_json(work.body())
                or str(revision[2]) != work.record_digest
                or digest(revision[1]) != work.record_digest
                or str(revision[3]) != "projection-aware close verified"
                or UUID(str(revision[4])) != authorization.actor_id
                or revision[5] != effect[5]
            ):
                raise PolicyViolation("Projection closure terminal Work revision drift")
            revision_id = UUID(str(revision[0]))
            cursor.execute(
                "select payload,payload_digest,actor_id,occurred_at"
                " from core.event where realm_id=%s and event_type='work.state.completed'"
                " and entity_type=%s and entity_id=%s and revision_id=%s",
                (self.realm_id, WORK_ENTITY_TYPE, work.id, revision_id),
            )
            events = tuple(cursor.fetchall())
            expected_event = {
                "state": "completed",
                "revision": work.revision,
                "close_receipt_digest": receipt.receipt_digest,
            }
            if len(events) != 1:
                raise PolicyViolation("Projection closure terminal Work event drift")

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select project_id,work_item_id,plan_id,run_id,state,fencing_token"
                " from runtime.job where realm_id=%s and id=%s",
                (self.realm_id, receipt.job_id),
            )
            job = cursor.fetchone()
            cursor.execute(
                "select job_id,fencing_token,worker_label,outcome,result_digest,finished_at"
                " from runtime.job_attempt where realm_id=%s and id=%s",
                (self.realm_id, receipt.attempt_id),
            )
            attempt = cursor.fetchone()
            cursor.execute(
                "select project_id,work_item_id,plan_id,client_id,session_id,"
                " source_revision,policy_digest,state,terminal_at"
                " from runtime.execution_run where realm_id=%s and id=%s",
                (self.realm_id, receipt.run_id),
            )
            run = cursor.fetchone()
            cursor.execute(
                "select id,revision,source_revision,policy_digest,plan_digest"
                " from work.task_plan where realm_id=%s and project_id=%s"
                " and work_item_id=%s order by revision desc,id desc limit 1",
                (self.realm_id, receipt.project_id, receipt.work_item_id),
            )
            task_plan = cursor.fetchone()
        if job is None or attempt is None or run is None or task_plan is None:
            raise PolicyViolation("Projection closure terminal runtime zinciri eksik")
        task_plan_id = UUID(str(task_plan[0]))
        expected_execution_identity = f"{attempt[2]}:{receipt.fencing_token}"
        if (
            authorization.plan_id != task_plan_id
            or UUID(str(job[0])) != receipt.project_id
            or UUID(str(job[1])) != receipt.work_item_id
            or UUID(str(job[2])) != task_plan_id
            or UUID(str(job[3])) != receipt.run_id
            or str(job[4]) != "completed"
            or int(job[5]) != receipt.fencing_token
            or UUID(str(attempt[0])) != receipt.job_id
            or int(attempt[1]) != receipt.fencing_token
            or str(attempt[3]) != "succeeded"
            or str(attempt[4]) != result_digest
            or attempt[5] != effect[5]
            or str(claim[8]) != expected_execution_identity
            or UUID(str(run[0])) != receipt.project_id
            or UUID(str(run[1])) != receipt.work_item_id
            or UUID(str(run[2])) != task_plan_id
            or str(run[3]) != receipt.client_id
            or str(run[4]) != receipt.session_id
            or str(run[5]) != str(task_plan[2])
            or str(run[6]) != receipt.policy_digest
            or str(task_plan[3]) != receipt.policy_digest
            or str(run[7]) != "completed"
            or run[8] != effect[5]
        ):
            raise PolicyViolation("Projection closure terminal runtime identity drift")
        expected_execution_event = {
            "outcome": "succeeded",
            "claim_id": str(claim_id),
            "claim_digest": str(claim[11]),
            "claim_idempotency_key": expected_claim_idempotency,
            "execution_identity": expected_execution_identity,
            "effect_receipt_id": str(effect[0]),
            "result_digest": result_digest,
            "plan_digest": plan_digest,
            "task_plan_id": str(task_plan_id),
            "resource": resource,
            "close_receipt_digest": receipt.receipt_digest,
        }

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,project_id,work_item_id,run_id,session_id,client_id,job_id,"
                " attempt_id,close_status,idempotency_key,receipt_body,receipt_digest,created_at"
                " from continuity.session_close_receipt where realm_id=%s and id=%s",
                (self.realm_id, receipt.receipt_id),
            )
            close_row = cursor.fetchone()
        if (
            close_row is None
            or UUID(str(close_row[1])) != receipt.project_id
            or UUID(str(close_row[2])) != receipt.work_item_id
            or UUID(str(close_row[3])) != receipt.run_id
            or str(close_row[4]) != receipt.session_id
            or str(close_row[5]) != receipt.client_id
            or UUID(str(close_row[6])) != receipt.job_id
            or UUID(str(close_row[7])) != receipt.attempt_id
            or str(close_row[8]) != "closed"
            or str(close_row[9]) != idempotency_key
            or canonical_json(close_row[10]) != canonical_json(receipt.body())
            or digest(close_row[10]) != receipt.receipt_digest
            or str(close_row[11]) != receipt.receipt_digest
            or close_row[12] != receipt.closed_at
        ):
            raise PolicyViolation("Projection closure terminal close receipt drift")

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,idempotency_key,source_ref,source_digest,projection_ref,"
                " projection_digest,generator_version,classification,public_filtered,"
                " receipt_body,receipt_digest,generated_at"
                " from continuity.projection_generation_receipt"
                " where realm_id=%s and project_id=%s and work_item_id=%s"
                " and projection_ref=%s and source_digest=%s",
                (
                    self.realm_id,
                    receipt.project_id,
                    receipt.work_item_id,
                    ACTIVE_WORK_PROJECTION_REF,
                    receipt.source_digest,
                ),
            )
            projection = cursor.fetchone()
        if projection is None:
            raise PolicyViolation("Projection closure terminal projection receipt eksik")
        projection_id = UUID(str(projection[0]))
        projection_body = {
            "schema": "zekam-projection-generation-receipt/v1",
            "receipt_id": str(projection_id),
            "realm_id": str(receipt.realm_id),
            "project_id": str(receipt.project_id),
            "work_item_id": str(receipt.work_item_id),
            "source_ref": f"work-item/{work.id}/revision/{work.revision}",
            "source_digest": receipt.source_digest,
            "projection_ref": ACTIVE_WORK_PROJECTION_REF,
            "projection_digest": str(projection[5]),
            "generator_version": PROJECTION_CLOSURE_GENERATOR,
            "generated_at": receipt.closed_at,
            "classification": "public",
            "public_filtered": True,
            "grants_authority": False,
        }
        projection_receipt_digest = digest(projection_body)
        if (
            str(projection[1])
            != f"projection-close/{receipt.work_item_id}/{receipt.source_digest[7:]}"
            or str(projection[2]) != projection_body["source_ref"]
            or str(projection[3]) != receipt.source_digest
            or str(projection[4]) != ACTIVE_WORK_PROJECTION_REF
            or str(projection[6]) != PROJECTION_CLOSURE_GENERATOR
            or str(projection[7]) != "public"
            or not bool(projection[8])
            or canonical_json(projection[9]) != canonical_json(projection_body)
            or str(projection[10]) != projection_receipt_digest
            or projection[11] != receipt.closed_at
        ):
            raise PolicyViolation("Projection closure terminal projection receipt drift")

        expected_event |= {"projection_receipt_digest": projection_receipt_digest}
        event = events[0]
        if (
            canonical_json(event[0]) != canonical_json(expected_event)
            or str(event[1]) != digest(expected_event)
            or UUID(str(event[2])) != authorization.actor_id
            or event[3] != effect[5]
        ):
            raise PolicyViolation("Projection closure terminal Work event drift")

        expected_adapter_evidence = digest(
            {
                "close_receipt_digest": receipt.receipt_digest,
                "projection_receipt_digest": projection_receipt_digest,
                "completed_work_record_digest": work.record_digest,
            }
        )
        if str(effect[4]) != expected_adapter_evidence:
            raise PolicyViolation("Projection closure terminal effect evidence drift")
        expected_execution_event["projection_receipt_digest"] = projection_receipt_digest
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select payload,occurred_at from runtime.execution_event"
                " where realm_id=%s and job_id=%s and attempt_id=%s"
                " and event_type='job.completed' order by occurred_at,id",
                (self.realm_id, receipt.job_id, receipt.attempt_id),
            )
            execution_events = tuple(cursor.fetchall())
        if (
            len(execution_events) != 1
            or canonical_json(execution_events[0][0])
            != canonical_json(expected_execution_event)
            or execution_events[0][1] != effect[5]
        ):
            raise PolicyViolation("Projection closure terminal execution event drift")

        identity = (
            self.realm_id,
            receipt.project_id,
            receipt.work_item_id,
            receipt.run_id,
            receipt.session_id,
            receipt.client_id,
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select event.id,event.event_digest,event.sequence,event.previous_digest,"
                " event.event_body,outbox.plan_digest,outbox.payload_digest,outbox.state,"
                " outbox.terminal_receipt_digest,outbox.completed_at,outbox.id"
                " from continuity.session_lifecycle_event event"
                " join continuity.lifecycle_delivery_outbox outbox"
                " on outbox.realm_id=event.realm_id and outbox.event_id=event.id"
                " where event.realm_id=%s and event.project_id=%s"
                " and event.work_item_id=%s and event.run_id=%s"
                " and event.session_id=%s and event.client_id=%s"
                " and event.event_type='pre_close'"
                " order by event.sequence desc,event.id desc limit 1",
                identity,
            )
            pre_close = cursor.fetchone()
            if pre_close is not None and int(pre_close[2]) > 1:
                cursor.execute(
                    "select event_digest from continuity.session_lifecycle_event"
                    " where realm_id=%s and project_id=%s and work_item_id=%s and run_id=%s"
                    " and session_id=%s and client_id=%s and sequence=%s",
                    (*identity, int(pre_close[2]) - 1),
                )
                previous = cursor.fetchone()
            else:
                previous = None
        if pre_close is None or not isinstance(pre_close[4], dict):
            raise PolicyViolation("Projection closure terminal pre_close outbox eksik")
        previous_digest = None if pre_close[3] is None else str(pre_close[3])
        if (
            (int(pre_close[2]) == 1) != (previous_digest is None)
            or (
                int(pre_close[2]) > 1
                and (previous is None or str(previous[0]) != previous_digest)
            )
            or pre_close[4].get("checkpoint_ref") != receipt.checkpoint_ref.ref
            or pre_close[4].get("plan_ref") != f"work-plan:{task_plan_id}"
            or str(pre_close[4].get("source_revision")) != str(task_plan[2])
            or str(pre_close[6])
            != digest(
                {
                    "event_digest": str(pre_close[1]),
                    "plan_digest": str(pre_close[5]),
                }
            )
            or str(pre_close[7]) != "completed"
            or str(pre_close[8]) != receipt.receipt_digest
            or pre_close[9] != effect[5]
        ):
            raise PolicyViolation("Projection closure terminal pre_close binding drift")

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select mode,expected_work_revision,expected_work_record_digest,"
                " plan_id,plan_digest,job_id,attempt_id,claim_id,authorization_id,run_id,"
                " close_receipt_id,projection_receipt_id,pre_close_outbox_id,"
                " checkpoint_id,effect_receipt_id,operation,admission_body,admission_digest,"
                " admitted_at,consumed_at,checkpoint.checkpoint_digest"
                " from work.completion_admission admission"
                " join work.checkpoint checkpoint on checkpoint.realm_id=admission.realm_id"
                " and checkpoint.id=admission.checkpoint_id"
                " where admission.realm_id=%s and admission.work_item_id=%s"
                " and admission.expected_work_revision=%s",
                (self.realm_id, work.id, work.revision),
            )
            admissions = tuple(cursor.fetchall())
        if len(admissions) != 1:
            raise PolicyViolation("Projection closure terminal admission count drift")
        admission = admissions[0]
        if (
            str(admission[0]) != "projection-aware"
            or int(admission[1]) != work.revision
            or str(admission[2]) != work.record_digest
            or UUID(str(admission[3])) != task_plan_id
            or str(admission[4]) != plan_digest
            or UUID(str(admission[5])) != receipt.job_id
            or UUID(str(admission[6])) != receipt.attempt_id
            or UUID(str(admission[7])) != claim_id
            or UUID(str(admission[8])) != authorization.id
            or UUID(str(admission[9])) != receipt.run_id
            or UUID(str(admission[10])) != receipt.receipt_id
            or UUID(str(admission[11])) != projection_id
            or UUID(str(admission[12])) != UUID(str(pre_close[10]))
            or UUID(str(admission[14])) != UUID(str(effect[0]))
            or str(admission[15]) != PROJECTION_CLOSURE_OPERATION
            or digest(admission[16]) != str(admission[17])
            or admission[18] != effect[5]
            or admission[19] != effect[5]
            or str(admission[20]) != receipt.checkpoint_ref.digest_value
        ):
            raise PolicyViolation("Projection closure terminal admission binding drift")

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select"
                " (select count(*) from runtime.lease lease"
                "   join runtime.job job on job.realm_id=lease.realm_id and job.id=lease.job_id"
                "   where lease.realm_id=%s and job.run_id=%s),"
                " (select count(*) from runtime.resource_lock held_lock"
                "   join runtime.job job on job.realm_id=held_lock.realm_id"
                "    and job.id=held_lock.job_id"
                "   where held_lock.realm_id=%s and job.run_id=%s),"
                " (select count(*) from runtime.claim_without_receipt pending"
                "   join runtime.job job on job.realm_id=pending.realm_id and job.id=pending.job_id"
                "   where pending.realm_id=%s and job.run_id=%s),"
                " (select count(*) from runtime.job where realm_id=%s and run_id=%s"
                "   and state in ('ready','running','blocked','recovery-required'))",
                (
                    self.realm_id,
                    receipt.run_id,
                    self.realm_id,
                    receipt.run_id,
                    self.realm_id,
                    receipt.run_id,
                    self.realm_id,
                    receipt.run_id,
                ),
            )
            terminal_counts = tuple(int(value) for value in cursor.fetchone())
        if terminal_counts != (0, 0, 0, 0):
            raise PolicyViolation("Projection closure terminal runtime cleanup drift")

        return ProjectionClosureApplyReceipt(
            work_item_id=work.id,
            work_revision=work.revision,
            close_receipt_id=receipt.receipt_id,
            close_receipt_digest=receipt.receipt_digest,
            projection_receipt_id=projection_id,
            projection_receipt_digest=projection_receipt_digest,
            effect_receipt_id=UUID(str(effect[0])),
            result_digest=result_digest,
            plan_digest=plan_digest,
            replayed=True,
            applied_at=effect[5],
        )

    def apply_closure(
        self,
        plan: ProjectionClosurePlan,
        *,
        authorization: Authorization,
        claim_id: UUID,
        applied_at: dt.datetime,
    ) -> ProjectionClosureApplyReceipt:
        """Write every terminal head/receipt inside the caller's transaction."""
        plan.assert_integrity()
        with self.connection.cursor() as cursor:
            cursor.execute("select statement_timestamp()")
            database_now = cursor.fetchone()
            if database_now is None:
                raise ConcurrencyConflict("Projection closure DB zamani okunamadi")
            applied_at = database_now[0]
            cursor.execute(
                "select consumed_at from security.authorization"
                " where realm_id=%s and id=%s and state='consumed'"
                " and consumed_by='projection-aware-close/v1'"
                " and consumed_at is not null and consumed_at<=%s",
                (self.realm_id, authorization.id, applied_at),
            )
            if cursor.fetchone() is None:
                raise PolicyViolation("Projection closure authorization DB clock drift")
            cursor.execute(
                "select job_id,attempt_id,operation,effect_digest,authorization_digest,"
                " authorization_id,resources,fencing_token,claim_digest"
                " from runtime.effect_claim where realm_id=%s and id=%s for update",
                (self.realm_id, claim_id),
            )
            claim = cursor.fetchone()
            if claim is None:
                raise NotFound("Projection closure exact effect claim bulunamadi")
            resources = tuple(dict(item) for item in (claim[6] or ()))
            adapter_digest = self._claim_adapter_digest(cursor, claim_id)
            idempotency_key = self._claim_idempotency_key(cursor, claim_id)
            execution_identity = self._claim_execution_identity(cursor, claim_id)
            if (
                UUID(str(claim[0])) != plan.receipt.job_id
                or UUID(str(claim[1])) != plan.receipt.attempt_id
                or str(claim[2]) != PROJECTION_CLOSURE_OPERATION
                or str(claim[3]) != plan.effect_digest
                or str(claim[4]) != authorization.authorization_digest
                or claim[5] is None
                or UUID(str(claim[5])) != authorization.id
                or int(claim[7]) != plan.receipt.fencing_token
                or resources != ({"resource": plan.resource, "mode": "write"},)
                or adapter_digest != PROJECTION_CLOSURE_ADAPTER_DIGEST
                or idempotency_key != plan.claim_idempotency_key
                or execution_identity != plan.execution_identity
            ):
                raise PolicyViolation("Projection closure claim/authorization/fence drift")
            claim_body = {
                "job_id": str(plan.receipt.job_id),
                "operation": str(claim[2]),
                "effect_digest": str(claim[3]),
                "authorization_digest": str(claim[4]),
                "idempotency_key": idempotency_key,
                "resources": list(resources),
                "execution_identity": execution_identity,
                "fencing_token": int(claim[7]),
                "adapter_digest": adapter_digest,
            }
            if str(claim[8]) != digest(claim_body):
                raise PolicyViolation("Projection closure immutable claim digest drift")
            cursor.execute(
                "select id,status,result_digest from runtime.effect_receipt"
                " where realm_id=%s and claim_id=%s",
                (self.realm_id, claim_id),
            )
            if cursor.fetchone() is not None:
                raise PolicyViolation("Projection closure claim zaten terminal receipt tasiyor")
            cursor.execute(
                "select claim_id from runtime.claim_without_receipt"
                " where realm_id=%s and job_id=%s order by claim_id",
                (self.realm_id, plan.receipt.job_id),
            )
            pending_claim_ids = tuple(UUID(str(row[0])) for row in cursor.fetchall())
            if pending_claim_ids != (claim_id,):
                raise PolicyViolation(
                    "Projection closure job exact tek receiptless claim tasimali"
                )

        continuity = MemoryContinuityRepository(self.connection, self.realm_id)
        projection_created = continuity.store_projection_receipt(
            plan.projection_receipt,
            idempotency_key=(
                f"projection-close/{plan.receipt.work_item_id}/"
                f"{plan.projection_receipt.source_digest[7:]}"
            ),
        )
        close_created = continuity.store_close_receipt(
            plan.receipt,
            idempotency_key=plan.idempotency_key,
        )
        if not projection_created or not close_created:
            raise ConcurrencyConflict("Projection closure terminal receipt replay drift")

        effect_receipt_id = uuid5(_EFFECT_NAMESPACE, str(claim_id))
        adapter_evidence_digest = digest(
            {
                "close_receipt_digest": plan.receipt.receipt_digest,
                "projection_receipt_digest": plan.projection_receipt.receipt_digest,
                "completed_work_record_digest": plan.completed_work.record_digest,
            }
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                "update continuity.lifecycle_delivery_outbox"
                " set state='completed',terminal_receipt_digest=%s,completed_at=%s"
                " where realm_id=%s and id=%s and event_id=%s"
                " and plan_digest=%s and payload_digest=%s"
                " and state in ('pending','processing') and terminal_receipt_digest is null",
                (
                    plan.receipt.receipt_digest,
                    applied_at,
                    self.realm_id,
                    plan.pre_close_outbox_id,
                    plan.pre_close_event_id,
                    plan.pre_close_outbox_plan_digest,
                    plan.pre_close_outbox_payload_digest,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("Projection closure pre_close outbox finalize drift")
            cursor.execute(
                "insert into runtime.effect_receipt"
                " (id,realm_id,claim_id,status,result_digest,adapter_evidence_digest,"
                " token_count,cost_micros,latency_ms,completed_at)"
                " values (%s,%s,%s,'completed',%s,%s,0,0,0,%s)",
                (
                    effect_receipt_id,
                    self.realm_id,
                    claim_id,
                    plan.result_digest,
                    adapter_evidence_digest,
                    applied_at,
                ),
            )
            cursor.execute(
                "select id from work.checkpoint"
                " where realm_id=%s and project_id=%s and work_item_id=%s"
                " and task_plan_id=%s and job_id=%s and checkpoint_digest=%s"
                " and (checkpoint_key=%s or 'db:work.checkpoint/'||id::text=%s)"
                " order by created_at desc,id desc limit 1",
                (
                    self.realm_id,
                    plan.receipt.project_id,
                    plan.receipt.work_item_id,
                    plan.task_plan_id,
                    plan.receipt.job_id,
                    plan.receipt.checkpoint_ref.digest_value,
                    plan.receipt.checkpoint_ref.ref,
                    plan.receipt.checkpoint_ref.ref,
                ),
            )
            checkpoint = cursor.fetchone()
            if checkpoint is None:
                raise ConcurrencyConflict("Projection closure exact checkpoint kayboldu")
            checkpoint_id = UUID(str(checkpoint[0]))
            cursor.execute(
                "select work.admit_projection_completion("
                "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    self.realm_id,
                    plan.receipt.project_id,
                    plan.receipt.work_item_id,
                    plan.completed_work.revision,
                    plan.completed_work.record_digest,
                    plan.task_plan_id,
                    plan.plan_digest,
                    plan.receipt.job_id,
                    plan.receipt.attempt_id,
                    claim_id,
                    authorization.id,
                    plan.receipt.run_id,
                    plan.receipt.receipt_id,
                    plan.projection_receipt.receipt_id,
                    plan.pre_close_outbox_id,
                    checkpoint_id,
                    effect_receipt_id,
                    PROJECTION_CLOSURE_OPERATION,
                ),
            )
            if cursor.fetchone() is None:
                raise ConcurrencyConflict("Projection closure admission uretilmedi")

        items = WorkItemRepository(self.connection, self.realm_id)
        items.replace(plan.completed_work, expected_revision=plan.completed_work.revision - 1)
        revision = RevisionStore(self.connection, self.realm_id).append(
            entity_type=WORK_ENTITY_TYPE,
            entity_id=plan.completed_work.id,
            payload=plan.completed_work.body(),
            reason="projection-aware close verified",
            actor_id=authorization.actor_id,
            expected_revision=plan.completed_work.revision - 1,
            now=applied_at,
        )
        if revision.revision != plan.completed_work.revision:
            raise ConcurrencyConflict("Projection closure Work revision zinciri drift")
        EventStore(self.connection, self.realm_id).append(
            event_type="work.state.completed",
            entity_type=WORK_ENTITY_TYPE,
            entity_id=plan.completed_work.id,
            payload={
                "state": "completed",
                "revision": plan.completed_work.revision,
                "close_receipt_digest": plan.receipt.receipt_digest,
                "projection_receipt_digest": plan.projection_receipt.receipt_digest,
            },
            revision_id=revision.id,
            actor_id=authorization.actor_id,
            occurred_at=applied_at,
        )

        with self.connection.cursor() as cursor:
            cursor.execute(
                "update runtime.job_attempt set outcome='succeeded',result_digest=%s,"
                " finished_at=%s where realm_id=%s and id=%s and job_id=%s"
                " and fencing_token=%s and outcome is null",
                (
                    plan.result_digest,
                    applied_at,
                    self.realm_id,
                    plan.receipt.attempt_id,
                    plan.receipt.job_id,
                    plan.receipt.fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("Projection closure attempt terminal drift")
            cursor.execute(
                "delete from runtime.resource_lock where realm_id=%s and job_id=%s"
                " and resource=%s and mode='write'",
                (self.realm_id, plan.receipt.job_id, plan.resource),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("Projection closure logical lock release drift")
            cursor.execute(
                "delete from runtime.lease where realm_id=%s and job_id=%s"
                " and attempt_id=%s and fencing_token=%s",
                (
                    self.realm_id,
                    plan.receipt.job_id,
                    plan.receipt.attempt_id,
                    plan.receipt.fencing_token,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("Projection closure lease release drift")
            cursor.execute(
                "update runtime.job set state='completed' where realm_id=%s and id=%s"
                " and state='running' and fencing_token=%s",
                (self.realm_id, plan.receipt.job_id, plan.receipt.fencing_token),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("Projection closure job terminal drift")
            cursor.execute(
                "update runtime.execution_run set state='completed',terminal_at=%s"
                " where realm_id=%s and id=%s and state='active'",
                (applied_at, self.realm_id, plan.receipt.run_id),
            )
            if cursor.rowcount != 1:
                raise ConcurrencyConflict("Projection closure execution run terminal drift")
            cursor.execute(
                "insert into runtime.execution_event"
                " (id,realm_id,job_id,attempt_id,event_type,payload,occurred_at)"
                " values (%s,%s,%s,%s,'job.completed',%s::jsonb,%s)",
                (
                    new_uuid7(now=applied_at),
                    self.realm_id,
                    plan.receipt.job_id,
                    plan.receipt.attempt_id,
                    canonical_json(
                        {
                            "outcome": "succeeded",
                            "claim_id": str(claim_id),
                            "claim_digest": str(claim[8]),
                            "claim_idempotency_key": idempotency_key,
                            "execution_identity": execution_identity,
                            "effect_receipt_id": str(effect_receipt_id),
                            "result_digest": plan.result_digest,
                            "plan_digest": plan.plan_digest,
                            "task_plan_id": str(plan.task_plan_id),
                            "resource": plan.resource,
                            "close_receipt_digest": plan.receipt.receipt_digest,
                            "projection_receipt_digest": plan.projection_receipt.receipt_digest,
                        }
                    ),
                    applied_at,
                ),
            )
        return ProjectionClosureApplyReceipt(
            work_item_id=plan.completed_work.id,
            work_revision=plan.completed_work.revision,
            close_receipt_id=plan.receipt.receipt_id,
            close_receipt_digest=plan.receipt.receipt_digest,
            projection_receipt_id=plan.projection_receipt.receipt_id,
            projection_receipt_digest=plan.projection_receipt.receipt_digest,
            effect_receipt_id=effect_receipt_id,
            result_digest=plan.result_digest,
            plan_digest=plan.plan_digest,
            replayed=False,
            applied_at=applied_at,
        )

    def _claim_idempotency_key(self, cursor: Any, claim_id: UUID) -> str:
        return self._claim_scalar(cursor, claim_id, "idempotency_key")

    def _claim_execution_identity(self, cursor: Any, claim_id: UUID) -> str:
        return self._claim_scalar(cursor, claim_id, "execution_identity")

    def _claim_adapter_digest(self, cursor: Any, claim_id: UUID) -> str:
        return self._claim_scalar(cursor, claim_id, "adapter_digest")

    def _claim_scalar(self, cursor: Any, claim_id: UUID, column: str) -> str:
        if column not in {"idempotency_key", "execution_identity", "adapter_digest"}:
            raise PolicyViolation("Projection closure claim column allowlist disinda")
        cursor.execute(
            f"select {column} from runtime.effect_claim where realm_id=%s and id=%s",
            (self.realm_id, claim_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise NotFound("Projection closure effect claim kayboldu")
        return str(row[0])
