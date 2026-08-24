"""PostgreSQL primitives for atomic Memory v2 promotion."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import NotFound, PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.memory import (
    MemoryCandidate,
    MemoryClass,
    MemoryEvidence,
    MemoryKey,
    MemoryRecord,
    MemoryScope,
    MemoryState,
)
from zekam.domain.memory_promotion import (
    MemoryPromotionPlan,
    MemoryPromotionReceipt,
)


@dataclass(frozen=True, slots=True)
class LockedMemoryPromotion:
    candidate: MemoryCandidate
    candidate_storage_id: UUID
    project_id: UUID | None
    work_item_id: UUID | None
    predecessor: MemoryRecord | None
    predecessor_storage_id: UUID | None


@dataclass(frozen=True, slots=True)
class MemoryPromotionRepository:
    connection: Any
    realm_id: UUID
    realm_ref: str

    def snapshot(
        self,
        *,
        candidate_id: str,
        logical_memory_id: str,
        expected_predecessor_storage_id: UUID | None,
        lock: bool = False,
    ) -> LockedMemoryPromotion:
        suffix = " for update" if lock else ""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,logical_candidate_id,scope,project_id,work_item_id,project_ref,"
                "work_ref,memory_class,content,author_ref,evidence,occurrence_key,"
                "observation_count,created_at,promoted_record_id"
                " from memory.candidate where realm_id=%s and logical_candidate_id=%s" + suffix,
                (self.realm_id, candidate_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise NotFound("Memory promotion candidate bulunamadi")
            if row[14] is not None:
                raise PolicyViolation("Memory candidate daha once promote edildi")
            candidate = self._candidate(row)

            cursor.execute(
                "select id,logical_memory_id,scope,project_ref,work_ref,memory_class,content,"
                "state,revision,evidence,entities,valid_from,valid_until,author_ref,reviewed_by,"
                "superseded_by,last_used_at,record_digest,created_at"
                " from memory.record where realm_id=%s and logical_memory_id=%s"
                " and state='active'" + suffix,
                (self.realm_id, logical_memory_id),
            )
            predecessor_row = cursor.fetchone()
            predecessor_storage_id = (
                None if predecessor_row is None else UUID(str(predecessor_row[0]))
            )
            if predecessor_storage_id != expected_predecessor_storage_id:
                if predecessor_storage_id is None:
                    raise PolicyViolation("Expected memory predecessor artik aktif degil")
                if expected_predecessor_storage_id is None:
                    raise PolicyViolation("Aktif memory ailesi exact predecessor ister")
                raise PolicyViolation("Memory predecessor identity drift")

            cursor.execute(
                "select id from memory.record where realm_id=%s and state='active'"
                " and scope=%s and project_id is not distinct from %s"
                " and work_item_id is not distinct from %s and content=%s limit 1",
                (self.realm_id, row[2], row[3], row[4], row[8]),
            )
            duplicate = cursor.fetchone()
            if duplicate is not None:
                raise PolicyViolation("Memory promotion duplicate active content")

        return LockedMemoryPromotion(
            candidate=candidate,
            candidate_storage_id=UUID(str(row[0])),
            project_id=row[3],
            work_item_id=row[4],
            predecessor=None if predecessor_row is None else self._record(predecessor_row),
            predecessor_storage_id=predecessor_storage_id,
        )

    def persist(
        self,
        locked: LockedMemoryPromotion,
        plan: MemoryPromotionPlan,
        *,
        authorization_id: UUID,
        now: dt.datetime,
    ) -> MemoryPromotionReceipt:
        candidate = locked.candidate
        record = candidate.promote(
            memory_id=plan.logical_memory_id,
            reviewed_by=plan.review.reviewer_ref,
            now=now,
            revision=plan.next_revision,
        )
        record_id = new_uuid7(now=now)
        review_id = new_uuid7(now=now)
        receipt_id = new_uuid7(now=now)
        predecessor_id = locked.predecessor_storage_id
        project_ref = candidate.key.project_ref
        work_ref = candidate.key.work_ref
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into memory.promotion_plan"
                " (id,realm_id,candidate_id,plan_body,plan_digest,created_at)"
                " values (%s,%s,%s,%s::jsonb,%s,%s)",
                (
                    new_uuid7(now=now),
                    self.realm_id,
                    locked.candidate_storage_id,
                    canonical_json(plan.body()),
                    plan.plan_digest,
                    now,
                ),
            )
            cursor.execute(
                "insert into memory.review"
                " (id,realm_id,candidate_id,reviewer_ref,decision,reason_digest,policy_digest,"
                "review_digest,decided_at,grants_authority)"
                " values (%s,%s,%s,%s,'approved',%s,%s,%s,%s,false)",
                (
                    review_id,
                    self.realm_id,
                    locked.candidate_storage_id,
                    plan.review.reviewer_ref,
                    digest(plan.review.reason),
                    plan.review.policy_digest,
                    plan.review.review_digest,
                    plan.review.decided_at,
                ),
            )
            if predecessor_id is not None:
                cursor.execute(
                    "update memory.record set state='superseded',superseded_by=%s,valid_until=%s"
                    " where realm_id=%s and id=%s and state='active'",
                    (record_id, now, self.realm_id, predecessor_id),
                )
                if cursor.rowcount != 1:
                    raise PolicyViolation("Memory predecessor supersession race")
            cursor.execute(
                "insert into memory.record"
                " (id,realm_id,logical_memory_id,scope,project_id,work_item_id,"
                "project_ref,work_ref,"
                "memory_class,content,state,revision,evidence,entities,valid_from,valid_until,"
                "author_ref,reviewed_by,superseded_by,record_digest,grants_authority,created_at,"
                "predecessor_id) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s::jsonb,"
                "%s,%s,null,%s,%s,null,%s,false,%s,%s)",
                (
                    record_id,
                    self.realm_id,
                    plan.logical_memory_id,
                    str(candidate.key.scope),
                    locked.project_id,
                    locked.work_item_id,
                    project_ref,
                    work_ref,
                    str(candidate.memory_class),
                    candidate.content,
                    plan.next_revision,
                    canonical_json([item.as_dict() for item in candidate.evidence]),
                    list(record.entities),
                    now,
                    candidate.author_ref,
                    plan.review.reviewer_ref,
                    record.record_digest,
                    now,
                    predecessor_id,
                ),
            )
            for ordinal, evidence in enumerate(candidate.evidence, start=1):
                cursor.execute(
                    "insert into memory.evidence_link"
                    " (id,realm_id,record_id,ordinal,evidence_kind,evidence_ref,evidence_digest,"
                    "created_at) values (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        new_uuid7(now=now),
                        self.realm_id,
                        record_id,
                        ordinal,
                        evidence.kind,
                        evidence.reference,
                        evidence.digest_value,
                        now,
                    ),
                )
            cursor.execute(
                "insert into memory.revision"
                " (id,realm_id,record_id,logical_memory_id,revision,predecessor_id,review_id,"
                "record_digest,plan_digest,created_at) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    new_uuid7(now=now),
                    self.realm_id,
                    record_id,
                    plan.logical_memory_id,
                    plan.next_revision,
                    predecessor_id,
                    review_id,
                    record.record_digest,
                    plan.plan_digest,
                    now,
                ),
            )
            if predecessor_id is not None:
                cursor.execute(
                    "insert into memory.relation (id,realm_id,from_id,to_id,kind,created_at)"
                    " values (%s,%s,%s,%s,'supersedes',%s)",
                    (new_uuid7(now=now), self.realm_id, record_id, predecessor_id, now),
                )
            for kind, target_ref in (
                ("embedding", plan.embedding_profile_digest),
                ("external-sync", plan.external_target_ref),
            ):
                cursor.execute(
                    "insert into memory.promotion_outbox"
                    " (id,realm_id,record_id,kind,target_ref,payload_digest,created_at)"
                    " values (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        new_uuid7(now=now),
                        self.realm_id,
                        record_id,
                        kind,
                        target_ref,
                        digest(
                            {
                                "record_digest": record.record_digest,
                                "kind": kind,
                                "target_ref": target_ref,
                            }
                        ),
                        now,
                    ),
                )
            cursor.execute(
                "update memory.candidate set reviewed=true,reviewer_ref=%s,review_reason=%s,"
                "promoted_record_id=%s,promotion_plan_digest=%s"
                " where realm_id=%s and id=%s and promoted_record_id is null",
                (
                    plan.review.reviewer_ref,
                    plan.review.reason,
                    record_id,
                    plan.plan_digest,
                    self.realm_id,
                    locked.candidate_storage_id,
                ),
            )
            if cursor.rowcount != 1:
                raise PolicyViolation("Memory candidate promotion race")

        result_digest = digest(
            {
                "plan_digest": plan.plan_digest,
                "record_id": str(record_id),
                "record_digest": record.record_digest,
                "review_id": str(review_id),
                "revision": plan.next_revision,
            }
        )
        return MemoryPromotionReceipt(
            id=receipt_id,
            plan_digest=plan.plan_digest,
            record_storage_id=record_id,
            logical_memory_id=plan.logical_memory_id,
            revision=plan.next_revision,
            review_id=review_id,
            authorization_id=authorization_id,
            result_digest=result_digest,
            created_at=now,
        )

    def store_receipt(
        self,
        receipt: MemoryPromotionReceipt,
        *,
        candidate_id: UUID,
        predecessor_id: UUID | None,
        effect_digest: str,
    ) -> None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into memory.promotion_receipt"
                " (id,realm_id,candidate_id,record_id,predecessor_id,review_id,authorization_id,"
                "plan_digest,effect_digest,result_digest,created_at)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    receipt.id,
                    self.realm_id,
                    candidate_id,
                    receipt.record_storage_id,
                    predecessor_id,
                    receipt.review_id,
                    receipt.authorization_id,
                    receipt.plan_digest,
                    effect_digest,
                    receipt.result_digest,
                    receipt.created_at,
                ),
            )

    def _candidate(self, row: Any) -> MemoryCandidate:
        key = MemoryKey(
            scope=MemoryScope(str(row[2])),
            realm_ref=self.realm_ref,
            project_ref=None if row[5] is None else str(row[5]),
            work_ref=None if row[6] is None else str(row[6]),
        )
        return MemoryCandidate(
            candidate_id=str(row[1]),
            key=key,
            memory_class=MemoryClass(str(row[7])),
            content=str(row[8]),
            author_ref=str(row[9]),
            evidence=tuple(
                MemoryEvidence(item["kind"], item["reference"], item["digest"])
                for item in (row[10] or ())
            ),
            occurrence_key=row[11],
            observation_count=int(row[12]),
            observed_at=row[13],
        )

    def _record(self, row: Any) -> MemoryRecord:
        return MemoryRecord(
            memory_id=str(row[1]),
            key=MemoryKey(
                scope=MemoryScope(str(row[2])),
                realm_ref=self.realm_ref,
                project_ref=None if row[3] is None else str(row[3]),
                work_ref=None if row[4] is None else str(row[4]),
            ),
            memory_class=MemoryClass(str(row[5])),
            content=str(row[6]),
            state=MemoryState(str(row[7])),
            revision=int(row[8]),
            evidence=tuple(
                MemoryEvidence(item["kind"], item["reference"], item["digest"])
                for item in (row[9] or ())
            ),
            entities=tuple(row[10] or ()),
            valid_from=row[11],
            valid_until=row[12],
            author_ref=row[13],
            reviewed_by=row[14],
            superseded_by=None if row[15] is None else str(row[15]),
            last_used_at=row[16],
            created_at=row[18],
        )
