"""PostgreSQL repository for revision-bound OpenCode benchmark campaigns."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.errors import ConcurrencyConflict, NotFound, PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.model_campaign import (
    CampaignMember,
    CampaignMemberDisposition,
    CampaignMemberPlan,
    CampaignMemberRecord,
    CampaignMemberResult,
    CampaignMemberResultRecord,
    CampaignMemberResultStage,
    CampaignMemberResultStatus,
    CampaignOutcome,
    CampaignOutcomeStatus,
    CampaignStatus,
    OpenCodeBenchmarkCampaign,
    QualificationEvent,
)


@dataclass(frozen=True, slots=True)
class ModelCampaignRepository:
    connection: Any
    realm_id: UUID

    def ensure_campaign(self, campaign: OpenCodeBenchmarkCampaign) -> tuple[UUID, bool]:
        if campaign.continuation is not None:
            raise PolicyViolation("Continuation campaign explicit API ile kaydedilmeli")
        return self._ensure_campaign(campaign)

    def ensure_continuation_campaign(
        self, campaign: OpenCodeBenchmarkCampaign
    ) -> tuple[UUID, bool]:
        if campaign.continuation is None:
            raise PolicyViolation("Continuation campaign exact parent provenance ister")
        return self._ensure_campaign(campaign)

    def _ensure_campaign(self, campaign: OpenCodeBenchmarkCampaign) -> tuple[UUID, bool]:
        """Insert an exact campaign or return its zero-call same-revision replay."""
        campaign_id = new_uuid7()
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.opencode_benchmark_campaign"
                " (id, realm_id, work_item_id, task_plan_id, campaign_key, revision,"
                "  source_revision, provider_ref, catalog_digest, endpoint_identity_digest,"
                "  inventory_digest, policy_digest, fixture_registry_digest, verifier_identity,"
                "  verifier_provenance_digest, source_digest, repetitions,"
                "  verifier_provider_calls_per_trial, configured_model_count,"
                "  member_count, eligible_model_count, audio_excluded_count, health_call_budget,"
                "  tested_call_budget, provider_call_budget, campaign_digest,"
                "  benchmark_suite_version, parent_campaign_id, parent_source_revision,"
                "  compatibility_evidence_digest, continuation_provenance_digest,"
                "  continuation_tested_call_budget, continuation_provider_call_budget)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                "         %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                "         %s, %s, %s, %s, %s)"
                " on conflict (realm_id, campaign_key, revision) do nothing returning id",
                (
                    campaign_id,
                    self.realm_id,
                    campaign.work_item_id,
                    campaign.task_plan_id,
                    campaign.campaign_key,
                    campaign.revision,
                    campaign.source_revision,
                    campaign.provider_ref,
                    campaign.catalog_digest,
                    campaign.endpoint_identity_digest,
                    campaign.inventory_digest,
                    campaign.policy_digest,
                    campaign.fixture_registry_digest,
                    campaign.verifier_identity,
                    campaign.verifier_provenance_digest,
                    campaign.source_digest,
                    campaign.repetitions,
                    campaign.verifier_provider_calls_per_trial,
                    campaign.configured_model_count,
                    campaign.member_count,
                    campaign.eligible_model_count,
                    campaign.audio_excluded_count,
                    campaign.health_call_budget,
                    campaign.tested_call_budget,
                    campaign.provider_call_budget,
                    campaign.campaign_digest,
                    campaign.benchmark_suite_version,
                    (
                        campaign.continuation.parent_campaign_id
                        if campaign.continuation is not None
                        else None
                    ),
                    (
                        campaign.continuation.parent_source_revision
                        if campaign.continuation is not None
                        else None
                    ),
                    (
                        campaign.continuation.compatibility_evidence_digest
                        if campaign.continuation is not None
                        else None
                    ),
                    (
                        campaign.continuation.continuation_provenance_digest
                        if campaign.continuation is not None
                        else None
                    ),
                    (
                        campaign.continuation.maximum_tested_call_count
                        if campaign.continuation is not None
                        else None
                    ),
                    (
                        campaign.continuation.maximum_provider_call_count
                        if campaign.continuation is not None
                        else None
                    ),
                ),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                cursor.execute(
                    "select id, campaign_digest from models.opencode_benchmark_campaign"
                    " where realm_id = %s and campaign_key = %s and revision = %s",
                    (self.realm_id, campaign.campaign_key, campaign.revision),
                )
                existing = cursor.fetchone()
                if existing is None:  # pragma: no cover - concurrent rollback edge
                    raise ConcurrencyConflict("Campaign revision concurrent olarak degisti")
                if str(existing[1]) != campaign.campaign_digest:
                    raise ConcurrencyConflict("Campaign revision payload drift tespit edildi")
                return UUID(str(existing[0])), False

            for member in campaign.members:
                tested_budget = member.tested_call_budget(campaign.repetitions)
                provider_budget = tested_budget * (1 + campaign.verifier_provider_calls_per_trial)
                cursor.execute(
                    "insert into models.opencode_benchmark_campaign_member"
                    " (id, realm_id, campaign_id, configured_model_id, canonical_model_id,"
                    "  modality, disposition, fixture_digests, exclusion_reason, suite_digest,"
                    "  tested_call_budget, provider_call_budget)"
                    " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        new_uuid7(),
                        self.realm_id,
                        campaign_id,
                        member.configured_model_id,
                        member.canonical_model_id,
                        member.modality,
                        member.disposition.value,
                        sorted(member.fixture_digests),
                        member.exclusion_reason,
                        member.suite_digest,
                        tested_budget,
                        provider_budget,
                    ),
                )
        return campaign_id, True

    def list_members(self, campaign_id: UUID) -> tuple[CampaignMemberRecord, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, configured_model_id, canonical_model_id, modality, disposition,"
                " fixture_digests, exclusion_reason, tested_call_budget, provider_call_budget"
                " from models.opencode_benchmark_campaign_member"
                " where realm_id = %s and campaign_id = %s"
                " order by configured_model_id, canonical_model_id nulls first",
                (self.realm_id, campaign_id),
            )
            rows = cursor.fetchall()
        return tuple(
            CampaignMemberRecord(
                id=UUID(str(row[0])),
                campaign_id=campaign_id,
                member=CampaignMember(
                    configured_model_id=str(row[1]),
                    canonical_model_id=str(row[2]) if row[2] is not None else None,
                    modality=str(row[3]),
                    disposition=CampaignMemberDisposition(str(row[4])),
                    fixture_digests=tuple(str(value) for value in row[5]),
                    exclusion_reason=str(row[6]) if row[6] is not None else None,
                ),
                tested_call_budget=int(row[7]),
                provider_call_budget=int(row[8]),
            )
            for row in rows
        )

    def store_member_plan(
        self,
        *,
        campaign_id: UUID,
        member_id: UUID,
        plan: CampaignMemberPlan,
    ) -> tuple[UUID, bool]:
        record_id = new_uuid7()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.opencode_benchmark_campaign_member_plan"
                " (id, realm_id, campaign_id, member_id, benchmark_plan_id,"
                "  benchmark_plan_digest, health_evidence_digest,"
                "  authorization_manifest_digest, tested_call_budget, provider_call_budget,"
                "  member_plan_digest)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, campaign_id, member_id) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    campaign_id,
                    member_id,
                    plan.benchmark_plan_id,
                    plan.benchmark_plan_digest,
                    plan.health_evidence_digest,
                    plan.authorization_manifest_digest,
                    plan.tested_call_budget,
                    plan.provider_call_budget,
                    plan.member_plan_digest,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                return UUID(str(inserted[0])), True
            cursor.execute(
                "select id, member_plan_digest"
                " from models.opencode_benchmark_campaign_member_plan"
                " where realm_id = %s and campaign_id = %s and member_id = %s",
                (self.realm_id, campaign_id, member_id),
            )
            existing = cursor.fetchone()
        if existing is None:  # pragma: no cover
            raise NotFound("Campaign member plan kaydedilemedi")
        if str(existing[1]) != plan.member_plan_digest:
            raise ConcurrencyConflict("Campaign member plan replay drift tespit edildi")
        return UUID(str(existing[0])), False

    def record_member_result(
        self,
        *,
        campaign_id: UUID,
        member_id: UUID,
        member_plan_id: UUID | None,
        result: CampaignMemberResult,
    ) -> tuple[UUID, bool]:
        if result.adoption is not None or result.recovery_evidence is not None:
            raise PolicyViolation("Continuation result explicit API ile kaydedilmeli")
        return self._record_member_result(
            campaign_id=campaign_id,
            member_id=member_id,
            member_plan_id=member_plan_id,
            result=result,
        )

    def record_adopted_result(
        self,
        *,
        campaign_id: UUID,
        member_id: UUID,
        result: CampaignMemberResult,
    ) -> tuple[UUID, bool]:
        if result.adoption is None or result.recovery_evidence is not None:
            raise PolicyViolation("Adopted result exact parent result provenance ister")
        return self._record_member_result(
            campaign_id=campaign_id,
            member_id=member_id,
            member_plan_id=None,
            result=result,
        )

    def record_recovered_health_failure(
        self,
        *,
        campaign_id: UUID,
        member_id: UUID,
        result: CampaignMemberResult,
    ) -> tuple[UUID, bool]:
        if result.recovery_evidence is None or result.adoption is not None:
            raise PolicyViolation("Recovered health result exact claim/receipt provenance ister")
        return self._record_member_result(
            campaign_id=campaign_id,
            member_id=member_id,
            member_plan_id=None,
            result=result,
        )

    def _record_member_result(
        self,
        *,
        campaign_id: UUID,
        member_id: UUID,
        member_plan_id: UUID | None,
        result: CampaignMemberResult,
    ) -> tuple[UUID, bool]:
        if result.stage is CampaignMemberResultStage.HEALTH and member_plan_id is not None:
            raise PolicyViolation("Health result member plan oncesinde kaydedilmeli")
        if (
            result.stage is CampaignMemberResultStage.BENCHMARK
            and member_plan_id is None
            and result.adoption is None
        ):
            raise PolicyViolation("Benchmark result exact member plan ister")
        record_id = new_uuid7()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select parent_campaign_id from models.opencode_benchmark_campaign"
                " where realm_id = %s and id = %s",
                (self.realm_id, campaign_id),
            )
            campaign_row = cursor.fetchone()
            if campaign_row is None:
                raise NotFound("OpenCode benchmark campaign bulunamadi")
            parent_campaign_id = UUID(str(campaign_row[0])) if campaign_row[0] is not None else None
            cursor.execute(
                "insert into models.opencode_benchmark_campaign_member_result"
                " (id, realm_id, campaign_id, member_id, member_plan_id, stage, status,"
                "  aggregate_id, evidence_digest, result_digest, failure_category,"
                "  actual_tested_call_count, actual_provider_call_count,"
                "  adopted_from_campaign_id, adopted_from_result_id,"
                "  adoption_provenance_digest, recovered_from_claim_id,"
                "  recovered_from_receipt_id, recovery_provenance_digest)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                "         %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, campaign_id, member_id, stage)"
                " do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    campaign_id,
                    member_id,
                    member_plan_id,
                    result.stage.value,
                    result.status.value,
                    result.aggregate_id,
                    result.evidence_digest,
                    result.result_digest,
                    result.failure_category,
                    result.actual_tested_call_count,
                    result.actual_provider_call_count,
                    parent_campaign_id if result.adoption is not None else None,
                    (
                        result.adoption.adopted_from_result_id
                        if result.adoption is not None
                        else None
                    ),
                    (
                        result.adoption.adoption_provenance_digest
                        if result.adoption is not None
                        else None
                    ),
                    (
                        result.recovery_evidence.recovered_from_claim_id
                        if result.recovery_evidence is not None
                        else None
                    ),
                    (
                        result.recovery_evidence.recovered_from_receipt_id
                        if result.recovery_evidence is not None
                        else None
                    ),
                    (
                        result.recovery_evidence.recovery_provenance_digest
                        if result.recovery_evidence is not None
                        else None
                    ),
                ),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                return UUID(str(inserted[0])), True
            cursor.execute(
                "select id, result_digest"
                " from models.opencode_benchmark_campaign_member_result"
                " where realm_id = %s and campaign_id = %s and member_id = %s and stage = %s",
                (self.realm_id, campaign_id, member_id, result.stage.value),
            )
            existing = cursor.fetchone()
        if existing is None:  # pragma: no cover
            raise NotFound("Campaign member result kaydedilemedi")
        if str(existing[1]) != result.result_digest:
            raise ConcurrencyConflict("Campaign member result replay drift tespit edildi")
        return UUID(str(existing[0])), False

    def record_outcome(self, *, campaign_id: UUID, outcome: CampaignOutcome) -> tuple[UUID, bool]:
        record_id = new_uuid7()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.opencode_benchmark_campaign_outcome"
                " (id, realm_id, campaign_id, status, passed_count, failed_count,"
                "  recovery_required_count, audio_excluded_count, actual_tested_call_count,"
                "  actual_provider_call_count, evidence_digest, outcome_digest)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, campaign_id) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    campaign_id,
                    outcome.status.value,
                    outcome.passed_count,
                    outcome.failed_count,
                    outcome.recovery_required_count,
                    outcome.audio_excluded_count,
                    outcome.actual_tested_call_count,
                    outcome.actual_provider_call_count,
                    outcome.evidence_digest,
                    outcome.outcome_digest,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                return UUID(str(inserted[0])), True
            cursor.execute(
                "select id, outcome_digest from models.opencode_benchmark_campaign_outcome"
                " where realm_id = %s and campaign_id = %s",
                (self.realm_id, campaign_id),
            )
            existing = cursor.fetchone()
        if existing is None:  # pragma: no cover
            raise NotFound("Campaign outcome kaydedilemedi")
        if str(existing[1]) != outcome.outcome_digest:
            raise ConcurrencyConflict("Campaign terminal outcome replay drift tespit edildi")
        return UUID(str(existing[0])), False

    def record_qualification(
        self,
        *,
        campaign_id: UUID,
        member_id: UUID,
        event: QualificationEvent,
    ) -> tuple[UUID, bool]:
        record_id = new_uuid7()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.opencode_model_qualification_event"
                " (id, realm_id, campaign_id, member_id, outcome_id, model_id, action,"
                "  aggregate_id, evidence_digest, reason_code, event_digest)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, event_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    campaign_id,
                    member_id,
                    event.outcome_id,
                    event.model_id,
                    event.action.value,
                    event.aggregate_id,
                    event.evidence_digest,
                    event.reason_code,
                    event.event_digest,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                return UUID(str(inserted[0])), True
            cursor.execute(
                "select id, campaign_id, member_id from models.opencode_model_qualification_event"
                " where realm_id = %s and event_digest = %s",
                (self.realm_id, event.event_digest),
            )
            existing = cursor.fetchone()
        if existing is None:  # pragma: no cover
            raise NotFound("Qualification event kaydedilemedi")
        if UUID(str(existing[1])) != campaign_id or UUID(str(existing[2])) != member_id:
            raise ConcurrencyConflict("Qualification event replay scope drift tespit edildi")
        return UUID(str(existing[0])), False

    def adoptable_results(self, parent_campaign_id: UUID) -> tuple[CampaignMemberResultRecord, ...]:
        """Return only direct terminal parent results eligible for zero-call adoption."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select r.id, r.campaign_id, r.member_id, m.configured_model_id,"
                " m.canonical_model_id, m.modality, r.stage, r.status, r.member_plan_id,"
                " mp.benchmark_plan_id, mp.benchmark_plan_digest, r.aggregate_id,"
                " r.evidence_digest, r.result_digest, r.failure_category,"
                " r.actual_tested_call_count, r.actual_provider_call_count"
                " from models.opencode_benchmark_campaign_member_result r"
                " join models.opencode_benchmark_campaign_member m"
                "   on m.realm_id = r.realm_id and m.campaign_id = r.campaign_id"
                "  and m.id = r.member_id"
                " left join models.opencode_benchmark_campaign_member_plan mp"
                "   on mp.realm_id = r.realm_id and mp.campaign_id = r.campaign_id"
                "  and mp.id = r.member_plan_id"
                " where r.realm_id = %s and r.campaign_id = %s"
                "   and r.status in ('passed', 'failed')"
                "   and r.adopted_from_result_id is null"
                "   and r.recovered_from_claim_id is null"
                " order by m.configured_model_id, m.canonical_model_id, r.stage",
                (self.realm_id, parent_campaign_id),
            )
            rows = cursor.fetchall()
        return tuple(self._member_result_record(row) for row in rows)

    def status(self, campaign_id: UUID) -> CampaignStatus:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select c.id, c.campaign_key, c.revision, c.campaign_digest,"
                " o.id, o.status, o.outcome_digest, c.tested_call_budget,"
                " c.provider_call_budget, o.actual_tested_call_count,"
                " o.actual_provider_call_count, c.parent_campaign_id,"
                " c.benchmark_suite_version, c.continuation_provenance_digest,"
                " c.compatibility_evidence_digest,"
                " coalesce(c.continuation_tested_call_budget, c.tested_call_budget),"
                " coalesce(c.continuation_provider_call_budget, c.provider_call_budget)"
                " from models.opencode_benchmark_campaign c"
                " left join models.opencode_benchmark_campaign_outcome o"
                "   on o.realm_id = c.realm_id and o.campaign_id = c.id"
                " where c.realm_id = %s and c.id = %s",
                (self.realm_id, campaign_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("OpenCode benchmark campaign bulunamadi")
        return self._status_from_row(row)

    def latest_terminal(self, campaign_key: str) -> CampaignStatus | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select c.id, c.campaign_key, c.revision, c.campaign_digest,"
                " o.id, o.status, o.outcome_digest, c.tested_call_budget,"
                " c.provider_call_budget, o.actual_tested_call_count,"
                " o.actual_provider_call_count, c.parent_campaign_id,"
                " c.benchmark_suite_version, c.continuation_provenance_digest,"
                " c.compatibility_evidence_digest,"
                " coalesce(c.continuation_tested_call_budget, c.tested_call_budget),"
                " coalesce(c.continuation_provider_call_budget, c.provider_call_budget)"
                " from models.opencode_benchmark_campaign c"
                " join models.opencode_benchmark_campaign_outcome o"
                "   on o.realm_id = c.realm_id and o.campaign_id = c.id"
                " where c.realm_id = %s and c.campaign_key = %s"
                " order by c.revision desc, o.completed_at desc, o.id desc limit 1",
                (self.realm_id, campaign_key),
            )
            row = cursor.fetchone()
        return None if row is None else self._status_from_row(row)

    def continuation_chain(self, campaign_id: UUID) -> tuple[CampaignStatus, ...]:
        """Return current campaign followed by its exact acyclic parent chain."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "with recursive chain as ("
                " select c.id, c.parent_campaign_id, 0 as depth, array[c.id] as path"
                " from models.opencode_benchmark_campaign c"
                " where c.realm_id = %s and c.id = %s"
                " union all"
                " select p.id, p.parent_campaign_id, chain.depth + 1, chain.path || p.id"
                " from chain join models.opencode_benchmark_campaign p"
                "   on p.realm_id = %s and p.id = chain.parent_campaign_id"
                " where not p.id = any(chain.path)"
                ")"
                " select c.id, c.campaign_key, c.revision, c.campaign_digest,"
                " o.id, o.status, o.outcome_digest, c.tested_call_budget,"
                " c.provider_call_budget, o.actual_tested_call_count,"
                " o.actual_provider_call_count, c.parent_campaign_id,"
                " c.benchmark_suite_version, c.continuation_provenance_digest,"
                " c.compatibility_evidence_digest,"
                " coalesce(c.continuation_tested_call_budget, c.tested_call_budget),"
                " coalesce(c.continuation_provider_call_budget, c.provider_call_budget)"
                " from chain join models.opencode_benchmark_campaign c on c.id = chain.id"
                " left join models.opencode_benchmark_campaign_outcome o"
                "   on o.realm_id = c.realm_id and o.campaign_id = c.id"
                " order by chain.depth",
                (self.realm_id, campaign_id, self.realm_id),
            )
            rows = cursor.fetchall()
        if not rows:
            raise NotFound("OpenCode benchmark campaign bulunamadi")
        return tuple(self._status_from_row(row) for row in rows)

    @staticmethod
    def _status_from_row(row: tuple[Any, ...]) -> CampaignStatus:
        return CampaignStatus(
            campaign_id=UUID(str(row[0])),
            campaign_key=str(row[1]),
            revision=int(row[2]),
            campaign_digest=str(row[3]),
            outcome_id=UUID(str(row[4])) if row[4] is not None else None,
            outcome_status=CampaignOutcomeStatus(str(row[5])) if row[5] is not None else None,
            outcome_digest=str(row[6]) if row[6] is not None else None,
            tested_call_budget=int(row[7]),
            provider_call_budget=int(row[8]),
            actual_tested_call_count=int(row[9]) if row[9] is not None else None,
            actual_provider_call_count=int(row[10]) if row[10] is not None else None,
            parent_campaign_id=UUID(str(row[11])) if row[11] is not None else None,
            benchmark_suite_version=int(row[12]),
            continuation_provenance_digest=str(row[13]) if row[13] is not None else None,
            compatibility_evidence_digest=str(row[14]) if row[14] is not None else None,
            current_tested_call_budget=int(row[15]),
            current_provider_call_budget=int(row[16]),
        )

    @staticmethod
    def _member_result_record(row: tuple[Any, ...]) -> CampaignMemberResultRecord:
        return CampaignMemberResultRecord(
            id=UUID(str(row[0])),
            campaign_id=UUID(str(row[1])),
            member_id=UUID(str(row[2])),
            configured_model_id=str(row[3]),
            canonical_model_id=str(row[4]),
            modality=str(row[5]),
            stage=CampaignMemberResultStage(str(row[6])),
            status=CampaignMemberResultStatus(str(row[7])),
            member_plan_id=UUID(str(row[8])) if row[8] is not None else None,
            benchmark_plan_id=UUID(str(row[9])) if row[9] is not None else None,
            benchmark_plan_digest=str(row[10]) if row[10] is not None else None,
            aggregate_id=UUID(str(row[11])) if row[11] is not None else None,
            evidence_digest=str(row[12]),
            result_digest=str(row[13]),
            failure_category=str(row[14]) if row[14] is not None else None,
            actual_tested_call_count=int(row[15]),
            actual_provider_call_count=int(row[16]),
        )
