"""PostgreSQL repository for the capability calibration approval/runtime ledger."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.errors import ConcurrencyConflict, NotFound, PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.model_capability_runtime import (
    MAX_PROVIDER_CALLS,
    CapabilityRuntimeApprovalManifest,
    CapabilityRuntimeCallOutcome,
    CapabilityRuntimeCallStatus,
    CapabilityRuntimeContinuityState,
    CapabilityRuntimeDerivation,
    CapabilityRuntimeDerivedAuthorization,
    CapabilityRuntimeOutcome,
    CapabilityRuntimeSlot,
    CapabilityRuntimeTurnCheckpoint,
)


@dataclass(frozen=True, slots=True)
class StoredCapabilityRuntimeSlot:
    slot_id: UUID
    slot: CapabilityRuntimeSlot
    derived_authorization: CapabilityRuntimeDerivedAuthorization | None
    terminal_status: CapabilityRuntimeCallStatus | None


@dataclass(frozen=True, slots=True)
class ModelCapabilityRuntimeRepository:
    connection: Any
    realm_id: UUID

    def ensure_manifest(
        self,
        manifest: CapabilityRuntimeApprovalManifest,
        slots: tuple[CapabilityRuntimeSlot, ...],
    ) -> tuple[UUID, bool]:
        """Persist the immutable manifest and all 168 pre-authorized slots atomically."""
        self._validate_slot_set(manifest, slots)
        manifest_id = new_uuid7()
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.capability_runtime_approval_manifest"
                " (id,realm_id,cohort_id,work_item_id,task_plan_id,coordinator_job_id,"
                " source_revision,"
                " model_ids,task_digests,episode_count,slots_per_episode,max_provider_calls,"
                " max_retries,approval_evidence_digest,manifest_digest)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (realm_id,cohort_id) do nothing returning id",
                (
                    manifest_id,
                    self.realm_id,
                    manifest.cohort_id,
                    manifest.work_item_id,
                    manifest.task_plan_id,
                    manifest.coordinator_job_id,
                    manifest.source_revision,
                    list(manifest.model_ids),
                    list(manifest.task_digests),
                    manifest.episode_count,
                    manifest.slots_per_episode,
                    manifest.max_provider_calls,
                    manifest.max_retries,
                    manifest.approval_evidence_digest,
                    manifest.manifest_digest,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                cursor.execute(
                    "select id,manifest_digest from models.capability_runtime_approval_manifest"
                    " where realm_id=%s and cohort_id=%s",
                    (self.realm_id, manifest.cohort_id),
                )
                existing = cursor.fetchone()
                if existing is None or str(existing[1]) != manifest.manifest_digest:
                    raise ConcurrencyConflict("Capability runtime manifest replay drift")
                manifest_id = UUID(str(existing[0]))
                self._require_existing_slots(cursor, manifest_id, slots)
                return manifest_id, False
            manifest_id = UUID(str(inserted[0]))
            for slot in slots:
                cursor.execute(
                    "insert into models.capability_runtime_approval_slot"
                    " (id,realm_id,manifest_id,cohort_id,model_id,task_digest,turn_number,"
                    " ordinal,job_id,provider_ref,backend_model,endpoint_resource,call_resource,"
                    " endpoint_identity_digest,operation,call_id,fixture_digest,"
                    " fixture_identity_digest,max_output_tokens,request_template,"
                    " request_template_digest,derivation_rule_digest,"
                    " chain_seed_digest,slot_digest)"
                    " values (" + ",".join(["%s"] * 24) + ")",
                    (
                        new_uuid7(),
                        self.realm_id,
                        manifest_id,
                        manifest.cohort_id,
                        slot.model_id,
                        slot.task_digest,
                        slot.turn_number,
                        slot.ordinal,
                        slot.job_id,
                        slot.provider_ref,
                        slot.backend_model,
                        slot.endpoint_resource,
                        slot.call_resource,
                        slot.endpoint_identity_digest,
                        slot.operation,
                        slot.call_id,
                        slot.fixture_digest,
                        slot.fixture_identity_digest,
                        slot.max_output_tokens,
                        json.dumps(slot.request_template, sort_keys=True),
                        slot.request_template_digest,
                        slot.derivation_rule_digest,
                        slot.chain_seed_digest,
                        slot.slot_digest,
                    ),
                )
        return manifest_id, True

    def bind_slot_authorization(
        self,
        manifest_id: UUID,
        slot_id: UUID,
        authorization: CapabilityRuntimeDerivedAuthorization,
    ) -> UUID:
        """Bind one derived one-shot authorization before claim/effect."""
        binding_id = new_uuid7()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.capability_runtime_slot_authorization"
                " (id,realm_id,manifest_id,slot_id,authorization_id,authorization_plan_digest,"
                " authorization_digest,request_body_digest,effect_digest,"
                " prior_response_chain_digest,binding_digest)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (slot_id) do nothing returning id",
                (
                    binding_id,
                    self.realm_id,
                    manifest_id,
                    slot_id,
                    authorization.authorization_id,
                    authorization.authorization_plan_digest,
                    authorization.authorization_digest,
                    authorization.request_body_digest,
                    authorization.effect_digest,
                    authorization.prior_response_chain_digest,
                    authorization.binding_digest,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                return UUID(str(inserted[0]))
            cursor.execute(
                "select id,binding_digest from models.capability_runtime_slot_authorization"
                " where realm_id=%s and manifest_id=%s and slot_id=%s",
                (self.realm_id, manifest_id, slot_id),
            )
            existing = cursor.fetchone()
        if existing is None or str(existing[1]) != authorization.binding_digest:
            raise ConcurrencyConflict("Capability runtime slot authorization replay drift")
        return UUID(str(existing[0]))

    def persist_continuity_state(
        self,
        manifest_id: UUID,
        slot_id: UUID,
        state: CapabilityRuntimeContinuityState,
    ) -> UUID:
        state_id = new_uuid7()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.capability_runtime_continuity_state"
                " (id,realm_id,manifest_id,slot_id,continuity_state,continuity_state_digest,"
                " prior_result_digest,derivation_attestation_digest,checkpoint_id,event_digest)"
                " values (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s) returning id",
                (
                    state_id,
                    self.realm_id,
                    manifest_id,
                    slot_id,
                    json.dumps(state.continuity_state, sort_keys=True),
                    state.continuity_state_digest,
                    state.prior_result_digest,
                    state.derivation_attestation_digest,
                    state.checkpoint_id,
                    state.event_digest,
                ),
            )
            return UUID(str(cursor.fetchone()[0]))

    def derive_slot_authorization(self, slot_id: UUID) -> CapabilityRuntimeDerivation:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select request_body,request_body_digest,authorization_plan_digest,effect_digest,"
                " effect_action,claim_operation"
                " from models.capability_runtime_derived_digests(%s,%s)",
                (self.realm_id, slot_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Capability runtime derivation material bulunamadi")
        return CapabilityRuntimeDerivation(
            request_body=dict(row[0]),
            request_body_digest=str(row[1]),
            authorization_plan_digest=str(row[2]),
            effect_digest=str(row[3]),
            effect_action=str(row[4]),
            claim_operation=str(row[5]),
        )

    def persist_turn_checkpoint(
        self,
        manifest_id: UUID,
        slot_id: UUID,
        job_id: UUID,
        checkpoint: CapabilityRuntimeTurnCheckpoint,
    ) -> UUID:
        checkpoint_id = new_uuid7()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.capability_runtime_turn_checkpoint"
                " (id,realm_id,manifest_id,slot_id,continuity_state_id,job_id,"
                " completed_turns,pending_turns,result_digest,checkpoint_digest)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id",
                (
                    checkpoint_id,
                    self.realm_id,
                    manifest_id,
                    slot_id,
                    checkpoint.continuity_state_id,
                    job_id,
                    list(checkpoint.completed_turns),
                    list(checkpoint.pending_turns),
                    checkpoint.result_digest,
                    checkpoint.checkpoint_digest,
                ),
            )
            return UUID(str(cursor.fetchone()[0]))

    def record_call_outcome(self, slot_id: UUID, outcome: CapabilityRuntimeCallOutcome) -> UUID:
        outcome_id = new_uuid7()
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.capability_runtime_call_outcome"
                " (id,realm_id,slot_id,claim_id,receipt_id,checkpoint_id,status,"
                " result_digest,failure_category,evidence_digest,completed_at)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (slot_id) do nothing returning id",
                (
                    outcome_id,
                    self.realm_id,
                    slot_id,
                    outcome.claim_id,
                    outcome.receipt_id,
                    outcome.checkpoint_id,
                    outcome.status.value,
                    outcome.result_digest,
                    outcome.failure_category,
                    outcome.evidence_digest,
                    outcome.completed_at,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                return UUID(str(inserted[0]))
            cursor.execute(
                "select id,evidence_digest from models.capability_runtime_call_outcome"
                " where realm_id=%s and slot_id=%s",
                (self.realm_id, slot_id),
            )
            existing = cursor.fetchone()
            if existing is None or str(existing[1]) != outcome.evidence_digest:
                raise ConcurrencyConflict("Capability runtime call outcome replay drift")
            return UUID(str(existing[0]))

    def finalize_outcome(self, manifest_id: UUID, outcome: CapabilityRuntimeOutcome) -> UUID:
        outcome_id = new_uuid7()
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.capability_runtime_outcome"
                " (id,realm_id,manifest_id,status,actual_provider_calls,actual_retries,"
                " call_evidence_digests,evidence_digest,completed_at)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (realm_id,manifest_id) do nothing returning id",
                (
                    outcome_id,
                    self.realm_id,
                    manifest_id,
                    outcome.status.value,
                    outcome.actual_provider_calls,
                    outcome.actual_retries,
                    list(outcome.call_evidence_digests),
                    outcome.evidence_digest,
                    outcome.completed_at,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                return UUID(str(inserted[0]))
            cursor.execute(
                "select id,evidence_digest from models.capability_runtime_outcome"
                " where realm_id=%s and manifest_id=%s",
                (self.realm_id, manifest_id),
            )
            existing = cursor.fetchone()
            if existing is None or str(existing[1]) != outcome.evidence_digest:
                raise ConcurrencyConflict("Capability runtime aggregate replay drift")
            return UUID(str(existing[0]))

    def outcome(self, manifest_id: UUID) -> dict[str, Any]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select status,actual_provider_calls,actual_retries,score_eligible,"
                " routing_eligible,evidence_digest,completed_at"
                " from models.capability_runtime_outcome"
                " where realm_id=%s and manifest_id=%s",
                (self.realm_id, manifest_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Capability runtime outcome bulunamadi")
        return {
            "status": str(row[0]),
            "actual_provider_calls": int(row[1]),
            "actual_retries": int(row[2]),
            "score_eligible": bool(row[3]),
            "routing_eligible": bool(row[4]),
            "evidence_digest": str(row[5]),
            "completed_at": row[6],
        }

    def manifest_for_cohort(
        self, cohort_id: UUID
    ) -> tuple[UUID, CapabilityRuntimeApprovalManifest]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id,work_item_id,task_plan_id,coordinator_job_id,source_revision,"
                " model_ids,task_digests,"
                " approval_evidence_digest from models.capability_runtime_approval_manifest"
                " where realm_id=%s and cohort_id=%s",
                (self.realm_id, cohort_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Capability runtime manifest bulunamadi")
        return UUID(str(row[0])), CapabilityRuntimeApprovalManifest(
            cohort_id=cohort_id,
            work_item_id=UUID(str(row[1])),
            task_plan_id=UUID(str(row[2])),
            coordinator_job_id=UUID(str(row[3])),
            source_revision=str(row[4]),
            model_ids=tuple(str(value) for value in row[5]),
            task_digests=tuple(str(value) for value in row[6]),
            approval_evidence_digest=str(row[7]),
        )

    def manifest(self, manifest_id: UUID) -> CapabilityRuntimeApprovalManifest:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select cohort_id,work_item_id,task_plan_id,coordinator_job_id,"
                " source_revision,model_ids,"
                " task_digests,approval_evidence_digest"
                " from models.capability_runtime_approval_manifest"
                " where realm_id=%s and id=%s",
                (self.realm_id, manifest_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Capability runtime manifest bulunamadi")
        return CapabilityRuntimeApprovalManifest(
            cohort_id=UUID(str(row[0])),
            work_item_id=UUID(str(row[1])),
            task_plan_id=UUID(str(row[2])),
            coordinator_job_id=UUID(str(row[3])),
            source_revision=str(row[4]),
            model_ids=tuple(str(value) for value in row[5]),
            task_digests=tuple(str(value) for value in row[6]),
            approval_evidence_digest=str(row[7]),
        )

    def slots(self, manifest_id: UUID) -> tuple[StoredCapabilityRuntimeSlot, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select s.id,s.model_id,s.task_digest,s.turn_number,s.ordinal,s.job_id,"
                " s.provider_ref,s.backend_model,s.endpoint_resource,s.call_resource,"
                " s.endpoint_identity_digest,s.operation,s.call_id,s.fixture_digest,"
                " s.fixture_identity_digest,s.max_output_tokens,s.request_template,"
                " s.request_template_digest,s.derivation_rule_digest,s.chain_seed_digest,"
                " a.authorization_id,a.authorization_plan_digest,a.authorization_digest,"
                " a.request_body_digest,a.effect_digest,a.prior_response_chain_digest,o.status"
                " from models.capability_runtime_approval_slot s"
                " left join models.capability_runtime_slot_authorization a"
                "   on a.realm_id=s.realm_id and a.slot_id=s.id"
                " left join models.capability_runtime_call_outcome o"
                "   on o.realm_id=s.realm_id and o.slot_id=s.id"
                " where s.realm_id=%s and s.manifest_id=%s order by s.ordinal",
                (self.realm_id, manifest_id),
            )
            rows = cursor.fetchall()
        return tuple(
            StoredCapabilityRuntimeSlot(
                slot_id=UUID(str(row[0])),
                slot=CapabilityRuntimeSlot(
                    model_id=str(row[1]),
                    task_digest=str(row[2]),
                    turn_number=int(row[3]),
                    ordinal=int(row[4]),
                    job_id=UUID(str(row[5])),
                    provider_ref=str(row[6]),
                    backend_model=str(row[7]),
                    endpoint_resource=str(row[8]),
                    call_resource=str(row[9]),
                    endpoint_identity_digest=str(row[10]),
                    operation=str(row[11]),
                    call_id=str(row[12]),
                    fixture_digest=str(row[13]),
                    fixture_identity_digest=str(row[14]),
                    max_output_tokens=int(row[15]),
                    request_template=dict(row[16]),
                    request_template_digest=str(row[17]),
                    derivation_rule_digest=str(row[18]),
                    chain_seed_digest=str(row[19]),
                ),
                derived_authorization=(
                    CapabilityRuntimeDerivedAuthorization(
                        authorization_id=UUID(str(row[20])),
                        authorization_plan_digest=str(row[21]),
                        authorization_digest=str(row[22]),
                        request_body_digest=str(row[23]),
                        effect_digest=str(row[24]),
                        prior_response_chain_digest=str(row[25]),
                    )
                    if row[20] is not None
                    else None
                ),
                terminal_status=(
                    CapabilityRuntimeCallStatus(str(row[26])) if row[26] is not None else None
                ),
            )
            for row in rows
        )

    @staticmethod
    def _validate_slot_set(
        manifest: CapabilityRuntimeApprovalManifest,
        slots: tuple[CapabilityRuntimeSlot, ...],
    ) -> None:
        expected = {
            (model_id, task_digest, turn)
            for model_id in manifest.model_ids
            for task_digest in manifest.task_digests
            for turn in range(1, manifest.slots_per_episode + 1)
        }
        actual = {(slot.model_id, slot.task_digest, slot.turn_number) for slot in slots}
        ordinals = {slot.ordinal for slot in slots}
        if (
            len(slots) != MAX_PROVIDER_CALLS
            or actual != expected
            or ordinals != set(range(1, MAX_PROVIDER_CALLS + 1))
            or len({slot.job_id for slot in slots}) != manifest.episode_count
        ):
            raise PolicyViolation("Capability runtime exact 168 pre-authorized slot seti ister")
        for model_id in manifest.model_ids:
            for task_digest in manifest.task_digests:
                episode_jobs = {
                    slot.job_id
                    for slot in slots
                    if slot.model_id == model_id and slot.task_digest == task_digest
                }
                if len(episode_jobs) != 1:
                    raise PolicyViolation("Capability runtime her episode icin tek job ister")

    def _require_existing_slots(
        self,
        cursor: Any,
        manifest_id: UUID,
        slots: tuple[CapabilityRuntimeSlot, ...],
    ) -> None:
        cursor.execute(
            "select slot_digest from models.capability_runtime_approval_slot"
            " where realm_id=%s and manifest_id=%s order by slot_digest",
            (self.realm_id, manifest_id),
        )
        existing = tuple(str(row[0]) for row in cursor.fetchall())
        requested = tuple(sorted(slot.slot_digest for slot in slots))
        if existing != requested:
            raise ConcurrencyConflict("Capability runtime manifest slot replay drift")
