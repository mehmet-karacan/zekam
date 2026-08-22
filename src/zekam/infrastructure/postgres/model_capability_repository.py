"""Append-only PostgreSQL persistence for capability benchmark scorecards."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest
from zekam.domain.errors import ConcurrencyConflict, NotFound, PolicyViolation
from zekam.domain.identifiers import new_uuid7
from zekam.domain.model_capability_benchmark import (
    CapabilityCohortPlan,
    CapabilityEpisodeResult,
    CapabilityModelResult,
)


@dataclass(frozen=True, slots=True)
class CapabilitySource:
    campaign_id: UUID
    source_revision: str
    inventory_digest: str
    policy_digest: str
    verifier_provenance_digest: str
    model_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelCapabilityRepository:
    connection: Any
    realm_id: UUID

    def latest_source(self) -> CapabilitySource:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select c.id, c.source_revision, c.inventory_digest, c.policy_digest,"
                " c.verifier_provenance_digest,"
                " array_agg(q.model_id order by q.model_id)"
                " from models.opencode_benchmark_campaign c"
                " join models.opencode_benchmark_campaign_outcome o"
                "   on o.realm_id=c.realm_id and o.campaign_id=c.id"
                " join models.opencode_model_qualification_event q"
                "   on q.realm_id=c.realm_id and q.campaign_id=c.id and q.action='qualified'"
                " where c.realm_id=%s and o.status in ('passed','failed')"
                " group by c.id, c.source_revision, c.inventory_digest, c.policy_digest,"
                "          c.verifier_provenance_digest, c.revision, o.completed_at"
                " order by c.revision desc, o.completed_at desc, c.id desc limit 1",
                (self.realm_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Capability benchmark icin qualified kampanya bulunamadi")
        return CapabilitySource(
            campaign_id=row[0],
            source_revision=str(row[1]),
            inventory_digest=str(row[2]),
            policy_digest=str(row[3]),
            verifier_provenance_digest=str(row[4]),
            model_ids=tuple(row[5]),
        )

    def model_labels(self, campaign_id: UUID, model_ids: tuple[str, ...]) -> dict[str, str]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select canonical_model_id,configured_model_id"
                " from models.opencode_benchmark_campaign_member"
                " where realm_id=%s and campaign_id=%s and canonical_model_id=any(%s)"
                " order by canonical_model_id",
                (self.realm_id, campaign_id, list(model_ids)),
            )
            rows = cursor.fetchall()
        labels = {str(row[0]): str(row[1]) for row in rows}
        if set(labels) != set(model_ids):
            raise PolicyViolation("Capability model label campaign binding eksik")
        return labels

    def ensure_plan(self, plan: CapabilityCohortPlan) -> tuple[UUID, UUID, bool]:
        suite_digest = digest(
            {
                "registry_digest": plan.registry.registry_digest,
                "execution_profile_digest": plan.execution_profile.profile_digest,
                "task_digests": sorted(task.task_digest for task in plan.registry.tasks),
                "max_parallelism": plan.max_parallelism,
            }
        )
        suite_id = new_uuid7()
        cohort_id = new_uuid7()
        task_digests = sorted(task.task_digest for task in plan.registry.tasks)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.capability_benchmark_suite"
                " (id,realm_id,registry_digest,execution_profile_digest,"
                "  evaluator_provenance_digest,task_digests,task_roles,task_budgets,"
                "  task_count,max_duration_seconds,max_model_turns,max_input_tokens,"
                "  max_output_tokens,max_tool_calls,max_parallelism,suite_digest)"
                " values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (realm_id,suite_digest) do nothing returning id",
                (
                    suite_id,
                    self.realm_id,
                    plan.registry.registry_digest,
                    plan.execution_profile.profile_digest,
                    plan.execution_profile.evaluator_provenance_digest,
                    task_digests,
                    json.dumps(
                        {task.task_digest: task.role.value for task in plan.registry.tasks},
                        sort_keys=True,
                    ),
                    json.dumps(
                        {
                            task.task_digest: {
                                "duration_seconds": task.max_duration_seconds,
                                "output_tokens": task.max_output_tokens,
                                "tool_calls": task.max_tool_calls,
                            }
                            for task in plan.registry.tasks
                        },
                        sort_keys=True,
                    ),
                    len(task_digests),
                    max(task.max_duration_seconds for task in plan.registry.tasks),
                    plan.execution_profile.max_model_turns,
                    plan.execution_profile.max_input_tokens_total,
                    plan.execution_profile.max_output_tokens_total,
                    plan.execution_profile.max_tool_calls,
                    plan.max_parallelism,
                    suite_digest,
                ),
            )
            inserted_suite = cursor.fetchone()
            if inserted_suite is None:
                cursor.execute(
                    "select id from models.capability_benchmark_suite"
                    " where realm_id=%s and suite_digest=%s",
                    (self.realm_id, suite_digest),
                )
                suite_id = cursor.fetchone()[0]
            else:
                suite_id = inserted_suite[0]
            cursor.execute(
                "insert into models.capability_benchmark_cohort"
                " (id,realm_id,suite_id,source_campaign_id,source_revision,inventory_digest,"
                "  policy_digest,verifier_provenance_digest,model_ids,provider_call_budget,"
                "  start_skew_budget_ms,plan_digest)"
                " values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " on conflict (realm_id,plan_digest) do nothing returning id",
                (
                    cohort_id,
                    self.realm_id,
                    suite_id,
                    plan.source_campaign_id,
                    plan.source_revision,
                    plan.inventory_digest,
                    plan.policy_digest,
                    plan.verifier_provenance_digest,
                    list(plan.model_ids),
                    plan.provider_call_budget,
                    plan.start_skew_budget_ms,
                    plan.plan_digest,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                return suite_id, inserted[0], True
            cursor.execute(
                "select id,suite_id from models.capability_benchmark_cohort"
                " where realm_id=%s and plan_digest=%s",
                (self.realm_id, plan.plan_digest),
            )
            existing = cursor.fetchone()
            if existing is None or existing[1] != suite_id:
                raise ConcurrencyConflict("Capability cohort replay drift")
            return suite_id, existing[0], False

    def record_episode(self, cohort_id: UUID, result: CapabilityEpisodeResult) -> UUID:
        episode_id = new_uuid7()
        values = (
            episode_id,
            self.realm_id,
            cohort_id,
            result.model_id,
            result.task_digest,
            result.role.value,
            result.status.value,
            result.started_at,
            result.duration_ms,
            result.start_skew_ms,
            result.model_turn_count,
            result.input_token_count,
            result.output_token_count,
            result.correctness,
            result.completion,
            result.sustained_progress,
            result.context_retention,
            result.self_correction,
            result.tool_efficiency,
            result.safety,
            result.hidden_acceptance_ratio,
            result.sustained_progress_auc,
            result.longest_stagnation_ms,
            result.regression_count,
            result.noop_ratio,
            result.checkpoint_count,
            result.self_correction_count,
            result.tool_call_count,
            list(result.checkpoint_receipt_digests),
            list(result.tool_receipt_digests),
            result.response_digest,
            result.verifier_model_id,
            result.verifier_execution_identity,
            result.verifier_provenance_digest,
            result.evidence_digest,
            result.acceptance_evidence_digest,
        )
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.capability_benchmark_episode"
                " (id,realm_id,cohort_id,model_id,task_digest,role,status,started_at,"
                " duration_ms,start_skew_ms,model_turn_count,input_token_count,output_token_count,"
                " correctness,completion,sustained_progress,"
                " context_retention,self_correction,tool_efficiency,safety,"
                " hidden_acceptance_ratio,sustained_progress_auc,longest_stagnation_ms,"
                " regression_count,noop_ratio,checkpoint_count,self_correction_count,"
                " tool_call_count,checkpoint_receipt_digests,tool_receipt_digests,"
                " response_digest,verifier_model_id,verifier_execution_identity,"
                " verifier_provenance_digest,evidence_digest,acceptance_evidence_digest)"
                " values (" + ",".join(["%s"] * 36) + ")"
                " on conflict (realm_id,cohort_id,model_id,task_digest) do nothing returning id",
                values,
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                return UUID(str(inserted[0]))
            cursor.execute(
                "select id,evidence_digest from models.capability_benchmark_episode"
                " where realm_id=%s and cohort_id=%s and model_id=%s and task_digest=%s",
                (self.realm_id, cohort_id, result.model_id, result.task_digest),
            )
            existing = cursor.fetchone()
            if existing is None or existing[1] != result.evidence_digest:
                raise ConcurrencyConflict("Capability episode replay drift")
            return UUID(str(existing[0]))

    def record_scorecard(self, cohort_id: UUID, result: CapabilityModelResult) -> UUID:
        scorecard_id = new_uuid7()
        role_scores = {role.value: score for role, score in result.role_scores}
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.capability_benchmark_scorecard"
                " (id,realm_id,cohort_id,model_id,episode_evidence_digests,general_score,"
                "  role_scores,completion_rate,mean_duration_ms,evidence_digest)"
                " values (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)"
                " on conflict (realm_id,cohort_id,model_id) do nothing returning id",
                (
                    scorecard_id,
                    self.realm_id,
                    cohort_id,
                    result.model_id,
                    list(result.episode_evidence_digests),
                    result.general_score,
                    json.dumps(role_scores, sort_keys=True),
                    result.completion_rate,
                    result.mean_duration_ms,
                    result.evidence_digest,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                return UUID(str(inserted[0]))
            cursor.execute(
                "select id,evidence_digest from models.capability_benchmark_scorecard"
                " where realm_id=%s and cohort_id=%s and model_id=%s",
                (self.realm_id, cohort_id, result.model_id),
            )
            existing = cursor.fetchone()
            if existing is None or existing[1] != result.evidence_digest:
                raise ConcurrencyConflict("Capability scorecard replay drift")
            return UUID(str(existing[0]))

    def scorecards(self, cohort_id: UUID) -> tuple[dict[str, Any], ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select model_id,general_score,role_scores,completion_rate,mean_duration_ms,"
                " evidence_digest from models.capability_benchmark_scorecard"
                " where realm_id=%s and cohort_id=%s order by general_score desc,model_id",
                (self.realm_id, cohort_id),
            )
            rows = cursor.fetchall()
        return tuple(
            {
                "model_id": row[0],
                "general_score": row[1],
                "role_scores": row[2],
                "completion_rate": row[3],
                "mean_duration_ms": row[4],
                "evidence_digest": row[5],
            }
            for row in rows
        )

    def require_current_source(self, plan: CapabilityCohortPlan) -> None:
        source = self.latest_source()
        if (
            source.campaign_id != plan.source_campaign_id
            or source.source_revision != plan.source_revision
            or source.inventory_digest != plan.inventory_digest
            or source.policy_digest != plan.policy_digest
            or source.verifier_provenance_digest != plan.verifier_provenance_digest
            or source.model_ids != tuple(sorted(plan.model_ids))
        ):
            raise PolicyViolation("Capability source campaign current binding drift")
