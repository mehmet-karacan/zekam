"""Projection-aware, claim-before-effect Work closure orchestration.

The dry-run plan binds the current Work/source/migration/projection snapshot,
the latest ``pre_close`` lifecycle outbox, and the exact runtime fence.  Apply
re-reads that snapshot inside one PostgreSQL transaction before consuming the
one-shot authorization and delegating the atomic terminal writes.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace
from typing import Any, Protocol
from uuid import UUID, uuid5

from zekam.application.continuity_projection import (
    ACTIVE_WORK_PROJECTION_REF,
    ProjectionReleaseSnapshot,
)
from zekam.application.memory_upgrade import canonical_projection_source_digest
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.security import Authorization
from zekam.domain.session_continuity import (
    CloseStatus,
    ProjectionGenerationReceipt,
    SessionCloseReceipt,
)
from zekam.domain.work import EvidenceRef, WorkItem, WorkState

_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
_PROJECTION_NAMESPACE = UUID("9df8f180-3c37-5b67-900e-23a997720a2a")
PROJECTION_CLOSURE_GENERATOR = "projection-aware-close/v1"
PROJECTION_CLOSURE_OPERATION = "projection-aware-close"
PROJECTION_CLOSURE_ADAPTER_DIGEST = digest(
    {"adapter": "projection-aware-close-postgres", "revision": 1}
)


class AuthorizationStore(Protocol):
    def get(self, authorization_id: UUID) -> Authorization: ...

    def consume(
        self,
        authorization_id: UUID,
        *,
        effect_digest: str,
        consumed_by: str,
        now: dt.datetime | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class ProjectionClosureSnapshot:
    work_item: WorkItem
    release: ProjectionReleaseSnapshot
    task_plan_id: UUID
    task_plan_revision: int
    task_plan_digest: str
    task_plan_source_revision: str
    task_plan_policy_digest: str
    job_id: UUID
    attempt_id: UUID
    run_id: UUID
    lease_id: UUID
    lease_worker_label: str
    fencing_token: int
    lease_expires_at: dt.datetime
    envelope_digest: str
    checkpoint_digest: str
    lock_digest: str
    pre_close_event_id: UUID
    pre_close_event_digest: str
    pre_close_sequence: int
    pre_close_previous_digest: str | None
    pre_close_outbox_id: UUID
    pre_close_outbox_plan_digest: str
    pre_close_outbox_payload_digest: str
    other_open_job_count: int
    other_receiptless_claim_count: int
    grants_authority: bool = False

    def __post_init__(self) -> None:
        if (
            self.work_item.id != self.release.work_item_id
            or self.work_item.project_id != self.release.project_id
            or self.work_item.revision != self.release.work_revision
            or self.work_item.state.value != self.release.work_state
            or self.work_item.record_digest != self.release.work_record_digest
        ):
            raise PolicyViolation("Closure Work/release binding drift")
        if (
            self.run_id.int == 0
            or self.task_plan_revision < 1
            or self.fencing_token < 1
            or self.pre_close_sequence < 1
        ):
            raise ValidationFailed("Closure runtime/lifecycle kimligi gecersiz")
        if not self.lease_worker_label.strip() or not self.task_plan_source_revision.strip():
            raise ValidationFailed("Closure Plan/worker kimligi bos olamaz")
        if self.lease_expires_at.tzinfo is None:
            raise ValidationFailed("Closure lease expiry timezone-aware olmali")
        for value in (
            self.envelope_digest,
            self.checkpoint_digest,
            self.lock_digest,
            self.task_plan_digest,
            self.task_plan_policy_digest,
            self.pre_close_event_digest,
            self.pre_close_outbox_plan_digest,
            self.pre_close_outbox_payload_digest,
        ):
            parse_digest(value)
        if self.pre_close_previous_digest is not None:
            parse_digest(self.pre_close_previous_digest)
        if (self.pre_close_sequence == 1) != (self.pre_close_previous_digest is None):
            raise PolicyViolation("Closure pre_close sequence/previous binding drift")
        if self.pre_close_outbox_payload_digest != digest(
            {
                "event_digest": self.pre_close_event_digest,
                "plan_digest": self.pre_close_outbox_plan_digest,
            }
        ):
            raise PolicyViolation("Closure pre_close outbox payload binding drift")
        if min(self.other_open_job_count, self.other_receiptless_claim_count) < 0:
            raise ValidationFailed("Closure pending sayaci negatif olamaz")
        if self.grants_authority:
            raise PolicyViolation("Closure snapshot authority uretemez")

    def assert_ready(self, *, now: dt.datetime) -> None:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValidationFailed("Closure zamani timezone-aware olmali")
        if self.work_item.state is not WorkState.VERIFICATION:
            raise PolicyViolation("Projection-aware close Work verification durumu ister")
        unverified = tuple(
            item.text for item in self.work_item.acceptance_criteria if not item.verified
        )
        if unverified:
            raise PolicyViolation(
                f"Tum acceptance criteria verified olmadan close yapilamaz: {len(unverified)}"
            )
        if self.lease_expires_at <= now:
            raise PolicyViolation("Projection-aware close lease sona ermis")
        if self.other_open_job_count:
            raise PolicyViolation("Projection-aware close ayni run'da baska acik job tasiyor")
        if self.other_receiptless_claim_count:
            raise PolicyViolation("Projection-aware close baska receiptless claim tasiyor")
        self.release.assert_release_ready(
            expected_source_digest=self.release.expected_projection_source_digest
        )

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-projection-closure-snapshot/v1",
            "work_item_id": str(self.work_item.id),
            "work_revision": self.work_item.revision,
            "work_record_digest": self.work_item.record_digest,
            "release_snapshot_digest": self.release.snapshot_digest,
            "task_plan_id": str(self.task_plan_id),
            "task_plan_revision": self.task_plan_revision,
            "task_plan_digest": self.task_plan_digest,
            "task_plan_source_revision": self.task_plan_source_revision,
            "task_plan_policy_digest": self.task_plan_policy_digest,
            "job_id": str(self.job_id),
            "attempt_id": str(self.attempt_id),
            "run_id": str(self.run_id),
            "lease_id": str(self.lease_id),
            "lease_worker_label": self.lease_worker_label,
            "fencing_token": self.fencing_token,
            "lease_expires_at": self.lease_expires_at,
            "envelope_digest": self.envelope_digest,
            "checkpoint_digest": self.checkpoint_digest,
            "lock_digest": self.lock_digest,
            "pre_close_event_id": str(self.pre_close_event_id),
            "pre_close_event_digest": self.pre_close_event_digest,
            "pre_close_sequence": self.pre_close_sequence,
            "pre_close_previous_digest": self.pre_close_previous_digest,
            "pre_close_outbox_id": str(self.pre_close_outbox_id),
            "pre_close_outbox_plan_digest": self.pre_close_outbox_plan_digest,
            "pre_close_outbox_payload_digest": self.pre_close_outbox_payload_digest,
            "other_open_job_count": self.other_open_job_count,
            "other_receiptless_claim_count": self.other_receiptless_claim_count,
            "grants_authority": False,
        }

    @property
    def snapshot_digest(self) -> str:
        return digest(self.body())


class ProjectionClosureStore(Protocol):
    connection: Any

    def has_terminal_effect_receipt(self, claim_id: UUID) -> bool: ...

    def read_closure_snapshot(
        self,
        receipt: SessionCloseReceipt,
        *,
        lock: bool = False,
    ) -> ProjectionClosureSnapshot: ...

    def apply_closure(
        self,
        plan: ProjectionClosurePlan,
        *,
        authorization: Authorization,
        claim_id: UUID,
        applied_at: dt.datetime,
    ) -> ProjectionClosureApplyReceipt: ...

    def replay_completed_closure(
        self,
        receipt: SessionCloseReceipt,
        *,
        idempotency_key: str,
        plan_digest: str,
        authorization: Authorization,
        claim_id: UUID,
    ) -> ProjectionClosureApplyReceipt | None: ...


@dataclass(frozen=True, slots=True)
class ProjectionClosurePlan:
    receipt: SessionCloseReceipt
    completed_work: WorkItem
    projection_receipt: ProjectionGenerationReceipt
    idempotency_key: str
    snapshot_digest: str
    task_plan_id: UUID
    task_plan_revision: int
    task_plan_digest: str
    pre_close_event_id: UUID
    pre_close_event_digest: str
    pre_close_sequence: int
    pre_close_previous_digest: str | None
    pre_close_outbox_id: UUID
    pre_close_outbox_plan_digest: str
    pre_close_outbox_payload_digest: str
    resource: str
    claim_idempotency_key: str
    execution_identity: str
    result_digest: str
    effect_digest: str
    plan_digest: str
    grants_authority: bool = False

    @classmethod
    def create(
        cls,
        *,
        receipt: SessionCloseReceipt,
        completed_work: WorkItem,
        projection_receipt: ProjectionGenerationReceipt,
        idempotency_key: str,
        snapshot: ProjectionClosureSnapshot,
    ) -> ProjectionClosurePlan:
        key = idempotency_key.strip()
        if not _KEY.fullmatch(key):
            raise ValidationFailed("Projection closure idempotency key gecersiz")
        snapshot_digest = snapshot.snapshot_digest
        parse_digest(snapshot_digest)
        resource = (
            f"work:{receipt.project_id}:{receipt.work_item_id}:projection-close:{receipt.run_id}"
        )
        result_digest = digest(
            {
                "schema": "zekam-projection-closure-result/v1",
                "close_receipt_digest": receipt.receipt_digest,
                "completed_work_record_digest": completed_work.record_digest,
                "projection_receipt_digest": projection_receipt.receipt_digest,
                "projection_digest": projection_receipt.projection_digest,
                "snapshot_digest": snapshot_digest,
            }
        )
        effect_digest = digest(
            {
                "effect": "database-write",
                "operation": PROJECTION_CLOSURE_OPERATION,
                "resource": resource,
                "result_digest": result_digest,
            }
        )
        claim_idempotency_key = digest(
            {
                "operation": PROJECTION_CLOSURE_OPERATION,
                "job_id": str(receipt.job_id),
                "effect_digest": effect_digest,
                "idempotency_key": key,
            }
        )
        draft = cls(
            receipt=receipt,
            completed_work=completed_work,
            projection_receipt=projection_receipt,
            idempotency_key=key,
            snapshot_digest=snapshot_digest,
            task_plan_id=snapshot.task_plan_id,
            task_plan_revision=snapshot.task_plan_revision,
            task_plan_digest=snapshot.task_plan_digest,
            pre_close_event_id=snapshot.pre_close_event_id,
            pre_close_event_digest=snapshot.pre_close_event_digest,
            pre_close_sequence=snapshot.pre_close_sequence,
            pre_close_previous_digest=snapshot.pre_close_previous_digest,
            pre_close_outbox_id=snapshot.pre_close_outbox_id,
            pre_close_outbox_plan_digest=snapshot.pre_close_outbox_plan_digest,
            pre_close_outbox_payload_digest=snapshot.pre_close_outbox_payload_digest,
            resource=resource,
            claim_idempotency_key=claim_idempotency_key,
            execution_identity=(
                f"{snapshot.lease_worker_label}:{snapshot.fencing_token}"
            ),
            result_digest=result_digest,
            effect_digest=effect_digest,
            plan_digest="",
        )
        return replace(draft, plan_digest=digest(draft.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-projection-aware-close-plan/v1",
            "work_item_id": str(self.receipt.work_item_id),
            "run_id": str(self.receipt.run_id),
            "job_id": str(self.receipt.job_id),
            "attempt_id": str(self.receipt.attempt_id),
            "fencing_token": self.receipt.fencing_token,
            "close_receipt_id": str(self.receipt.receipt_id),
            "close_receipt_digest": self.receipt.receipt_digest,
            "completed_work_revision": self.completed_work.revision,
            "completed_work_record_digest": self.completed_work.record_digest,
            "projection_receipt_id": str(self.projection_receipt.receipt_id),
            "projection_receipt_digest": self.projection_receipt.receipt_digest,
            "projection_source_digest": self.projection_receipt.source_digest,
            "projection_digest": self.projection_receipt.projection_digest,
            "projection_ref": self.projection_receipt.projection_ref,
            "idempotency_key": self.idempotency_key,
            "snapshot_digest": self.snapshot_digest,
            "task_plan_id": str(self.task_plan_id),
            "task_plan_revision": self.task_plan_revision,
            "task_plan_digest": self.task_plan_digest,
            "pre_close_event_id": str(self.pre_close_event_id),
            "pre_close_event_digest": self.pre_close_event_digest,
            "pre_close_sequence": self.pre_close_sequence,
            "pre_close_previous_digest": self.pre_close_previous_digest,
            "pre_close_outbox_id": str(self.pre_close_outbox_id),
            "pre_close_outbox_plan_digest": self.pre_close_outbox_plan_digest,
            "pre_close_outbox_payload_digest": self.pre_close_outbox_payload_digest,
            "resource": self.resource,
            "claim_idempotency_key": self.claim_idempotency_key,
            "execution_identity": self.execution_identity,
            "operation": PROJECTION_CLOSURE_OPERATION,
            "adapter_digest": PROJECTION_CLOSURE_ADAPTER_DIGEST,
            "result_digest": self.result_digest,
            "effect_digest": self.effect_digest,
            "requires_authorization": True,
            "grants_authority": False,
        }

    def assert_integrity(self) -> None:
        for value in (
            self.snapshot_digest,
            self.task_plan_digest,
            self.pre_close_event_digest,
            self.pre_close_outbox_plan_digest,
            self.pre_close_outbox_payload_digest,
            self.claim_idempotency_key,
            self.result_digest,
            self.effect_digest,
            self.plan_digest,
        ):
            parse_digest(value)
        if self.pre_close_previous_digest is not None:
            parse_digest(self.pre_close_previous_digest)
        expected_resource = (
            f"work:{self.receipt.project_id}:{self.receipt.work_item_id}:"
            f"projection-close:{self.receipt.run_id}"
        )
        expected_result = digest(
            {
                "schema": "zekam-projection-closure-result/v1",
                "close_receipt_digest": self.receipt.receipt_digest,
                "completed_work_record_digest": self.completed_work.record_digest,
                "projection_receipt_digest": self.projection_receipt.receipt_digest,
                "projection_digest": self.projection_receipt.projection_digest,
                "snapshot_digest": self.snapshot_digest,
            }
        )
        expected_effect = digest(
            {
                "effect": "database-write",
                "operation": PROJECTION_CLOSURE_OPERATION,
                "resource": expected_resource,
                "result_digest": expected_result,
            }
        )
        expected_claim_idempotency = digest(
            {
                "operation": PROJECTION_CLOSURE_OPERATION,
                "job_id": str(self.receipt.job_id),
                "effect_digest": expected_effect,
                "idempotency_key": self.idempotency_key,
            }
        )
        identities_match = (
            self.completed_work.realm_id == self.receipt.realm_id
            and self.completed_work.project_id == self.receipt.project_id
            and self.completed_work.id == self.receipt.work_item_id
            and self.projection_receipt.realm_id == self.receipt.realm_id
            and self.projection_receipt.project_id == self.receipt.project_id
            and self.projection_receipt.work_item_id == self.receipt.work_item_id
        )
        if (
            not identities_match
            or not _KEY.fullmatch(self.idempotency_key)
            or self.task_plan_revision < 1
            or self.grants_authority
            or self.pre_close_sequence < 1
            or (self.pre_close_sequence == 1)
            != (self.pre_close_previous_digest is None)
            or self.pre_close_outbox_payload_digest
            != digest(
                {
                    "event_digest": self.pre_close_event_digest,
                    "plan_digest": self.pre_close_outbox_plan_digest,
                }
            )
            or self.completed_work.state is not WorkState.COMPLETED
            or self.receipt.status is not CloseStatus.CLOSED
            or self.receipt.next_safe_action is not None
            or bool(self.receipt.pending_steps)
            or self.receipt.source_digest != self.projection_receipt.source_digest
            or self.projection_receipt.projection_ref != ACTIVE_WORK_PROJECTION_REF
            or self.resource != expected_resource
            or self.claim_idempotency_key != expected_claim_idempotency
            or not self.execution_identity.endswith(f":{self.receipt.fencing_token}")
            or self.result_digest != expected_result
            or self.effect_digest != expected_effect
            or self.plan_digest != digest(self.body())
        ):
            raise PolicyViolation("Projection closure plan exact binding/digest drift")


@dataclass(frozen=True, slots=True)
class ProjectionClosureApplyReceipt:
    work_item_id: UUID
    work_revision: int
    close_receipt_id: UUID
    close_receipt_digest: str
    projection_receipt_id: UUID
    projection_receipt_digest: str
    effect_receipt_id: UUID
    result_digest: str
    plan_digest: str
    replayed: bool
    applied_at: dt.datetime
    grants_authority: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-projection-aware-close-apply-receipt/v1",
            "work_item_id": str(self.work_item_id),
            "work_revision": self.work_revision,
            "close_receipt_id": str(self.close_receipt_id),
            "close_receipt_digest": self.close_receipt_digest,
            "projection_receipt_id": str(self.projection_receipt_id),
            "projection_receipt_digest": self.projection_receipt_digest,
            "effect_receipt_id": str(self.effect_receipt_id),
            "result_digest": self.result_digest,
            "plan_digest": self.plan_digest,
            "replayed": self.replayed,
            "applied_at": self.applied_at,
            "next_safe_action": None,
            "grants_authority": False,
        }


def _completed_projection(
    receipt: SessionCloseReceipt,
    completed: WorkItem,
    release: ProjectionReleaseSnapshot,
) -> ProjectionGenerationReceipt:
    database_revision_digest = digest(
        {
            "project_id": str(completed.project_id),
            "work_item_id": str(completed.id),
            "work_revision": completed.revision,
            "work_state": completed.state.value,
            "work_record_digest": completed.record_digest,
        }
    )
    source_digest = canonical_projection_source_digest(
        source_head=release.source_head,
        source_tree_digest=release.source_tree_digest,
        migration_head=release.migration_head,
        database_revision_digest=database_revision_digest,
    )
    if receipt.source_digest != source_digest:
        raise PolicyViolation(
            "Close receipt completed-state projection source digest ile eslesmiyor"
        )
    projection_body = {
        "schema": "zekam-memory-continuity-public-projection/v1",
        "project_id": str(completed.project_id),
        "work_item_id": str(completed.id),
        "work_revision": completed.revision,
        "work_state": completed.state.value,
        "source_head": release.source_head,
        "source_tree_digest": release.source_tree_digest,
        "migration_head": release.migration_head,
        "database_revision_digest": database_revision_digest,
        "source_digest": source_digest,
        "classification": "public",
        "public_filtered": True,
        "content_included": False,
        "fresh": True,
        "read_only": True,
        "grants_authority": False,
    }
    return ProjectionGenerationReceipt(
        receipt_id=uuid5(_PROJECTION_NAMESPACE, f"{receipt.receipt_id}:completed"),
        realm_id=receipt.realm_id,
        project_id=receipt.project_id,
        work_item_id=receipt.work_item_id,
        source_ref=f"work-item/{completed.id}/revision/{completed.revision}",
        source_digest=source_digest,
        projection_ref=ACTIVE_WORK_PROJECTION_REF,
        projection_digest=digest(projection_body),
        generator_version=PROJECTION_CLOSURE_GENERATOR,
        generated_at=receipt.closed_at,
    )


@dataclass(frozen=True, slots=True)
class ProjectionAwareClosureService:
    repository: ProjectionClosureStore
    authorizations: AuthorizationStore

    def prepare(
        self,
        receipt: SessionCloseReceipt,
        *,
        idempotency_key: str,
        now: dt.datetime | None = None,
    ) -> ProjectionClosurePlan:
        moment = now or dt.datetime.now(dt.UTC)
        if receipt.status is not CloseStatus.CLOSED:
            raise PolicyViolation("Projection-aware Work completion closed receipt ister")
        if receipt.next_safe_action is not None or receipt.pending_steps:
            raise PolicyViolation("Completed close next-safe-action/pending step tasiyamaz")
        if not receipt.verified_outcomes:
            raise PolicyViolation("Projection-aware Work completion verified outcome ister")
        with self.repository.connection.transaction():
            with self.repository.connection.cursor() as cursor:
                cursor.execute(
                    "set transaction isolation level repeatable read read only"
                )
            snapshot = self.repository.read_closure_snapshot(receipt)
        snapshot.assert_ready(now=moment)
        if receipt.closed_at < snapshot.work_item.updated_at or receipt.closed_at > moment:
            raise PolicyViolation("Projection close receipt zamani Work/apply sirasi ile uyusmuyor")
        evidence = EvidenceRef(
            kind="closure-checkpoint",
            reference=receipt.checkpoint_ref.ref,
            digest_value=receipt.checkpoint_ref.digest_value,
        )
        completed = snapshot.work_item.with_state(
            WorkState.COMPLETED,
            evidence=(evidence,),
            now=receipt.closed_at,
        )
        projection = _completed_projection(receipt, completed, snapshot.release)
        return ProjectionClosurePlan.create(
            receipt=receipt,
            completed_work=completed,
            projection_receipt=projection,
            idempotency_key=idempotency_key,
            snapshot=snapshot,
        )

    def apply(
        self,
        plan: ProjectionClosurePlan,
        *,
        authorization_id: UUID,
        claim_id: UUID,
        now: dt.datetime | None = None,
    ) -> ProjectionClosureApplyReceipt:
        moment = now or dt.datetime.now(dt.UTC)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValidationFailed("Projection closure apply zamani timezone-aware olmali")
        plan.assert_integrity()
        replay = self.replay_completed(
            plan.receipt,
            idempotency_key=plan.idempotency_key,
            plan_digest=plan.plan_digest,
            authorization_id=authorization_id,
            claim_id=claim_id,
        )
        if replay is not None:
            return replay
        try:
            with self.repository.connection.transaction():
                with self.repository.connection.cursor() as cursor:
                    cursor.execute("set transaction isolation level serializable")
                current = self.repository.read_closure_snapshot(plan.receipt, lock=True)
                current.assert_ready(now=moment)
                if not plan.receipt.verified_outcomes:
                    raise PolicyViolation("Projection closure verified outcome ister")
                if (
                    plan.receipt.closed_at < current.work_item.updated_at
                    or plan.receipt.closed_at > moment
                ):
                    raise PolicyViolation(
                        "Projection close receipt zamani current Work ile uyusmuyor"
                    )
                if current.snapshot_digest != plan.snapshot_digest:
                    raise PolicyViolation("Projection closure snapshot stale; replan required")
                expected_completed = current.work_item.with_state(
                    WorkState.COMPLETED,
                    evidence=(
                        EvidenceRef(
                            kind="closure-checkpoint",
                            reference=plan.receipt.checkpoint_ref.ref,
                            digest_value=plan.receipt.checkpoint_ref.digest_value,
                        ),
                    ),
                    now=plan.receipt.closed_at,
                )
                expected_projection = _completed_projection(
                    plan.receipt, expected_completed, current.release
                )
                if (
                    plan.completed_work != expected_completed
                    or plan.projection_receipt != expected_projection
                    or plan.task_plan_id != current.task_plan_id
                    or plan.task_plan_revision != current.task_plan_revision
                    or plan.task_plan_digest != current.task_plan_digest
                    or plan.pre_close_event_id != current.pre_close_event_id
                    or plan.pre_close_event_digest != current.pre_close_event_digest
                    or plan.pre_close_sequence != current.pre_close_sequence
                    or plan.pre_close_previous_digest != current.pre_close_previous_digest
                    or plan.pre_close_outbox_id != current.pre_close_outbox_id
                    or plan.pre_close_outbox_plan_digest
                    != current.pre_close_outbox_plan_digest
                    or plan.pre_close_outbox_payload_digest
                    != current.pre_close_outbox_payload_digest
                ):
                    raise PolicyViolation(
                        "Projection closure plan/current lifecycle identity drift"
                    )
                authorization = self.authorizations.get(authorization_id)
                rejection = authorization.rejection_reason(moment)
                if (
                    rejection is not None
                    or authorization.realm_id != plan.receipt.realm_id
                    or authorization.work_item_id != plan.receipt.work_item_id
                    or authorization.plan_id != current.task_plan_id
                    or authorization.plan_digest != plan.plan_digest
                    or authorization.effect_digest != plan.effect_digest
                    or tuple(authorization.scope.allowed_resources) != (plan.resource,)
                    or tuple(authorization.scope.allowed_effects) != ("database-write",)
                    or authorization.scope.provider_refs
                    or authorization.scope.secret_ref_ids
                    or authorization.scope.data_classifications
                ):
                    raise AuthorizationRequired(
                        "Projection closure exact authorization binding yok: "
                        f"{rejection or 'scope-mismatch'}"
                    )
                consumed = self.authorizations.consume(
                    authorization_id,
                    effect_digest=plan.effect_digest,
                    consumed_by="projection-aware-close/v1",
                    now=moment,
                )
                if not bool(getattr(consumed, "consumed", False)):
                    raise AuthorizationRequired(
                        "Projection closure authorization tuketilemedi: "
                        f"{getattr(consumed, 'reason', 'unknown')}"
                    )
                return self.repository.apply_closure(
                    plan,
                    authorization=authorization,
                    claim_id=claim_id,
                    applied_at=moment,
                )
        except Exception:
            # The effect is never retried here.  A concurrent winner may have
            # committed after the first lookup; only its fully verified exact
            # terminal chain can turn the failed apply into a replay receipt.
            replay = self.replay_completed(
                plan.receipt,
                idempotency_key=plan.idempotency_key,
                plan_digest=plan.plan_digest,
                authorization_id=authorization_id,
                claim_id=claim_id,
            )
            if replay is not None:
                return replay
            raise

    def replay_completed(
        self,
        receipt: SessionCloseReceipt,
        *,
        idempotency_key: str,
        plan_digest: str,
        authorization_id: UUID,
        claim_id: UUID,
    ) -> ProjectionClosureApplyReceipt | None:
        """Return only a fully verified terminal replay; never repeat an effect."""
        key = idempotency_key.strip()
        if not _KEY.fullmatch(key):
            raise ValidationFailed("Projection closure idempotency key gecersiz")
        parse_digest(plan_digest)
        if not self.repository.has_terminal_effect_receipt(claim_id):
            return None
        authorization = self.authorizations.get(authorization_id)
        return self.repository.replay_completed_closure(
            receipt,
            idempotency_key=key,
            plan_digest=plan_digest,
            authorization=authorization,
            claim_id=claim_id,
        )
