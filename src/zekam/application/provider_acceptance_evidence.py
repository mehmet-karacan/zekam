"""Canonical, sanitized OpenCode benchmark acceptance evidence.

The evidence is a projection of append-only PostgreSQL state.  It never stores
endpoint, credential, prompt, response, or source content values.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg.rows import dict_row

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.model_benchmark import (
    TrialResult,
    TrialStatus,
    VerifierIdentity,
    VerifierVerdict,
    aggregate_trials,
    benchmark_effect_digest,
    benchmark_verifier_effect_digest,
)
from zekam.domain.model_campaign import (
    CampaignContinuation,
    CampaignMemberResult,
    CampaignMemberResultStage,
    CampaignMemberResultStatus,
    CampaignOutcome,
    CampaignOutcomeStatus,
    QualificationAction,
    QualificationEvent,
    ResultAdoption,
    ResultRecoveryEvidence,
)

SCHEMA = "zekam-opencode-benchmark-campaign-acceptance/v3"


def _one(rows: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    if len(rows) != 1:
        raise PolicyViolation(reason)
    return rows[0]


def _required(row: dict[str, Any] | None, reason: str) -> dict[str, Any]:
    if row is None:
        raise PolicyViolation(reason)
    return row


def _validated_executed_call_evidence(
    *,
    expected_calls: dict[str, Any],
    executed_evidence: dict[str, str],
    health_status_by_model: dict[str, str],
) -> dict[str, str]:
    """Reconstruct outcome call evidence, including deterministic health-gated skips."""

    outcome_evidence = dict(executed_evidence)
    for call_id, planned in expected_calls.items():
        if call_id in outcome_evidence:
            continue
        if (
            planned.kind.value == "benchmark"
            and health_status_by_model.get(planned.canonical_model_id) == "failed"
        ):
            outcome_evidence[call_id] = digest(
                {
                    "status": "not-run-health-failed",
                    "model_id": planned.canonical_model_id,
                    "call_id": call_id,
                }
            )
            continue
        raise PolicyViolation("Campaign expected executed call evidence missing")
    return outcome_evidence


def build_provider_acceptance_evidence(
    connection: Connection[Any],
    *,
    realm_id: UUID,
    campaign_id: UUID,
    expected_source_revision: str,
    expected_bindings: dict[str, str],
    expected_campaign: Any,
    expected_parent_campaign: Any | None,
    expected_calls: dict[str, Any],
    expected_current_calls: dict[str, Any],
    expected_continuation: CampaignContinuation | None,
    expected_secret_name: str,
    expected_secret_locator: str,
) -> dict[str, Any]:
    """Recompute terminal full-campaign evidence from canonical state.

    Revision three supports both a standalone terminal campaign and the existing
    recovery-continuation chain.  A standalone campaign never acquires adoption
    or recovery provenance merely to make it exportable.
    """

    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "select c.id campaign_id, c.work_item_id, c.task_plan_id, c.campaign_key,"
            " c.revision, c.source_revision, c.source_digest, c.catalog_digest,"
            " c.endpoint_identity_digest, c.inventory_digest, c.policy_digest,"
            " c.fixture_registry_digest, c.verifier_identity,"
            " c.verifier_provenance_digest, c.campaign_digest,"
            " c.configured_model_count, c.member_count, c.eligible_model_count,"
            " c.audio_excluded_count, c.health_call_budget, c.tested_call_budget,"
            " c.provider_call_budget, c.parent_campaign_id, c.parent_source_revision,"
            " c.compatibility_evidence_digest, c.continuation_provenance_digest,"
            " c.continuation_tested_call_budget, c.continuation_provider_call_budget,"
            " o.id outcome_id, o.status outcome_status, o.passed_count, o.failed_count,"
            " o.recovery_required_count, o.actual_tested_call_count,"
            " o.actual_provider_call_count, o.evidence_digest outcome_evidence_digest,"
            " o.outcome_digest, o.completed_at"
            " from models.opencode_benchmark_campaign c"
            " join models.opencode_benchmark_campaign_outcome o"
            "   on o.realm_id=c.realm_id and o.campaign_id=c.id"
            " where c.realm_id=%s and c.id=%s and c.campaign_key='opencode-aihub'",
            (realm_id, campaign_id),
        )
        current = _one(cursor.fetchall(), "Canonical terminal campaign bulunamadi")
        parent_id = current["parent_campaign_id"]
        is_continuation = parent_id is not None
        if current["outcome_status"] not in {"passed", "failed"}:
            raise PolicyViolation("Acceptance terminal passed/failed campaign ister")
        parent: dict[str, Any] | None = None
        if is_continuation:
            if expected_parent_campaign is None or expected_continuation is None:
                raise PolicyViolation("Continuation expected provenance eksik")
            cursor.execute(
                "select c.id campaign_id, c.work_item_id, c.task_plan_id, c.revision,"
                " c.source_revision, c.campaign_digest, o.id outcome_id, o.status outcome_status,"
                " o.passed_count, o.failed_count, o.recovery_required_count,"
                " o.actual_tested_call_count, o.actual_provider_call_count,"
                " o.evidence_digest outcome_evidence_digest, o.outcome_digest"
                " from models.opencode_benchmark_campaign c"
                " join models.opencode_benchmark_campaign_outcome o"
                "   on o.realm_id=c.realm_id and o.campaign_id=c.id"
                " where c.realm_id=%s and c.id=%s",
                (realm_id, parent_id),
            )
            parent = _one(cursor.fetchall(), "Canonical parent campaign bulunamadi")
            if (
                parent["outcome_status"] != "recovery-required"
                or current["work_item_id"] != parent["work_item_id"]
                or current["revision"] != parent["revision"] + 1
                or current["parent_source_revision"] != parent["source_revision"]
            ):
                raise PolicyViolation("Campaign continuation parent/source binding drift")
        elif expected_parent_campaign is not None or expected_continuation is not None:
            raise PolicyViolation("Standalone campaign continuation provenance tasiyamaz")
        if current["source_revision"] != expected_source_revision:
            raise PolicyViolation("Campaign current source revision drift")
        for key, value in expected_bindings.items():
            if current.get(key) != value:
                raise PolicyViolation(f"Campaign current {key} binding drift")
        if current["campaign_digest"] != expected_campaign.campaign_digest:
            raise PolicyViolation("Campaign canonical digest drift")
        if is_continuation:
            assert parent is not None
            assert expected_parent_campaign is not None
            assert expected_continuation is not None
            if (
                parent["campaign_digest"] != expected_parent_campaign.campaign_digest
                or current["compatibility_evidence_digest"]
                != expected_continuation.compatibility_evidence_digest
                or current["continuation_provenance_digest"]
                != expected_continuation.continuation_provenance_digest
                or current["continuation_tested_call_budget"]
                != expected_continuation.maximum_tested_call_count
                or current["continuation_provider_call_budget"]
                != expected_continuation.maximum_provider_call_count
            ):
                raise PolicyViolation("Campaign/continuation canonical digest drift")
        elif any(
            current[key] is not None
            for key in (
                "parent_source_revision",
                "compatibility_evidence_digest",
                "continuation_provenance_digest",
                "continuation_tested_call_budget",
                "continuation_provider_call_budget",
            )
        ):
            raise PolicyViolation("Standalone campaign continuation metadata tasiyamaz")

        current_outcome = CampaignOutcome(
            status=CampaignOutcomeStatus(str(current["outcome_status"])),
            passed_count=int(current["passed_count"]),
            failed_count=int(current["failed_count"]),
            recovery_required_count=int(current["recovery_required_count"]),
            audio_excluded_count=int(current["audio_excluded_count"]),
            actual_tested_call_count=int(current["actual_tested_call_count"]),
            actual_provider_call_count=int(current["actual_provider_call_count"]),
            evidence_digest=str(current["outcome_evidence_digest"]),
        )
        if current_outcome.outcome_digest != current["outcome_digest"]:
            raise PolicyViolation("Campaign outcome canonical digest drift")
        if parent is not None:
            parent_outcome = CampaignOutcome(
                status=CampaignOutcomeStatus(str(parent["outcome_status"])),
                passed_count=int(parent["passed_count"]),
                failed_count=int(parent["failed_count"]),
                recovery_required_count=int(parent["recovery_required_count"]),
                audio_excluded_count=int(current["audio_excluded_count"]),
                actual_tested_call_count=int(parent["actual_tested_call_count"]),
                actual_provider_call_count=int(parent["actual_provider_call_count"]),
                evidence_digest=str(parent["outcome_evidence_digest"]),
            )
            if parent_outcome.outcome_digest != parent["outcome_digest"]:
                raise PolicyViolation("Parent campaign outcome canonical digest drift")
        cursor.execute(
            "select count(*) n from models.opencode_benchmark_campaign"
            " where realm_id=%s and campaign_key='opencode-aihub' and revision>%s",
            (realm_id, current["revision"]),
        )
        if _required(cursor.fetchone(), "Campaign revision count bulunamadi")["n"] != 0:
            raise PolicyViolation("Campaign continuation latest revision degil")
        cursor.execute(
            "select count(*) n from work.task_plan where realm_id=%s and work_item_id=%s"
            " and revision>(select revision from work.task_plan where id=%s)",
            (realm_id, current["work_item_id"], current["task_plan_id"]),
        )
        if _required(cursor.fetchone(), "TaskPlan revision count bulunamadi")["n"] != 0:
            raise PolicyViolation("Campaign TaskPlan current degil")

        plan_ids = (
            (current["task_plan_id"],)
            if parent is None
            else (parent["task_plan_id"], current["task_plan_id"])
        )
        cursor.execute(
            "select j.id job_id, j.project_id, j.plan_id, j.state job_state, j.max_attempts,"
            " a.id attempt_id, a.outcome attempt_outcome, ec.id campaign_claim_id,"
            " er.id campaign_receipt_id, er.status receipt_status,"
            " cp.id checkpoint_record_id, cp.checkpoint_key,"
            " cardinality(cp.pending_steps) pending_count, cp.grants_authority"
            " from runtime.job j"
            " join runtime.job_attempt a on a.realm_id=j.realm_id and a.job_id=j.id"
            " join runtime.effect_claim ec on ec.realm_id=j.realm_id and ec.job_id=j.id"
            "   and ec.operation='model-campaign-outcome-ledger'"
            " join runtime.effect_receipt er on er.realm_id=ec.realm_id and er.claim_id=ec.id"
            " join work.checkpoint cp on cp.realm_id=j.realm_id and cp.job_id=j.id"
            " where j.realm_id=%s and j.plan_id=%s and j.step_id='campaign-finalize'"
            "   and er.result_digest=%s and a.result_digest=%s",
            (
                realm_id,
                current["task_plan_id"],
                current["outcome_digest"],
                current["outcome_digest"],
            ),
        )
        runtime = _one(cursor.fetchall(), "Canonical continuation runtime zinciri tekil degil")
        if (
            runtime["job_state"] != "completed"
            or runtime["max_attempts"] != 1
            or runtime["attempt_outcome"] != "succeeded"
            or runtime["receipt_status"] != "completed"
            or runtime["pending_count"] != 0
            or runtime["grants_authority"]
        ):
            raise PolicyViolation("Canonical continuation runtime terminal degil")
        cursor.execute(
            "select ec.effect_digest, ec.authorization_digest, ec.resources, ec.claimed_at,"
            " er.adapter_evidence_digest, er.completed_at,"
            " au.id authorization_id, au.authorization_digest auth_digest,"
            " au.effect_digest auth_effect_digest,"
            " au.plan_digest, au.state auth_state, au.work_item_id, au.plan_id auth_plan_id,"
            " au.allowed_resources, au.allowed_effects, au.provider_refs, au.secret_ref_ids,"
            " au.scope, au.consumed_at"
            " from runtime.effect_claim ec"
            " join runtime.effect_receipt er on er.realm_id=ec.realm_id and er.claim_id=ec.id"
            " join security.authorization au on au.realm_id=ec.realm_id"
            "   and au.id=ec.authorization_id"
            " where ec.realm_id=%s and ec.id=%s",
            (realm_id, runtime["campaign_claim_id"]),
        )
        campaign_authority = _required(
            cursor.fetchone(), "Campaign DB_WRITE authority zinciri bulunamadi"
        )
        campaign_effect_digest = digest(
            {
                "campaign_digest": expected_campaign.campaign_digest,
                "effect": "campaign-outcome-qualification-ledger",
            }
        )
        campaign_resource = f"work:{runtime['project_id']}:{current['work_item_id']}"
        if (
            campaign_authority["effect_digest"] != campaign_effect_digest
            or campaign_authority["authorization_digest"] != campaign_authority["auth_digest"]
            or campaign_authority["auth_effect_digest"] != campaign_effect_digest
            or campaign_authority["plan_digest"] != expected_campaign.campaign_digest
            or campaign_authority["auth_state"] != "consumed"
            or campaign_authority["work_item_id"] != current["work_item_id"]
            or campaign_authority["auth_plan_id"] != current["task_plan_id"]
            or tuple(campaign_authority["allowed_resources"]) != (campaign_resource,)
            or tuple(campaign_authority["allowed_effects"]) != ("database-write",)
            or tuple(campaign_authority["provider_refs"])
            or tuple(campaign_authority["secret_ref_ids"])
            or dict(campaign_authority["scope"]).get("data_classifications") != ["public"]
            or list(campaign_authority["resources"])
            != [{"mode": "write", "resource": campaign_resource}]
            or campaign_authority["adapter_evidence_digest"] != current["outcome_evidence_digest"]
            or not campaign_authority["claimed_at"]
            <= campaign_authority["consumed_at"]
            <= campaign_authority["completed_at"]
        ):
            raise PolicyViolation("Campaign DB_WRITE canonical authority digest drift")

        cursor.execute(
            "select id, provider, allowed_operations, store_backend, store_locator, status,"
            " (expires_at is null or expires_at > now()) usable"
            " from security.secret_ref where realm_id=%s and name=%s"
            " order by version desc limit 1",
            (realm_id, expected_secret_name),
        )
        secret_ref = _required(cursor.fetchone(), "Current benchmark SecretRef bulunamadi")
        if (
            secret_ref["provider"] != expected_campaign.provider_ref
            or secret_ref["store_backend"] != "environment"
            or secret_ref["store_locator"] != expected_secret_locator
            or secret_ref["status"] != "active"
            or not secret_ref["usable"]
        ):
            raise PolicyViolation("Current benchmark SecretRef metadata drift")

        cursor.execute(
            "select substr(ec.operation,length('provider-contract:')+1) call_id,"
            " j.id job_id, j.plan_id, ec.id claim_id, ec.effect_digest, ec.authorization_digest,"
            " ec.resources, ec.claimed_at, er.id receipt_id, er.status receipt_status,"
            " er.result_digest response_digest, er.adapter_evidence_digest, er.completed_at,"
            " au.id authorization_id, au.plan_digest, au.effect_digest auth_effect_digest,"
            " au.authorization_digest auth_digest, au.state auth_state,"
            " au.work_item_id, au.plan_id auth_plan_id, au.allowed_resources,"
            " au.allowed_effects, au.provider_refs, au.secret_ref_ids, au.scope, au.consumed_at"
            " from runtime.effect_claim ec"
            " join runtime.effect_receipt er on er.realm_id=ec.realm_id and er.claim_id=ec.id"
            " join runtime.job j on j.realm_id=ec.realm_id and j.id=ec.job_id"
            " join security.authorization au on au.realm_id=ec.realm_id"
            "   and au.id=ec.authorization_id"
            " where ec.realm_id=%s and j.plan_id=any(%s)"
            "   and ec.operation like 'provider-contract:%%' order by ec.claimed_at",
            (realm_id, list(plan_ids)),
        )
        call_rows = cursor.fetchall()
        calls: list[dict[str, Any]] = []
        call_ids: set[str] = set()
        call_receipts: dict[str, UUID] = {}
        call_response_digests: dict[str, str] = {}
        for row in call_rows:
            call_id = str(row["call_id"])
            resources = list(row["resources"])
            scope = dict(row["scope"])
            if (
                call_id in call_ids
                or call_id not in expected_calls
                or row["receipt_status"] != "completed"
                or row["response_digest"] is None
                or row["adapter_evidence_digest"] is None
                or row["auth_state"] != "consumed"
                or row["effect_digest"] != row["auth_effect_digest"]
                or row["authorization_digest"] != row["auth_digest"]
                or row["work_item_id"] != current["work_item_id"]
                or row["auth_plan_id"] != row["plan_id"]
                or tuple(row["allowed_effects"]) != ("provider-call",)
                or len(row["provider_refs"]) != 1
                or tuple(row["secret_ref_ids"]) != (secret_ref["id"],)
                or scope.get("data_classifications") != ["public"]
                or len(resources) != 1
                or resources[0].get("mode") != "write"
                or resources[0].get("resource") not in row["allowed_resources"]
                or not row["claimed_at"] <= row["consumed_at"] <= row["completed_at"]
            ):
                raise PolicyViolation("Canonical provider authority/receipt binding drift")
            planned = expected_calls[call_id].prepared.plan
            if (
                row["plan_digest"] != planned.authorization_plan_digest
                or row["effect_digest"] != planned.effect_request.effect_digest
                or set(row["allowed_resources"]) != {planned.target, planned.call_resource}
                or resources != [{"mode": "write", "resource": planned.call_resource}]
                or tuple(row["provider_refs"]) != (planned.provider_ref,)
                or planned.operation not in secret_ref["allowed_operations"]
            ):
                raise PolicyViolation("Canonical provider manifest plan/effect drift")
            call_ids.add(call_id)
            call_receipts[call_id] = UUID(str(row["receipt_id"]))
            call_response_digests[call_id] = str(row["response_digest"])
            calls.append(
                {
                    "call_id": call_id,
                    "campaign_id": str(
                        parent_id
                        if parent is not None and row["plan_id"] == parent["task_plan_id"]
                        else campaign_id
                    ),
                    "authorization_id": str(row["authorization_id"]),
                    "claim_id": str(row["claim_id"]),
                    "receipt_id": str(row["receipt_id"]),
                    "receipt_status": str(row["receipt_status"]),
                    "response_digest": str(row["response_digest"]),
                    "provider_evidence_digest": str(row["adapter_evidence_digest"]),
                    "plan_digest": str(row["plan_digest"]),
                    "effect_digest": str(row["effect_digest"]),
                }
            )

        cursor.execute(
            "select m.id member_id, m.canonical_model_id model_id,"
            " h.id health_result_id, h.status health_status,"
            " h.evidence_digest health_evidence_digest, h.result_digest health_result_digest,"
            " h.failure_category health_failure_category,"
            " h.actual_tested_call_count health_tested_calls,"
            " h.actual_provider_call_count health_provider_calls,"
            " h.adopted_from_campaign_id health_adopted_campaign,"
            " h.adopted_from_result_id health_adopted_from,"
            " h.adoption_provenance_digest health_adoption_provenance,"
            " h.recovered_from_claim_id, h.recovered_from_receipt_id,"
            " h.recovery_provenance_digest,"
            " b.id benchmark_result_id, b.status benchmark_status,"
            " b.evidence_digest benchmark_evidence_digest,"
            " b.result_digest benchmark_result_digest,"
            " b.failure_category benchmark_failure_category,"
            " b.actual_tested_call_count benchmark_tested_calls,"
            " b.actual_provider_call_count benchmark_provider_calls, b.aggregate_id,"
            " b.adopted_from_campaign_id benchmark_adopted_campaign,"
            " b.adopted_from_result_id benchmark_adopted_from,"
            " b.adoption_provenance_digest benchmark_adoption_provenance,"
            " q.id qualification_id, q.action qualification,"
            " q.evidence_digest qualification_evidence_digest,"
            " q.event_digest, q.reason_code"
            " from models.opencode_benchmark_campaign_member m"
            " join models.opencode_benchmark_campaign_member_result h"
            "   on h.realm_id=m.realm_id and h.campaign_id=m.campaign_id"
            "  and h.member_id=m.id and h.stage='health'"
            " left join models.opencode_benchmark_campaign_member_result b"
            "   on b.realm_id=m.realm_id and b.campaign_id=m.campaign_id"
            "  and b.member_id=m.id and b.stage='benchmark'"
            " join models.opencode_model_qualification_event q"
            "   on q.realm_id=m.realm_id and q.campaign_id=m.campaign_id and q.member_id=m.id"
            " where m.realm_id=%s and m.campaign_id=%s and m.disposition='health-pending'"
            " order by m.canonical_model_id",
            (realm_id, campaign_id),
        )
        member_rows = cursor.fetchall()
        if len(member_rows) != 17:
            raise PolicyViolation("Canonical exact 17 eligible member sonucu ister")
        members: list[dict[str, Any]] = []
        health_passed = qualified = 0
        member_result_digests: dict[str, str] = {}
        aggregate_ids: dict[str, UUID] = {}
        failed_models: dict[str, str] = {}
        trial_authorization_ids_by_model: dict[str, UUID] = {}
        for row in member_rows:
            hp = row["health_status"] == "passed"
            benchmark_status = (
                "not-run" if row["benchmark_result_id"] is None else str(row["benchmark_status"])
            )
            repetitions = 5 if row["benchmark_result_id"] is not None else 0
            expected_qualification = (
                "qualified" if hp and benchmark_status == "passed" else "disqualified"
            )
            if (
                row["qualification"] != expected_qualification
                or (hp and row["benchmark_result_id"] is None)
                or (not hp and row["benchmark_result_id"] is not None)
                or (
                    row["health_adopted_from"] is not None
                    and row["recovered_from_claim_id"] is not None
                )
            ):
                raise PolicyViolation("Canonical member health/benchmark/qualification drift")

            def result_for(stage: str, member_row: dict[str, Any] = row) -> CampaignMemberResult:
                adopted_id = member_row[f"{stage}_adopted_from"]
                adoption = None
                if adopted_id is not None:
                    if parent is None or expected_continuation is None:
                        raise PolicyViolation("Standalone campaign adopted result tasiyamaz")
                    if member_row[f"{stage}_adopted_campaign"] != parent_id:
                        raise PolicyViolation("Adopted result parent campaign drift")
                    cursor.execute(
                        "select result_digest from models.opencode_benchmark_campaign_member_result"
                        " where realm_id=%s and id=%s and campaign_id=%s",
                        (realm_id, adopted_id, parent_id),
                    )
                    source = _required(cursor.fetchone(), "Adopted source result bulunamadi")
                    expected_adoption = digest(
                        {
                            "schema": "zekam-opencode-result-adoption/v1",
                            "continuation": expected_continuation.continuation_provenance_digest,
                            "parent_result_id": adopted_id,
                            "parent_result_digest": source["result_digest"],
                            "model_id": str(member_row["model_id"]),
                        }
                    )
                    if member_row[f"{stage}_adoption_provenance"] != expected_adoption:
                        raise PolicyViolation("Adopted result provenance digest drift")
                    adoption = ResultAdoption(UUID(str(adopted_id)), expected_adoption)
                recovery = None
                if stage == "health" and member_row["recovered_from_claim_id"] is not None:
                    if expected_continuation is None:
                        raise PolicyViolation("Standalone campaign recovered result tasiyamaz")
                    cursor.execute(
                        "select operation from runtime.effect_claim where realm_id=%s and id=%s",
                        (realm_id, member_row["recovered_from_claim_id"]),
                    )
                    recovered_claim = _required(cursor.fetchone(), "Recovered claim bulunamadi")
                    call_id = str(recovered_claim["operation"]).removeprefix("provider-contract:")
                    expected_recovery = digest(
                        {
                            "schema": "zekam-opencode-health-projection-recovery/v1",
                            "continuation": expected_continuation.continuation_provenance_digest,
                            "call_id": call_id,
                            "claim_id": member_row["recovered_from_claim_id"],
                            "receipt_id": member_row["recovered_from_receipt_id"],
                        }
                    )
                    if member_row["recovery_provenance_digest"] != expected_recovery:
                        raise PolicyViolation("Recovered health provenance digest drift")
                    recovery = ResultRecoveryEvidence(
                        UUID(str(member_row["recovered_from_claim_id"])),
                        UUID(str(member_row["recovered_from_receipt_id"])),
                        expected_recovery,
                    )
                result = CampaignMemberResult(
                    stage=CampaignMemberResultStage(stage),
                    status=CampaignMemberResultStatus(str(member_row[f"{stage}_status"])),
                    evidence_digest=str(member_row[f"{stage}_evidence_digest"]),
                    actual_tested_call_count=int(member_row[f"{stage}_tested_calls"]),
                    actual_provider_call_count=int(member_row[f"{stage}_provider_calls"]),
                    aggregate_id=(
                        None
                        if stage == "health" or member_row["aggregate_id"] is None
                        else UUID(str(member_row["aggregate_id"]))
                    ),
                    failure_category=(
                        None
                        if member_row[f"{stage}_failure_category"] is None
                        else str(member_row[f"{stage}_failure_category"])
                    ),
                    adoption=adoption,
                    recovery_evidence=recovery,
                )
                if result.result_digest != member_row[f"{stage}_result_digest"]:
                    raise PolicyViolation("Campaign member result canonical digest drift")
                return result

            health_result = result_for("health")
            benchmark_result = (
                None if row["benchmark_result_id"] is None else result_for("benchmark")
            )
            terminal_result = benchmark_result or health_result
            qualification_member_evidence = (
                terminal_result.result_digest
                if benchmark_result is not None
                or row["health_adopted_from"] is not None
                or row["recovered_from_claim_id"] is not None
                else terminal_result.evidence_digest
            )
            member_result_digests[str(row["model_id"])] = qualification_member_evidence
            if row["qualification"] == "qualified":
                if terminal_result.aggregate_id is None:
                    raise PolicyViolation("Qualified result aggregate missing")
                aggregate_ids[str(row["model_id"])] = terminal_result.aggregate_id
            else:
                if terminal_result.failure_category is None:
                    raise PolicyViolation("Disqualified result failure missing")
                failed_models[str(row["model_id"])] = terminal_result.failure_category
            expected_qualification_evidence = digest(
                {
                    "campaign_outcome": current_outcome.outcome_digest,
                    "model_id": str(row["model_id"]),
                    "member_result": qualification_member_evidence,
                }
            )
            qualification_event = QualificationEvent(
                action=QualificationAction(str(row["qualification"])),
                model_id=str(row["model_id"]),
                outcome_id=UUID(str(current["outcome_id"])),
                evidence_digest=expected_qualification_evidence,
                aggregate_id=(
                    None if row["aggregate_id"] is None else UUID(str(row["aggregate_id"]))
                ),
                reason_code=None if row["reason_code"] is None else str(row["reason_code"]),
            )
            if (
                row["qualification_evidence_digest"] != expected_qualification_evidence
                or row["event_digest"] != qualification_event.event_digest
            ):
                raise PolicyViolation(
                    "Qualification canonical evidence/event digest drift: " + str(row["model_id"])
                )
            if row["benchmark_result_id"] is not None:
                source_result_id = row["benchmark_adopted_from"] or row["benchmark_result_id"]
                cursor.execute(
                    "select r.member_id source_member_id, t.id trial_id,"
                    " t.fixture_digest, t.repetition, t.response_digest,"
                    " t.evidence_digest trial_evidence_digest,"
                    " t.status trial_status, t.parse_ok, t.format_ok, t.evidence_ok,"
                    " t.verifier_approved, t.quality, t.reliability, t.latency_ms,"
                    " t.input_tokens, t.output_tokens, t.retry_count, t.human_corrections,"
                    " t.estimated_cost, t.actual_cost, t.failure_category,"
                    " t.tested_model_id trial_tested_model_id,"
                    " t.verifier_model_id trial_verifier_model_id,"
                    " t.verifier_execution_identity trial_verifier_execution_identity,"
                    " t.verifier_evidence_digest, t.verifier_provenance_digest,"
                    " bp.plan_digest benchmark_plan_digest, s.suite_digest,"
                    " tc.id tested_claim_id, tr.id tested_receipt_id,"
                    " tr.status tested_receipt_status, tr.result_digest tested_result_digest,"
                    " tr.adapter_evidence_digest tested_adapter_evidence,"
                    " tc.effect_digest tested_effect_digest,"
                    " tc.authorization_digest tested_claim_authorization_digest,"
                    " tc.resources tested_resources, tc.operation tested_operation,"
                    " vc.id verifier_claim_id, vr.id verifier_receipt_id,"
                    " vr.status verifier_receipt_status, vr.result_digest verifier_result_digest,"
                    " vr.adapter_evidence_digest verifier_adapter_evidence,"
                    " vc.effect_digest verifier_effect_digest,"
                    " vc.authorization_digest verifier_claim_authorization_digest,"
                    " vc.resources verifier_resources, vc.operation verifier_operation,"
                    " vc.adapter_digest verifier_claim_adapter_digest,"
                    " ta.id tested_authorization_id, ta.authorization_digest tested_auth_digest,"
                    " ta.plan_digest tested_auth_plan_digest,"
                    " ta.effect_digest member_effect_digest,"
                    " ta.state tested_auth_state, ta.allowed_resources tested_allowed_resources,"
                    " ta.allowed_effects tested_allowed_effects, ta.provider_refs tested_providers,"
                    " ta.secret_ref_ids tested_secrets, ta.scope tested_scope,"
                    " va.id verifier_authorization_id,"
                    " va.authorization_digest verifier_auth_digest,"
                    " va.plan_digest verifier_auth_plan_digest, va.state verifier_auth_state,"
                    " v.id verifier_result_id, v.tested_model_id verdict_tested_model_id,"
                    " v.verifier_model_id verdict_verifier_model_id,"
                    " v.tested_response_digest, v.approved verifier_result_approved,"
                    " v.evidence_digest canonical_verifier_evidence, v.execution_identity,"
                    " a.id aggregate_id, a.tested_model_id aggregate_tested_model_id,"
                    " a.verifier_model_id aggregate_verifier_model_id,"
                    " a.verifier_execution_identity aggregate_verifier_execution_identity,"
                    " a.verifier_provenance_digest aggregate_verifier_provenance_digest,"
                    " a.approved aggregate_approved, a.unsafe aggregate_unsafe,"
                    " a.metrics aggregate_metrics, a.evidence_digest aggregate_evidence_digest"
                    " from models.opencode_benchmark_campaign_member_result r"
                    " join models.opencode_benchmark_campaign_member_plan mp"
                    "   on mp.realm_id=r.realm_id and mp.id=r.member_plan_id"
                    " join models.benchmark_plan bp on bp.realm_id=mp.realm_id"
                    "   and bp.id=mp.benchmark_plan_id"
                    " join models.benchmark_suite s on s.realm_id=bp.realm_id and s.id=bp.suite_id"
                    " join models.benchmark_trial t on t.realm_id=bp.realm_id and t.plan_id=bp.id"
                    " join runtime.effect_claim tc"
                    "   on tc.realm_id=t.realm_id and tc.id=t.tested_claim_id"
                    " join runtime.effect_receipt tr"
                    "   on tr.realm_id=tc.realm_id and tr.claim_id=tc.id"
                    " join security.authorization ta"
                    "   on ta.realm_id=tc.realm_id and ta.id=tc.authorization_id"
                    " join runtime.effect_claim vc"
                    "   on vc.realm_id=t.realm_id and vc.id=t.verifier_claim_id"
                    " join runtime.effect_receipt vr"
                    "   on vr.realm_id=vc.realm_id and vr.claim_id=vc.id"
                    " join security.authorization va"
                    "   on va.realm_id=vc.realm_id and va.id=vc.authorization_id"
                    " join models.benchmark_verifier_result v"
                    "   on v.realm_id=t.realm_id and v.claim_id=t.verifier_claim_id"
                    " left join models.benchmark_aggregate a"
                    "   on a.realm_id=bp.realm_id and a.plan_id=bp.id"
                    " where r.realm_id=%s and r.id=%s and bp.model_id=%s",
                    (realm_id, source_result_id, row["model_id"]),
                )
                trials = cursor.fetchall()
                if len(trials) != 5 or {int(item["repetition"]) for item in trials} != set(
                    range(1, 6)
                ):
                    raise PolicyViolation("Canonical independent verifier 5-trial zinciri drift")
                reconstructed_trials: list[TrialResult] = []
                for trial in trials:
                    planned_matches = [
                        planned
                        for planned in expected_calls.values()
                        if planned.kind.value == "benchmark"
                        and planned.canonical_model_id == str(row["model_id"])
                        and planned.fixture_digest == str(trial["fixture_digest"])
                        and planned.repetition == int(trial["repetition"])
                    ]
                    if len(planned_matches) != 1:
                        raise PolicyViolation("Trial exact provider call plan drift")
                    planned_call = planned_matches[0]
                    provider_receipt_id = call_receipts.get(planned_call.call_id)
                    if provider_receipt_id is None:
                        raise PolicyViolation("Trial provider receipt binding missing")
                    expected_trial_evidence = digest(
                        {
                            "tested": str(trial["tested_adapter_evidence"]),
                            "verifier": str(trial["canonical_verifier_evidence"]),
                            "provider_receipt": provider_receipt_id,
                        }
                    )
                    verdict = VerifierVerdict(
                        tested_model_id=str(trial["verdict_tested_model_id"]),
                        verifier_model_id=str(trial["verdict_verifier_model_id"]),
                        execution_identity=str(trial["execution_identity"]),
                        tested_response_digest=str(trial["tested_response_digest"]),
                        approved=bool(trial["verifier_result_approved"]),
                        evidence_digest=str(trial["canonical_verifier_evidence"]),
                    )
                    reconstructed_trial = TrialResult(
                        fixture_digest=str(trial["fixture_digest"]),
                        repetition=int(trial["repetition"]),
                        status=TrialStatus(str(trial["trial_status"])),
                        parse_ok=bool(trial["parse_ok"]),
                        format_ok=bool(trial["format_ok"]),
                        evidence_ok=bool(trial["evidence_ok"]),
                        verifier_approved=bool(trial["verifier_approved"]),
                        quality=float(trial["quality"]),
                        reliability=float(trial["reliability"]),
                        latency_ms=int(trial["latency_ms"]),
                        input_tokens=int(trial["input_tokens"]),
                        output_tokens=int(trial["output_tokens"]),
                        retry_count=int(trial["retry_count"]),
                        human_corrections=int(trial["human_corrections"]),
                        estimated_cost=float(trial["estimated_cost"]),
                        actual_cost=(
                            None if trial["actual_cost"] is None else float(trial["actual_cost"])
                        ),
                        response_digest=str(trial["response_digest"]),
                        evidence_digest=str(trial["trial_evidence_digest"]),
                        failure_category=(
                            None
                            if trial["failure_category"] is None
                            else str(trial["failure_category"])
                        ),
                    )
                    benchmark_plan_digest = str(trial["benchmark_plan_digest"])
                    suite_resource = (
                        f"model-benchmark:{row['model_id']}:"
                        f"{str(trial['suite_digest']).removeprefix('sha256:')}"
                    )
                    ledger_resource = f"model-benchmark:{row['model_id']}:campaign-ledger"
                    if row["benchmark_adopted_from"] is not None:
                        if expected_parent_campaign is None:
                            raise PolicyViolation("Standalone campaign adopted benchmark tasiyamaz")
                        effect_campaign_digest = expected_parent_campaign.campaign_digest
                    else:
                        effect_campaign_digest = expected_campaign.campaign_digest
                    expected_member_effect = digest(
                        {
                            "campaign_digest": effect_campaign_digest,
                            "member_id": UUID(str(trial["source_member_id"])),
                            "benchmark_plan_digest": benchmark_plan_digest,
                            "effect": "benchmark-ledger-write",
                        }
                    )
                    tested_authorization_id = UUID(str(trial["tested_authorization_id"]))
                    existing_trial_authorization = tested_authorization_id
                    if row["benchmark_adopted_from"] is None:
                        existing_trial_authorization = trial_authorization_ids_by_model.setdefault(
                            str(row["model_id"]), tested_authorization_id
                        )
                    expected_tested_effect = benchmark_effect_digest(
                        benchmark_plan_digest,
                        str(trial["fixture_digest"]),
                        int(trial["repetition"]),
                    )
                    expected_verifier_effect = benchmark_verifier_effect_digest(
                        benchmark_plan_digest,
                        str(trial["fixture_digest"]),
                        int(trial["repetition"]),
                        verdict.verifier_model_id,
                        reconstructed_trial.response_digest,
                    )
                    trial_drift = tuple(
                        label
                        for label, mismatch in (
                            (
                                "trial-evidence",
                                trial["trial_evidence_digest"] != expected_trial_evidence,
                            ),
                            (
                                "verifier-evidence",
                                trial["verifier_evidence_digest"]
                                != trial["canonical_verifier_evidence"],
                            ),
                            (
                                "verifier-approval",
                                reconstructed_trial.verifier_approved != verdict.approved,
                            ),
                            (
                                "verifier-provenance",
                                trial["verifier_provenance_digest"]
                                != expected_campaign.verifier_provenance_digest,
                            ),
                            (
                                "tested-response",
                                trial["tested_response_digest"] != trial["response_digest"],
                            ),
                            (
                                "tested-result",
                                trial["tested_result_digest"] != trial["response_digest"],
                            ),
                            ("tested-receipt", trial["tested_receipt_status"] != "completed"),
                            ("verifier-receipt", trial["verifier_receipt_status"] != "completed"),
                            (
                                "verifier-result",
                                trial["verifier_result_digest"]
                                != trial["canonical_verifier_evidence"],
                            ),
                            (
                                "verifier-adapter-evidence",
                                trial["verifier_adapter_evidence"]
                                != trial["canonical_verifier_evidence"],
                            ),
                            (
                                "verifier-identity",
                                trial["execution_identity"] != expected_campaign.verifier_identity,
                            ),
                            (
                                "self-verifier",
                                trial["verdict_verifier_model_id"] == str(row["model_id"]),
                            ),
                            (
                                "tested-operation",
                                trial["tested_operation"] != "model-benchmark-tested",
                            ),
                            (
                                "verifier-operation",
                                trial["verifier_operation"] != "model-benchmark-verifier",
                            ),
                            (
                                "tested-effect",
                                trial["tested_effect_digest"] != expected_tested_effect,
                            ),
                            (
                                "verifier-effect",
                                trial["verifier_effect_digest"] != expected_verifier_effect,
                            ),
                            (
                                "tested-authorization",
                                trial["tested_claim_authorization_digest"]
                                != trial["tested_auth_digest"],
                            ),
                            (
                                "verifier-authorization",
                                trial["verifier_claim_authorization_digest"]
                                != trial["verifier_auth_digest"],
                            ),
                            (
                                "authorization-identity",
                                tested_authorization_id
                                != UUID(str(trial["verifier_authorization_id"]))
                                or existing_trial_authorization != tested_authorization_id,
                            ),
                            (
                                "authorization-plan",
                                trial["tested_auth_plan_digest"] != benchmark_plan_digest
                                or trial["verifier_auth_plan_digest"] != benchmark_plan_digest,
                            ),
                            (
                                "authorization-effect",
                                trial["member_effect_digest"] != expected_member_effect,
                            ),
                            (
                                "authorization-state",
                                trial["tested_auth_state"] != "consumed"
                                or trial["verifier_auth_state"] != "consumed",
                            ),
                            (
                                "authorization-scope",
                                set(trial["tested_allowed_resources"])
                                != {suite_resource, ledger_resource}
                                or tuple(trial["tested_allowed_effects"]) != ("database-write",)
                                or tuple(trial["tested_providers"])
                                or tuple(trial["tested_secrets"])
                                or dict(trial["tested_scope"]).get("data_classifications")
                                != ["public"],
                            ),
                            (
                                "claim-resources",
                                list(trial["tested_resources"])
                                != [{"mode": "write", "resource": suite_resource}]
                                or list(trial["verifier_resources"])
                                != [{"mode": "write", "resource": suite_resource}],
                            ),
                            (
                                "verifier-claim-adapter",
                                trial["verifier_claim_adapter_digest"]
                                != expected_campaign.verifier_provenance_digest,
                            ),
                            (
                                "model-identities",
                                trial["trial_tested_model_id"] != str(row["model_id"])
                                or verdict.tested_model_id != str(row["model_id"])
                                or trial["trial_verifier_model_id"] != verdict.verifier_model_id
                                or trial["trial_verifier_execution_identity"]
                                != verdict.execution_identity,
                            ),
                        )
                        if mismatch
                    )
                    if trial_drift:
                        raise PolicyViolation(
                            "Trial/verifier canonical evidence digest drift: "
                            + ",".join(trial_drift)
                        )
                    reconstructed_trials.append(reconstructed_trial)

                aggregate_row = trials[0]
                if row["aggregate_id"] is not None:
                    verifier_identity = VerifierIdentity(
                        model_id=str(aggregate_row["verdict_verifier_model_id"]),
                        execution_identity=str(aggregate_row["execution_identity"]),
                        provenance_digest=str(aggregate_row["verifier_provenance_digest"]),
                    )
                    recomputed_aggregate = aggregate_trials(
                        tuple(reconstructed_trials),
                        tested_model_id=str(row["model_id"]),
                        verifier=verifier_identity,
                    )
                    if (
                        aggregate_row["aggregate_id"] != row["aggregate_id"]
                        or aggregate_row["aggregate_tested_model_id"] != str(row["model_id"])
                        or aggregate_row["aggregate_verifier_model_id"]
                        != recomputed_aggregate.verifier_model_id
                        or aggregate_row["aggregate_verifier_execution_identity"]
                        != recomputed_aggregate.verifier_execution_identity
                        or aggregate_row["aggregate_verifier_provenance_digest"]
                        != recomputed_aggregate.verifier_provenance_digest
                        or bool(aggregate_row["aggregate_approved"])
                        != recomputed_aggregate.approved
                        or bool(aggregate_row["aggregate_unsafe"]) != recomputed_aggregate.unsafe
                        or dict(aggregate_row["aggregate_metrics"])
                        != recomputed_aggregate.as_dict()
                        or aggregate_row["aggregate_evidence_digest"]
                        != recomputed_aggregate.evidence_digest
                        or benchmark_result is None
                        or benchmark_result.evidence_digest != recomputed_aggregate.evidence_digest
                    ):
                        raise PolicyViolation("Benchmark aggregate canonical recompute drift")
                elif any(trial["aggregate_id"] is not None for trial in trials):
                    raise PolicyViolation("Unexpected benchmark aggregate binding")
            health_passed += int(hp)
            qualified += int(row["qualification"] == "qualified")
            members.append(
                {
                    "member_id": str(row["member_id"]),
                    "model_id": str(row["model_id"]),
                    "health_result_id": str(row["health_result_id"]),
                    "health_status": str(row["health_status"]),
                    "health_evidence_digest": str(row["health_evidence_digest"]),
                    "benchmark_result_id": None
                    if row["benchmark_result_id"] is None
                    else str(row["benchmark_result_id"]),
                    "benchmark_status": benchmark_status,
                    "result_evidence_digest": str(
                        row["health_evidence_digest"]
                        if row["benchmark_result_id"] is None
                        else row["benchmark_evidence_digest"]
                    ),
                    "repetitions": repetitions,
                    "qualification_id": str(row["qualification_id"]),
                    "qualification": str(row["qualification"]),
                    "qualification_evidence_digest": str(row["qualification_evidence_digest"]),
                    "aggregate_id": None
                    if row["aggregate_id"] is None
                    else str(row["aggregate_id"]),
                    "provenance": (
                        "recovered"
                        if row["recovered_from_claim_id"] is not None
                        else "adopted"
                        if row["health_adopted_from"] is not None
                        or row["benchmark_adopted_from"] is not None
                        else "current"
                    ),
                }
            )

        cursor.execute(
            "select ec.id claim_id, ec.effect_digest, ec.authorization_digest, ec.resources,"
            " ec.claimed_at, er.status receipt_status, er.result_digest,"
            " er.adapter_evidence_digest, er.completed_at,"
            " au.id authorization_id, au.authorization_digest auth_digest,"
            " au.effect_digest auth_effect_digest,"
            " au.plan_digest, au.state auth_state, au.work_item_id, au.plan_id auth_plan_id,"
            " au.allowed_resources, au.allowed_effects, au.provider_refs, au.secret_ref_ids,"
            " au.scope, au.consumed_at, mr.evidence_digest member_evidence_digest,"
            " m.id member_id, m.canonical_model_id model_id,"
            " s.suite_digest"
            " from runtime.effect_claim ec"
            " join runtime.effect_receipt er on er.realm_id=ec.realm_id and er.claim_id=ec.id"
            " join security.authorization au on au.realm_id=ec.realm_id"
            "   and au.id=ec.authorization_id"
            " join models.opencode_benchmark_campaign_member_result mr"
            "   on mr.realm_id=er.realm_id and mr.campaign_id=%s"
            "  and (mr.result_digest=er.result_digest or ("
            "       mr.stage='health' and mr.status='failed'"
            "       and mr.adopted_from_result_id is null"
            "       and mr.recovered_from_claim_id is null"
            "       and mr.evidence_digest=er.result_digest))"
            " join models.opencode_benchmark_campaign_member m"
            "   on m.realm_id=mr.realm_id and m.campaign_id=mr.campaign_id and m.id=mr.member_id"
            " join models.benchmark_plan bp on bp.realm_id=au.realm_id"
            "   and bp.plan_digest=au.plan_digest"
            " join models.benchmark_suite s on s.realm_id=bp.realm_id and s.id=bp.suite_id"
            " where ec.realm_id=%s and ec.job_id=%s"
            "   and ec.operation='model-campaign-member-ledger'",
            (campaign_id, realm_id, runtime["job_id"]),
        )
        member_claims = cursor.fetchall()
        if len(member_claims) != 17:
            raise PolicyViolation("Campaign exact 17 member DB_WRITE claim ister")
        for claim in member_claims:
            model_id = str(claim["model_id"])
            ledger_resource = f"model-benchmark:{model_id}:campaign-ledger"
            suite_resource = (
                f"model-benchmark:{model_id}:{str(claim['suite_digest']).removeprefix('sha256:')}"
            )
            expected_member_effect = digest(
                {
                    "campaign_digest": expected_campaign.campaign_digest,
                    "member_id": UUID(str(claim["member_id"])),
                    "benchmark_plan_digest": str(claim["plan_digest"]),
                    "effect": "benchmark-ledger-write",
                }
            )
            if (
                claim["effect_digest"] != expected_member_effect
                or claim["authorization_digest"] != claim["auth_digest"]
                or claim["auth_effect_digest"] != expected_member_effect
                or claim["auth_state"] != "consumed"
                or claim["work_item_id"] != current["work_item_id"]
                or claim["auth_plan_id"] != current["task_plan_id"]
                or (
                    model_id in trial_authorization_ids_by_model
                    and UUID(str(claim["authorization_id"]))
                    != trial_authorization_ids_by_model[model_id]
                )
                or set(claim["allowed_resources"]) != {ledger_resource, suite_resource}
                or tuple(claim["allowed_effects"]) != ("database-write",)
                or tuple(claim["provider_refs"])
                or tuple(claim["secret_ref_ids"])
                or dict(claim["scope"]).get("data_classifications") != ["public"]
                or list(claim["resources"]) != [{"mode": "write", "resource": ledger_resource}]
                or claim["receipt_status"] != "completed"
                or claim["adapter_evidence_digest"] != claim["member_evidence_digest"]
                or not claim["claimed_at"] <= claim["consumed_at"] <= claim["completed_at"]
            ):
                raise PolicyViolation("Member DB_WRITE canonical authority digest drift")

        actual_current_call_evidence = {
            row["call_id"]: row["provider_evidence_digest"]
            for row in calls
            if row["campaign_id"] == str(campaign_id)
        }
        health_status_by_model = {item["model_id"]: item["health_status"] for item in members}
        current_call_evidence = _validated_executed_call_evidence(
            expected_calls=expected_current_calls,
            executed_evidence=actual_current_call_evidence,
            health_status_by_model=health_status_by_model,
        )
        expected_outcome_evidence = digest(
            {
                "campaign_digest": expected_campaign.campaign_digest,
                "member_results": member_result_digests,
                "aggregate_ids": aggregate_ids,
                "failed_models": failed_models,
                "provider_receipts": current_call_evidence,
            }
        )
        if expected_outcome_evidence != current["outcome_evidence_digest"]:
            raise PolicyViolation("Campaign outcome evidence canonical digest drift")

        if parent is not None:
            assert expected_parent_campaign is not None
            parent_call_rows = [
                row for row in call_rows if row["plan_id"] == parent["task_plan_id"]
            ]
            parent_job_ids = {UUID(str(row["job_id"])) for row in parent_call_rows}
            if len(parent_job_ids) != 1:
                raise PolicyViolation("Parent recovery provider job tekil degil")
            expected_parent_recovery_evidence = digest(
                {
                    "campaign_digest": expected_parent_campaign.campaign_digest,
                    "job_id": next(iter(parent_job_ids)),
                    "error_type": "ValidationFailed",
                    "executed_call_ids": sorted(str(row["call_id"]) for row in parent_call_rows),
                }
            )
            if expected_parent_recovery_evidence != parent["outcome_evidence_digest"]:
                raise PolicyViolation("Parent recovery outcome evidence canonical digest drift")
            cumulative_provider_calls = int(parent["actual_provider_call_count"]) + int(
                current["actual_provider_call_count"]
            )
            cumulative_tested_calls = int(parent["actual_tested_call_count"]) + int(
                current["actual_tested_call_count"]
            )
        else:
            cumulative_provider_calls = int(current["actual_provider_call_count"])
            cumulative_tested_calls = int(current["actual_tested_call_count"])
        if (
            len(calls) != cumulative_provider_calls
            or cumulative_provider_calls != 17 + 5 * health_passed
            or cumulative_provider_calls > int(current["provider_call_budget"])
            or cumulative_tested_calls > int(current["tested_call_budget"])
            or qualified != int(current["passed_count"])
        ):
            raise PolicyViolation("Canonical continuation cumulative budget/count drift")

        cursor.execute(
            "select count(*) filter(where state='issued') issued"
            " from security.authorization where realm_id=%s and plan_id=any(%s)",
            (realm_id, list(plan_ids)),
        )
        if _required(cursor.fetchone(), "Authorization state count bulunamadi")["issued"] != 0:
            raise PolicyViolation("Campaign terminal issued authorization birakti")
        cursor.execute(
            "select"
            " (select count(*) from runtime.claim_without_receipt c"
            "   join runtime.job j on j.id=c.job_id where j.plan_id=any(%s)) receiptless,"
            " (select count(*) from runtime.lease l"
            "   join runtime.job j on j.id=l.job_id where j.plan_id=any(%s)) leases,"
            " (select count(*) from runtime.resource_lock l"
            "   join runtime.job j on j.id=l.job_id where j.plan_id=any(%s)) locks",
            (list(plan_ids), list(plan_ids), list(plan_ids)),
        )
        open_state = _required(cursor.fetchone(), "Runtime open-state count bulunamadi")
        if tuple(open_state.values()) != (0, 0, 0):
            raise PolicyViolation("Campaign terminal runtime acik state birakti")

    chain = []
    if parent is not None:
        chain.append(
            {
                "campaign_id": str(parent_id),
                "revision": int(parent["revision"]),
                "outcome_id": str(parent["outcome_id"]),
                "outcome_status": str(parent["outcome_status"]),
                "provider_calls": int(parent["actual_provider_call_count"]),
                "tested_calls": int(parent["actual_tested_call_count"]),
                "outcome_digest": str(parent["outcome_digest"]),
            }
        )
    chain.append(
        {
            "campaign_id": str(campaign_id),
            "revision": int(current["revision"]),
            "outcome_id": str(current["outcome_id"]),
            "outcome_status": str(current["outcome_status"]),
            "provider_calls": int(current["actual_provider_call_count"]),
            "tested_calls": int(current["actual_tested_call_count"]),
            "outcome_digest": str(current["outcome_digest"]),
        }
    )
    runtime_evidence = {
        "job_id": str(runtime["job_id"]),
        "attempt_id": str(runtime["attempt_id"]),
        "campaign_claim_id": str(runtime["campaign_claim_id"]),
        "campaign_receipt_id": str(runtime["campaign_receipt_id"]),
        "checkpoint_record_id": str(runtime["checkpoint_record_id"]),
        "checkpoint_key": str(runtime["checkpoint_key"]),
        "job_state": "completed",
        "attempt_outcome": "succeeded",
        "receiptless_claim_count": 0,
        "open_lease_count": 0,
        "open_resource_lock_count": 0,
        "checkpoint_complete": True,
    }
    qualification_set_digest = digest(
        sorted(row["qualification_evidence_digest"] for row in members)
    )
    runtime_evidence_digest = digest(runtime_evidence)
    verifier_digest = digest(
        {
            "schema": SCHEMA,
            "campaign_digest": str(current["campaign_digest"]),
            "outcome_digest": str(current["outcome_digest"]),
            "chain": chain,
            "calls": calls,
            "members": members,
            "runtime": runtime_evidence,
        }
    )
    return {
        "schema": SCHEMA,
        "status": "verified",
        "campaign_key": "opencode-aihub",
        "campaign_id": str(campaign_id),
        "parent_campaign_id": None if parent_id is None else str(parent_id),
        "outcome_id": str(current["outcome_id"]),
        "work_item_id": str(current["work_item_id"]),
        "task_plan_id": str(current["task_plan_id"]),
        "campaign_digest": str(current["campaign_digest"]),
        "outcome_status": str(current["outcome_status"]),
        "outcome_evidence_digest": str(current["outcome_evidence_digest"]),
        "outcome_digest": str(current["outcome_digest"]),
        "completed_at": current["completed_at"].isoformat(),
        **expected_bindings,
        "configured_model_count": int(current["configured_model_count"]),
        "canonical_target_count": int(current["member_count"]),
        "eligible_model_count": int(current["eligible_model_count"]),
        "audio_excluded_count": int(current["audio_excluded_count"]),
        "health_result_count": int(current["health_call_budget"]),
        "tested_call_budget": int(current["tested_call_budget"]),
        "provider_call_budget": int(current["provider_call_budget"]),
        "actual_tested_call_count": cumulative_tested_calls,
        "actual_provider_call_count": cumulative_provider_calls,
        "current_actual_tested_call_count": int(current["actual_tested_call_count"]),
        "current_actual_provider_call_count": int(current["actual_provider_call_count"]),
        "qualified_model_count": int(current["passed_count"]),
        "disqualified_model_count": int(current["failed_count"]),
        "continuation_provenance_digest": (
            None
            if current["continuation_provenance_digest"] is None
            else str(current["continuation_provenance_digest"])
        ),
        "compatibility_evidence_digest": (
            None
            if current["compatibility_evidence_digest"] is None
            else str(current["compatibility_evidence_digest"])
        ),
        "chain": chain,
        "calls": sorted(calls, key=lambda row: row["call_id"]),
        "members": sorted(members, key=lambda row: row["model_id"]),
        "runtime": runtime_evidence,
        "qualification_set_digest": qualification_set_digest,
        "runtime_evidence_digest": runtime_evidence_digest,
        "verifier": {
            "verified": True,
            "identity": "release-readiness-canonical-db/v3",
            "provenance_digest": str(current["verifier_provenance_digest"]),
            "evidence_digest": verifier_digest,
        },
        "raw_prompt_values_reported": 0,
        "raw_response_values_reported": 0,
        "endpoint_values_reported": 0,
        "secret_values_reported": 0,
        "grants_authority": False,
    }
