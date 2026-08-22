"""Global DoD degerlendirmesi, SBOM ve release hazirlik raporu.

Bu betik **iddia uretmez, kanit arar**. Bir kriter ancak `kalite/GLOBAL_DOD.yaml`
icinde `passed` isaretliyse ve gerekli kanit turleri gercekten mevcutsa kapali
sayilir. Eksik kanit gorunur kalir.

Kullanim:

    python scripts/surum_hazirligi.py            # metin ozet
    python scripts/surum_hazirligi.py --json     # makine okunur
    python scripts/surum_hazirligi.py --yaz      # GLOBAL_DOD_DURUM.md uretir
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.metadata as metadata
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOD_PATH = ROOT / "kalite" / "GLOBAL_DOD.yaml"
REPORT_PATH = ROOT / "GLOBAL_DOD_DURUM.md"

#: Kanit dizini surum kontrolunde degildir; varsa okunur.
EVIDENCE_DIRS = (ROOT / ".zekam" / "phases",)
DEFAULT_PROVIDER_ACCEPTANCE_PATH = ROOT / ".zekam" / "evidence" / "ZEKAM-DOD-025-live.json"
PROVIDER_ACCEPTANCE_PATH = DEFAULT_PROVIDER_ACCEPTANCE_PATH
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _build_canonical_provider_acceptance_v3(campaign_id: UUID) -> dict[str, Any]:
    """Build sanitized continuation-aware evidence directly from canonical state."""

    from zekam.application.governance import DEFAULT_POLICY_NAME, GovernanceService
    from zekam.application.opencode_benchmark_campaign import BENCHMARK_SECRET_REF_NAME
    from zekam.application.provider_acceptance_evidence import (
        build_provider_acceptance_evidence,
    )
    from zekam.domain.canonical import digest
    from zekam.domain.realm import DEFAULT_REALM_SLUG
    from zekam.infrastructure.postgres.model_campaign_repository import ModelCampaignRepository
    from zekam.interfaces.cli.model_campaign import (
        _continuation_runtime,
        _domain_campaign,
        _load_manifest,
        _source_revision,
    )
    from zekam.interfaces.cli.session import RealmSession

    discovery, manifest = _load_manifest(config_file=None, scope_file=None)
    _, current_source_revision = _source_revision()
    with RealmSession(None, DEFAULT_REALM_SLUG) as context:
        policy = GovernanceService(context.connection, context.realm).policies.current(
            DEFAULT_POLICY_NAME
        )
        if policy is None:
            raise RuntimeError("current policy missing")
        with context.connection.cursor() as cursor:
            cursor.execute(
                "select parent_campaign_id, work_item_id, task_plan_id, revision"
                " from models.opencode_benchmark_campaign where realm_id=%s and id=%s",
                (context.realm.id, campaign_id),
            )
            row = cursor.fetchone()
        if row is None or row[0] is None:
            raise RuntimeError("continuation campaign missing")
        parent_campaign_id = UUID(str(row[0]))
        work_id = UUID(str(row[1]))
        plan_id = UUID(str(row[2]))
        revision = int(row[3])
        repository = ModelCampaignRepository(context.connection, context.realm.id)
        continuation_runtime = _continuation_runtime(
            context.connection,
            repository,
            parent_campaign_id=parent_campaign_id,
            manifest=manifest,
            work_id=work_id,
            revision=revision,
            current_source_revision=current_source_revision,
            current_policy_digest=policy.policy_digest,
        )
        expected_campaign = _domain_campaign(
            discovery,
            manifest,
            revision=revision,
            work_id=work_id,
            task_plan_id=plan_id,
            source_revision=current_source_revision,
            policy_digest=policy.policy_digest,
            continuation=continuation_runtime.continuation,
        )
        with context.connection.cursor() as cursor:
            cursor.execute(
                "select task_plan_id, revision, source_revision"
                " from models.opencode_benchmark_campaign where realm_id=%s and id=%s",
                (context.realm.id, parent_campaign_id),
            )
            parent_row = cursor.fetchone()
        if parent_row is None:
            raise RuntimeError("parent campaign missing")
        expected_parent_campaign = _domain_campaign(
            discovery,
            manifest,
            revision=int(parent_row[1]),
            work_id=work_id,
            task_plan_id=UUID(str(parent_row[0])),
            source_revision=str(parent_row[2]),
            policy_digest=policy.policy_digest,
        )
        return build_provider_acceptance_evidence(
            context.connection,
            realm_id=context.realm.id,
            campaign_id=campaign_id,
            expected_source_revision=current_source_revision,
            expected_bindings={
                "source_revision": current_source_revision,
                "source_digest": manifest.manifest_digest,
                "catalog_digest": digest(discovery.catalog.sanitized()),
                "endpoint_identity_digest": discovery.catalog.endpoint_identity_digest,
                "inventory_digest": discovery.inventory_digest,
                "policy_digest": policy.policy_digest,
                "fixture_registry_digest": discovery.fixture_registry_digest,
                "verifier_provenance_digest": discovery.verifier_provenance_digest,
            },
            expected_campaign=expected_campaign,
            expected_parent_campaign=expected_parent_campaign,
            expected_calls={item.call_id: item for item in manifest.calls},
            expected_current_calls={
                item.call_id: item for item in continuation_runtime.active_calls
            },
            expected_continuation=continuation_runtime.continuation,
            expected_secret_name=BENCHMARK_SECRET_REF_NAME,
            expected_secret_locator=manifest.credential_locator,
        )


def _canonical_provider_acceptance(document: dict[str, Any]) -> list[str]:
    """Dosya iddiasini current source ve kanonik PostgreSQL zincirinden yeniden turetir.

    Bu kontrol salt okunurdur. Evidence dosyasi tek basina authority veya kabul kaniti
    degildir; exact campaign/runtime/provider/trial/verifier kayitlariyla eslesmelidir.
    """

    if document.get("schema") == "zekam-opencode-benchmark-campaign-acceptance/v3":
        campaign_id = _uuid(document.get("campaign_id"))
        if campaign_id is None:
            return ["canonical-evidence-id-invalid"]
        try:
            expected = _build_canonical_provider_acceptance_v3(campaign_id)
        except Exception:
            return ["canonical-campaign-verification-unavailable"]
        return [] if document == expected else ["canonical-continuation-evidence-mismatch"]

    required_ids = (
        "campaign_id",
        "outcome_id",
        "work_item_id",
        "task_plan_id",
    )
    parsed = {key: _uuid(document.get(key)) for key in required_ids}
    runtime_document = document.get("runtime")
    if not isinstance(runtime_document, dict):
        return ["canonical-runtime-evidence-invalid"]
    for key in (
        "job_id",
        "attempt_id",
        "campaign_claim_id",
        "campaign_receipt_id",
        "checkpoint_record_id",
    ):
        parsed[key] = _uuid(runtime_document.get(key))
    if any(value is None for value in parsed.values()):
        return ["canonical-evidence-id-invalid"]
    ids: dict[str, UUID] = {key: value for key, value in parsed.items() if value is not None}

    try:
        from zekam.application.governance import DEFAULT_POLICY_NAME, GovernanceService
        from zekam.domain.canonical import digest
        from zekam.domain.model_campaign import (
            CampaignMemberResult,
            CampaignMemberResultStage,
            CampaignMemberResultStatus,
            CampaignOutcome,
            CampaignOutcomeStatus,
        )
        from zekam.domain.realm import DEFAULT_REALM_SLUG
        from zekam.interfaces.cli.model_campaign import (
            _assert_terminal_runtime_evidence,
            _domain_campaign,
            _load_manifest,
            _source_revision,
        )
        from zekam.interfaces.cli.session import RealmSession

        discovery, manifest = _load_manifest(config_file=None, scope_file=None)
        _, current_source_revision = _source_revision()
        with RealmSession(None, DEFAULT_REALM_SLUG) as context:
            policy = GovernanceService(context.connection, context.realm).policies.current(
                DEFAULT_POLICY_NAME
            )
            if policy is None:
                return ["canonical-current-policy-missing"]
            with context.connection.cursor() as cursor:
                cursor.execute("select project_id from runtime.job where id=%s", (ids["job_id"],))
                project_id = UUID(str(cursor.fetchone()[0]))
                cursor.execute(
                    "select c.work_item_id, c.task_plan_id, c.revision, c.source_revision,"
                    " c.source_digest, c.catalog_digest, c.endpoint_identity_digest,"
                    " c.inventory_digest, c.policy_digest, c.fixture_registry_digest,"
                    " c.verifier_identity, c.verifier_provenance_digest, c.campaign_digest,"
                    " c.configured_model_count, c.member_count, c.eligible_model_count,"
                    " c.audio_excluded_count, c.health_call_budget, c.tested_call_budget,"
                    " c.provider_call_budget, o.status, o.passed_count, o.failed_count,"
                    " o.recovery_required_count, o.actual_tested_call_count,"
                    " o.actual_provider_call_count, o.evidence_digest, o.outcome_digest,"
                    " o.completed_at"
                    " from models.opencode_benchmark_campaign c"
                    " join models.opencode_benchmark_campaign_outcome o"
                    "   on o.realm_id = c.realm_id and o.campaign_id = c.id"
                    " where c.id = %s and o.id = %s and c.campaign_key = 'opencode-aihub'",
                    (ids["campaign_id"], ids["outcome_id"]),
                )
                campaign_row = cursor.fetchone()
            if campaign_row is None:
                return ["canonical-campaign-outcome-missing"]

            work_id = UUID(str(campaign_row[0]))
            plan_id = UUID(str(campaign_row[1]))
            revision = int(campaign_row[2])
            completed_at = campaign_row[28]
            now = dt.datetime.now(dt.UTC)
            if completed_at < now - dt.timedelta(days=7) or completed_at > now + dt.timedelta(
                minutes=5
            ):
                return ["canonical-campaign-evidence-stale"]
            if document.get("completed_at") != completed_at.isoformat():
                return ["canonical-campaign-completed-at-mismatch"]
            with context.connection.cursor() as cursor:
                cursor.execute(
                    "select count(*) from models.opencode_benchmark_campaign"
                    " where campaign_key='opencode-aihub' and revision > %s",
                    (revision,),
                )
                newer_campaigns = int(cursor.fetchone()[0])
                cursor.execute(
                    "select count(*) from work.task_plan"
                    " where work_item_id=%s and revision >"
                    " (select revision from work.task_plan where id=%s)",
                    (work_id, plan_id),
                )
                newer_plans = int(cursor.fetchone()[0])
            if newer_campaigns or newer_plans:
                return ["canonical-campaign-or-plan-not-current"]
            expected_campaign = _domain_campaign(
                discovery,
                manifest,
                revision=revision,
                work_id=work_id,
                task_plan_id=plan_id,
                source_revision=current_source_revision,
                policy_digest=policy.policy_digest,
            )
            expected_bindings = {
                "source_revision": current_source_revision,
                "source_digest": manifest.manifest_digest,
                "catalog_digest": digest(discovery.catalog.sanitized()),
                "endpoint_identity_digest": discovery.catalog.endpoint_identity_digest,
                "inventory_digest": discovery.inventory_digest,
                "policy_digest": policy.policy_digest,
                "fixture_registry_digest": discovery.fixture_registry_digest,
                "verifier_identity": discovery.scope.verifier.execution_identity,
                "verifier_provenance_digest": discovery.verifier_provenance_digest,
                "campaign_digest": expected_campaign.campaign_digest,
            }
            stored_bindings = dict(
                zip(
                    expected_bindings,
                    (
                        str(campaign_row[3]),
                        str(campaign_row[4]),
                        str(campaign_row[5]),
                        str(campaign_row[6]),
                        str(campaign_row[7]),
                        str(campaign_row[8]),
                        str(campaign_row[9]),
                        str(campaign_row[10]),
                        str(campaign_row[11]),
                        str(campaign_row[12]),
                    ),
                    strict=True,
                )
            )
            if stored_bindings != expected_bindings:
                return ["canonical-campaign-current-binding-drift"]
            if ids["work_item_id"] != work_id or ids["task_plan_id"] != plan_id:
                return ["canonical-campaign-work-plan-mismatch"]
            for key, expected_value in expected_bindings.items():
                if key != "verifier_identity" and document.get(key) != expected_value:
                    return [f"canonical-{key}-mismatch"]

            counts = {
                "configured_model_count": int(campaign_row[13]),
                "canonical_target_count": int(campaign_row[14]),
                "eligible_model_count": int(campaign_row[15]),
                "audio_excluded_count": int(campaign_row[16]),
                "health_result_count": int(campaign_row[17]),
                "tested_call_budget": int(campaign_row[18]),
                "provider_call_budget": int(campaign_row[19]),
                "qualified_model_count": int(campaign_row[21]),
                "disqualified_model_count": int(campaign_row[22]),
                "actual_tested_call_count": int(campaign_row[24]),
                "actual_provider_call_count": int(campaign_row[25]),
            }
            if any(document.get(key) != value for key, value in counts.items()):
                return ["canonical-campaign-count-mismatch"]
            outcome_status = str(campaign_row[20])
            if (
                outcome_status
                not in {
                    CampaignOutcomeStatus.PASSED.value,
                    CampaignOutcomeStatus.FAILED.value,
                }
                or document.get("outcome_status") != outcome_status
            ):
                return ["canonical-terminal-outcome-mismatch"]
            if int(campaign_row[23]) != 0:
                return ["canonical-recovery-required-outcome"]
            canonical_outcome = CampaignOutcome(
                status=CampaignOutcomeStatus(outcome_status),
                passed_count=int(campaign_row[21]),
                failed_count=int(campaign_row[22]),
                recovery_required_count=int(campaign_row[23]),
                audio_excluded_count=int(campaign_row[16]),
                actual_tested_call_count=int(campaign_row[24]),
                actual_provider_call_count=int(campaign_row[25]),
                evidence_digest=str(campaign_row[26]),
            )
            if canonical_outcome.outcome_digest != str(campaign_row[27]):
                return ["canonical-outcome-recomputed-digest-mismatch"]
            if document.get("outcome_evidence_digest") != str(campaign_row[26]):
                return ["canonical-outcome-evidence-mismatch"]
            if document.get("outcome_digest") != str(campaign_row[27]):
                return ["canonical-outcome-digest-mismatch"]

            _assert_terminal_runtime_evidence(
                context.connection,
                campaign_id=ids["campaign_id"],
                campaign_digest=str(campaign_row[12]),
                outcome_digest=str(campaign_row[27]),
                outcome_status=outcome_status,
            )

            with context.connection.cursor() as cursor:
                cursor.execute(
                    "select j.id, a.id, ec.id, er.id, cp.id, cp.checkpoint_key,"
                    " j.state, j.max_attempts, a.outcome, er.status,"
                    " cardinality(cp.pending_steps), cp.grants_authority"
                    " from runtime.job j"
                    " join runtime.job_attempt a on a.realm_id=j.realm_id and a.job_id=j.id"
                    " join runtime.effect_claim ec on ec.realm_id=j.realm_id and ec.job_id=j.id"
                    "   and ec.operation='model-campaign-outcome-ledger'"
                    " join runtime.effect_receipt er on er.realm_id=ec.realm_id"
                    "   and er.claim_id=ec.id"
                    " join work.checkpoint cp on cp.realm_id=j.realm_id and cp.job_id=j.id"
                    " where j.work_item_id=%s and j.plan_id=%s and j.step_id='campaign-finalize'"
                    "   and er.result_digest=%s and a.result_digest=%s",
                    (work_id, plan_id, str(campaign_row[27]), str(campaign_row[27])),
                )
                runtime_rows = cursor.fetchall()
            if len(runtime_rows) != 1:
                return ["canonical-runtime-chain-ambiguous"]
            runtime_row = runtime_rows[0]
            runtime_ids = tuple(UUID(str(value)) for value in runtime_row[:5])
            if runtime_ids != (
                ids["job_id"],
                ids["attempt_id"],
                ids["campaign_claim_id"],
                ids["campaign_receipt_id"],
                ids["checkpoint_record_id"],
            ) or runtime_document.get("checkpoint_key") != str(runtime_row[5]):
                return ["canonical-runtime-id-binding-mismatch"]
            if tuple(runtime_row[6:]) != ("completed", 1, "succeeded", "completed", 0, False):
                return ["canonical-runtime-terminal-state-mismatch"]
            with context.connection.cursor() as cursor:
                cursor.execute(
                    "select ec.effect_digest, ec.authorization_digest, ca.authorization_digest,"
                    " ca.work_item_id, ca.plan_id, ca.plan_digest, ca.effect_digest, ca.state,"
                    " ca.allowed_resources, ca.allowed_effects, ca.provider_refs,"
                    " ca.secret_ref_ids,"
                    " ca.scope, ec.resources, ec.claimed_at, ca.consumed_at,"
                    " er.adapter_evidence_digest, er.completed_at"
                    " from runtime.effect_claim ec"
                    " join security.authorization ca on ca.id=ec.authorization_id"
                    " join runtime.effect_receipt er on er.claim_id=ec.id"
                    " where ec.id=%s",
                    (ids["campaign_claim_id"],),
                )
                campaign_authority_row = cursor.fetchone()
            campaign_effect_digest = digest(
                {
                    "campaign_digest": str(campaign_row[12]),
                    "effect": "campaign-outcome-qualification-ledger",
                }
            )
            if (
                campaign_authority_row is None
                or str(campaign_authority_row[0]) != campaign_effect_digest
                or str(campaign_authority_row[1]) != str(campaign_authority_row[2])
                or UUID(str(campaign_authority_row[3])) != work_id
                or UUID(str(campaign_authority_row[4])) != plan_id
                or str(campaign_authority_row[5]) != str(campaign_row[12])
                or str(campaign_authority_row[6]) != campaign_effect_digest
                or str(campaign_authority_row[7]) != "consumed"
                or tuple(campaign_authority_row[8]) != (f"work:{project_id}:{work_id}",)
                or tuple(campaign_authority_row[9]) != ("database-write",)
                or tuple(campaign_authority_row[10])
                or tuple(campaign_authority_row[11])
                or dict(campaign_authority_row[12]).get("data_classifications") != ["public"]
                or list(campaign_authority_row[13])
                != [{"mode": "write", "resource": f"work:{project_id}:{work_id}"}]
                or campaign_authority_row[15] is None
                or not (
                    campaign_authority_row[14]
                    <= campaign_authority_row[15]
                    <= campaign_authority_row[17]
                )
                or str(campaign_authority_row[16]) != str(campaign_row[26])
            ):
                return ["canonical-campaign-db-write-authority-mismatch"]
            with context.connection.cursor() as cursor:
                cursor.execute(
                    "select"
                    " (select count(*) from runtime.claim_without_receipt where job_id=%s),"
                    " (select count(*) from runtime.lease where job_id=%s),"
                    " (select count(*) from runtime.resource_lock where job_id=%s)",
                    (ids["job_id"], ids["job_id"], ids["job_id"]),
                )
                open_runtime = tuple(int(value) for value in cursor.fetchone())
            if open_runtime != (0, 0, 0):
                return ["canonical-runtime-open-state"]

            from zekam.application.opencode_benchmark_campaign import BENCHMARK_SECRET_REF_NAME

            with context.connection.cursor() as cursor:
                cursor.execute(
                    "select id, provider, store_locator, status, allowed_operations, store_backend"
                    " from security.secret_ref where name=%s and status <> 'revoked'"
                    " order by version desc limit 1",
                    (BENCHMARK_SECRET_REF_NAME,),
                )
                secret_row = cursor.fetchone()
            if (
                secret_row is None
                or str(secret_row[1]) != discovery.scope.provider_id
                or str(secret_row[2]) != manifest.credential_locator
                or str(secret_row[3]) not in {"active", "rotating"}
                or set(secret_row[4]) != {"chat-completions", "embeddings", "rerank"}
                or str(secret_row[5]) != "environment"
            ):
                return ["canonical-current-secret-ref-metadata-mismatch"]
            secret_ref_id = UUID(str(secret_row[0]))

            expected_calls = {item.call_id: item for item in manifest.calls}
            with context.connection.cursor() as cursor:
                cursor.execute(
                    "select ec.id, ec.operation, ec.effect_digest, ec.claim_digest, ec.claimed_at,"
                    " ec.authorization_digest, auth.id, auth.plan_digest, auth.effect_digest,"
                    " auth.authorization_digest, auth.work_item_id, auth.plan_id, auth.state,"
                    " auth.consumed_at, auth.allowed_resources, auth.allowed_effects,"
                    " auth.provider_refs, auth.secret_ref_ids, auth.scope,"
                    " er.id, er.status, er.result_digest, er.adapter_evidence_digest,"
                    " er.completed_at"
                    " from runtime.effect_claim ec"
                    " join security.authorization auth on auth.realm_id=ec.realm_id"
                    "   and auth.id=ec.authorization_id"
                    " join runtime.effect_receipt er on er.realm_id=ec.realm_id"
                    "   and er.claim_id=ec.id"
                    " where ec.job_id=%s and ec.operation like 'provider-contract:%%'"
                    " order by ec.operation",
                    (ids["job_id"],),
                )
                call_rows = cursor.fetchall()
            evidence_calls = document.get("calls")
            if not isinstance(evidence_calls, list):
                return ["canonical-provider-call-evidence-invalid"]
            evidence_by_call = {
                row.get("call_id"): row for row in evidence_calls if isinstance(row, dict)
            }
            if len(evidence_by_call) != len(call_rows):
                return ["canonical-provider-call-set-mismatch"]
            normalized_calls: list[dict[str, Any]] = []
            for row in call_rows:
                call_id = str(row[1]).removeprefix("provider-contract:")
                planned = expected_calls.get(call_id)
                evidence = evidence_by_call.get(call_id)
                if planned is None or not isinstance(evidence, dict):
                    return ["canonical-provider-call-unplanned"]
                plan = planned.prepared.plan
                if (
                    str(row[2]) != plan.effect_request.effect_digest
                    or str(row[5]) != str(row[9])
                    or str(row[7]) != plan.authorization_plan_digest
                    or str(row[8]) != plan.effect_request.effect_digest
                    or UUID(str(row[10])) != work_id
                    or UUID(str(row[11])) != plan_id
                    or str(row[12]) != "consumed"
                    or set(row[14]) != {plan.target, plan.call_resource}
                    or tuple(row[15]) != ("provider-call",)
                    or tuple(row[16]) != (plan.provider_ref,)
                    or tuple(row[17]) != (secret_ref_id,)
                    or dict(row[18]).get("data_classifications") != ["public"]
                    or str(row[20]) != "completed"
                    or row[13] is None
                    or not (row[4] <= row[13] <= row[23])
                    or plan.operation not in tuple(secret_row[4])
                ):
                    return ["canonical-provider-call-authority-chain-mismatch"]
                expected_evidence = {
                    "call_id": call_id,
                    "kind": planned.kind.value,
                    "model_id": planned.canonical_model_id,
                    "fixture_digest": planned.fixture_digest,
                    "repetition": planned.repetition,
                    "authorization_id": str(row[6]),
                    "claim_id": str(row[0]),
                    "receipt_id": str(row[19]),
                    "receipt_status": str(row[20]),
                    "response_digest": str(row[21]),
                    "provider_evidence_digest": str(row[22]),
                    "plan_digest": str(row[7]),
                    "effect_digest": str(row[2]),
                }
                if evidence != expected_evidence:
                    return ["canonical-provider-call-evidence-mismatch"]
                normalized_calls.append(expected_evidence)

            with context.connection.cursor() as cursor:
                cursor.execute(
                    "select state, count(*) from security.authorization"
                    " where work_item_id=%s and plan_id=%s group by state",
                    (work_id, plan_id),
                )
                authority_counts = {str(state): int(count) for state, count in cursor.fetchall()}
            if (
                authority_counts.get("issued", 0) != 0
                or authority_counts.get("consumed", 0) != len(call_rows) + 18
                or authority_counts.get("revoked", 0) != 102 - len(call_rows)
                or sum(authority_counts.values()) != 120
            ):
                return ["canonical-terminal-authorization-set-mismatch"]

            with context.connection.cursor() as cursor:
                cursor.execute(
                    "select m.id, m.canonical_model_id, h.id, h.status, h.evidence_digest,"
                    " b.id, b.status, b.evidence_digest, b.actual_tested_call_count,"
                    " mp.id, mp.benchmark_plan_id, b.aggregate_id, q.id, q.action,"
                    " q.evidence_digest, q.event_digest,"
                    " coalesce(b.result_digest, h.result_digest), q.aggregate_id, q.reason_code"
                    " from models.opencode_benchmark_campaign_member m"
                    " join models.opencode_benchmark_campaign_member_result h"
                    "   on h.realm_id=m.realm_id and h.campaign_id=m.campaign_id"
                    "  and h.member_id=m.id and h.stage='health'"
                    " left join models.opencode_benchmark_campaign_member_plan mp"
                    "   on mp.realm_id=m.realm_id and mp.campaign_id=m.campaign_id"
                    "  and mp.member_id=m.id"
                    " left join models.opencode_benchmark_campaign_member_result b"
                    "   on b.realm_id=m.realm_id and b.campaign_id=m.campaign_id"
                    "  and b.member_id=m.id and b.stage='benchmark'"
                    " join models.opencode_model_qualification_event q"
                    "   on q.realm_id=m.realm_id and q.campaign_id=m.campaign_id"
                    "  and q.member_id=m.id"
                    " where m.campaign_id=%s and m.disposition='health-pending'"
                    " order by m.canonical_model_id",
                    (ids["campaign_id"],),
                )
                member_rows = cursor.fetchall()
            evidence_members = document.get("members")
            if not isinstance(evidence_members, list):
                return ["canonical-member-evidence-invalid"]
            evidence_by_member = {
                row.get("member_id"): row for row in evidence_members if isinstance(row, dict)
            }
            if len(member_rows) != 17 or len(evidence_by_member) != 17:
                return ["canonical-member-set-mismatch"]
            normalized_members: list[dict[str, Any]] = []
            normalized_trials: list[dict[str, Any]] = []
            expected_executed_call_ids: set[str] = set()
            member_result_digests: dict[str, str] = {}
            aggregate_ids: dict[str, UUID] = {}
            failed_models: dict[str, str] = {}
            for row in member_rows:
                health_passed = str(row[3]) == "passed"
                benchmark_status = "not-run" if row[6] is None else str(row[6])
                repetitions = 0 if row[8] is None else int(row[8])
                qualification = str(row[13])
                expected_qualification = (
                    "qualified"
                    if health_passed and benchmark_status == "passed"
                    else "disqualified"
                )
                if (
                    qualification != expected_qualification
                    or (health_passed and (row[5] is None or repetitions != 5))
                    or (not health_passed and row[5] is not None)
                ):
                    return ["canonical-member-health-benchmark-binding-mismatch"]
                with context.connection.cursor() as cursor:
                    cursor.execute(
                        "select mc.id, mr.id, ma.id, mc.effect_digest,"
                        " mc.authorization_digest, ma.authorization_digest, ma.effect_digest,"
                        " ma.plan_digest, ma.state, ma.work_item_id, ma.plan_id,"
                        " ma.allowed_resources, ma.allowed_effects, ma.provider_refs,"
                        " ma.secret_ref_ids, ma.scope, bp.id, bp.plan_digest, bs.suite_digest,"
                        " mr.status, mr.result_digest, mc.resources, mc.claimed_at,"
                        " ma.consumed_at, mr.adapter_evidence_digest, mr.completed_at"
                        " from runtime.effect_claim mc"
                        " join runtime.effect_receipt mr on mr.claim_id=mc.id"
                        " join security.authorization ma on ma.id=mc.authorization_id"
                        " join models.benchmark_plan bp on bp.plan_digest=ma.plan_digest"
                        " join models.benchmark_suite bs on bs.id=bp.suite_id"
                        " where mc.job_id=%s and mc.attempt_id=%s"
                        "   and mc.operation='model-campaign-member-ledger'"
                        "   and mr.result_digest=%s and bp.model_id=%s"
                        "   and bp.repetitions=5 and bp.inventory_digest=%s"
                        "   and bp.policy_digest=%s and bp.fixture_registry_digest=%s",
                        (
                            ids["job_id"],
                            ids["attempt_id"],
                            str(row[16]),
                            str(row[1]),
                            discovery.inventory_digest,
                            policy.policy_digest,
                            discovery.fixture_registry_digest,
                        ),
                    )
                    member_claim_rows = cursor.fetchall()
                if len(member_claim_rows) != 1:
                    return ["canonical-member-ledger-chain-mismatch"]
                member_claim = member_claim_rows[0]
                member_effect_digest = digest(
                    {
                        "campaign_digest": str(campaign_row[12]),
                        "member_id": UUID(str(row[0])),
                        "benchmark_plan_digest": str(member_claim[17]),
                        "effect": "benchmark-ledger-write",
                    }
                )
                expected_member_resources = {
                    f"model-benchmark:{row[1]}:{str(member_claim[18]).removeprefix('sha256:')}",
                    f"model-benchmark:{row[1]}:campaign-ledger",
                }
                if (
                    str(member_claim[3]) != member_effect_digest
                    or str(member_claim[4]) != str(member_claim[5])
                    or str(member_claim[6]) != member_effect_digest
                    or str(member_claim[7]) != str(member_claim[17])
                    or str(member_claim[8]) != "consumed"
                    or UUID(str(member_claim[9])) != work_id
                    or UUID(str(member_claim[10])) != plan_id
                    or set(member_claim[11]) != expected_member_resources
                    or tuple(member_claim[12]) != ("database-write",)
                    or tuple(member_claim[13])
                    or tuple(member_claim[14])
                    or dict(member_claim[15]).get("data_classifications") != ["public"]
                    or str(member_claim[19]) != "completed"
                    or str(member_claim[20]) != str(row[16])
                    or list(member_claim[21])
                    != [
                        {
                            "mode": "write",
                            "resource": f"model-benchmark:{row[1]}:campaign-ledger",
                        }
                    ]
                    or member_claim[23] is None
                    or not member_claim[22] <= member_claim[23] <= member_claim[25]
                    or str(member_claim[24]) != str(row[4] if row[7] is None else row[7])
                ):
                    return ["canonical-member-ledger-chain-mismatch"]
                member_calls = tuple(
                    item for item in manifest.calls if item.canonical_model_id == str(row[1])
                )
                expected_executed_call_ids.update(
                    item.call_id
                    for item in member_calls
                    if item.kind.value == "health" or health_passed
                )
                with context.connection.cursor() as cursor:
                    cursor.execute(
                        "select stage, status, evidence_digest, result_digest, failure_category,"
                        " actual_tested_call_count, actual_provider_call_count, aggregate_id"
                        " from models.opencode_benchmark_campaign_member_result"
                        " where campaign_id=%s and member_id=%s order by stage",
                        (ids["campaign_id"], row[0]),
                    )
                    result_rows = cursor.fetchall()
                if len(result_rows) != (2 if health_passed else 1):
                    return ["canonical-member-result-set-mismatch"]
                reconstructed_results: dict[str, CampaignMemberResult] = {}
                for result_row in result_rows:
                    result = CampaignMemberResult(
                        stage=CampaignMemberResultStage(str(result_row[0])),
                        status=CampaignMemberResultStatus(str(result_row[1])),
                        evidence_digest=str(result_row[2]),
                        actual_tested_call_count=int(result_row[5]),
                        actual_provider_call_count=int(result_row[6]),
                        aggregate_id=(None if result_row[7] is None else UUID(str(result_row[7]))),
                        failure_category=(None if result_row[4] is None else str(result_row[4])),
                    )
                    if result.result_digest != str(result_row[3]):
                        return ["canonical-member-result-recomputed-digest-mismatch"]
                    reconstructed_results[result.stage.value] = result
                terminal_result = reconstructed_results.get(
                    "benchmark", reconstructed_results["health"]
                )
                member_result_digests[str(row[1])] = terminal_result.result_digest
                if qualification == "qualified":
                    if terminal_result.aggregate_id is None:
                        return ["canonical-qualified-member-aggregate-missing"]
                    aggregate_ids[str(row[1])] = terminal_result.aggregate_id
                else:
                    if terminal_result.failure_category is None:
                        return ["canonical-disqualified-member-failure-missing"]
                    failed_models[str(row[1])] = terminal_result.failure_category
                if row[10] is not None:
                    with context.connection.cursor() as cursor:
                        cursor.execute(
                            "select t.id, t.fixture_digest, t.repetition, t.response_digest,"
                            " t.evidence_digest, t.tested_claim_id, t.verifier_claim_id,"
                            " t.verifier_evidence_digest, t.verifier_provenance_digest,"
                            " tc.job_id, tc.attempt_id, tc.authorization_id, tr.id, tr.status,"
                            " tr.result_digest, vc.job_id, vc.attempt_id, vc.authorization_id,"
                            " vr.id, vr.status, vr.result_digest, v.id, v.tested_response_digest,"
                            " v.approved, v.evidence_digest, v.execution_identity,"
                            " v.verifier_model_id, ma.state, ma.work_item_id, ma.plan_id,"
                            " tc.authorization_digest, vc.authorization_digest,"
                            " ma.authorization_digest, tc.operation, vc.operation"
                            " from models.benchmark_trial t"
                            " join models.benchmark_verifier_result v"
                            "   on v.realm_id=t.realm_id and v.claim_id=t.verifier_claim_id"
                            " join runtime.effect_claim tc on tc.id=t.tested_claim_id"
                            " join runtime.effect_claim vc on vc.id=t.verifier_claim_id"
                            " join runtime.effect_receipt tr on tr.claim_id=t.tested_claim_id"
                            " join runtime.effect_receipt vr on vr.claim_id=t.verifier_claim_id"
                            " join security.authorization ma on ma.id=tc.authorization_id"
                            "   and ma.id=vc.authorization_id"
                            " where t.plan_id=%s",
                            (row[10],),
                        )
                        trial_rows = cursor.fetchall()
                    if len(trial_rows) != 5 or {int(item[2]) for item in trial_rows} != set(
                        range(1, 6)
                    ):
                        return ["canonical-five-trial-independent-verifier-mismatch"]
                    benchmark_calls = {
                        (item.fixture_digest, item.repetition): item
                        for item in member_calls
                        if item.kind.value == "benchmark"
                    }
                    for trial in trial_rows:
                        planned_call = benchmark_calls.get((str(trial[1]), int(trial[2])))
                        if planned_call is None:
                            return ["canonical-trial-provider-call-mismatch"]
                        provider_evidence = evidence_by_call.get(planned_call.call_id)
                        if not isinstance(provider_evidence, dict):
                            return ["canonical-trial-provider-call-mismatch"]
                        final_trial_digest = digest(
                            {
                                "tested": str(trial[14]),
                                "verifier": str(trial[24]),
                                "provider_receipt": UUID(str(provider_evidence["receipt_id"])),
                            }
                        )
                        if (
                            str(trial[4]) != final_trial_digest
                            or str(trial[8]) != discovery.verifier_provenance_digest
                            or UUID(str(trial[9])) != ids["job_id"]
                            or UUID(str(trial[10])) != ids["attempt_id"]
                            or UUID(str(trial[15])) != ids["job_id"]
                            or UUID(str(trial[16])) != ids["attempt_id"]
                            or UUID(str(trial[11])) != UUID(str(trial[17]))
                            or str(trial[13]) != "completed"
                            or str(trial[19]) != "completed"
                            or str(trial[20]) != str(trial[24])
                            or str(trial[22]) != str(trial[3])
                            or bool(trial[23]) is not True
                            or str(trial[24]) != str(trial[7])
                            or str(trial[25]) != discovery.scope.verifier.execution_identity
                            or str(trial[26]) == str(row[1])
                            or str(trial[27]) != "consumed"
                            or UUID(str(trial[28])) != work_id
                            or UUID(str(trial[29])) != plan_id
                            or str(trial[30]) != str(trial[32])
                            or str(trial[31]) != str(trial[32])
                            or str(trial[33]) != "model-benchmark-tested"
                            or str(trial[34]) != "model-benchmark-verifier"
                        ):
                            return ["canonical-trial-independent-verifier-chain-mismatch"]
                        normalized_trials.append(
                            {
                                "trial_id": str(trial[0]),
                                "call_id": planned_call.call_id,
                                "tested_claim_id": str(trial[5]),
                                "tested_receipt_id": str(trial[12]),
                                "verifier_claim_id": str(trial[6]),
                                "verifier_receipt_id": str(trial[18]),
                                "verifier_result_id": str(trial[21]),
                                "response_digest": str(trial[3]),
                                "trial_evidence_digest": str(trial[4]),
                                "verifier_evidence_digest": str(trial[24]),
                            }
                        )
                expected_member = {
                    "member_id": str(row[0]),
                    "model_id": str(row[1]),
                    "health_result_id": str(row[2]),
                    "health_status": str(row[3]),
                    "health_evidence_digest": str(row[4]),
                    "benchmark_result_id": None if row[5] is None else str(row[5]),
                    "benchmark_status": benchmark_status,
                    "result_evidence_digest": str(row[4] if row[7] is None else row[7]),
                    "repetitions": repetitions,
                    "qualification_id": str(row[12]),
                    "qualification": qualification,
                    "qualification_evidence_digest": str(row[14]),
                    "aggregate_id": None if row[11] is None else str(row[11]),
                    "member_authorization_id": str(member_claim[2]),
                    "member_claim_id": str(member_claim[0]),
                    "member_receipt_id": str(member_claim[1]),
                }
                if evidence_by_member.get(str(row[0])) != expected_member:
                    return ["canonical-member-evidence-mismatch"]
                recomputed_qualification_evidence = digest(
                    {
                        "campaign_outcome": str(campaign_row[27]),
                        "model_id": str(row[1]),
                        "member_result": member_result_digests[str(row[1])],
                    }
                )
                if recomputed_qualification_evidence != str(row[14]):
                    return ["canonical-qualification-evidence-recomputed-digest-mismatch"]
                recomputed_event_digest = digest(
                    {
                        "action": qualification,
                        "model_id": str(row[1]),
                        "outcome_id": ids["outcome_id"],
                        "evidence_digest": str(row[14]),
                        "aggregate_id": None if row[17] is None else UUID(str(row[17])),
                        "reason_code": None if row[18] is None else str(row[18]),
                    }
                )
                if recomputed_event_digest != str(row[15]):
                    return ["canonical-qualification-recomputed-digest-mismatch"]
                normalized_members.append(
                    expected_member | {"event_digest": recomputed_event_digest}
                )

            if set(evidence_by_call) != expected_executed_call_ids:
                return ["canonical-health-gated-provider-call-set-mismatch"]
            call_evidence = {
                call_id: str(evidence["provider_evidence_digest"])
                for call_id, evidence in evidence_by_call.items()
            }
            for planned_call in manifest.calls:
                if planned_call.call_id not in call_evidence:
                    call_evidence[planned_call.call_id] = digest(
                        {
                            "status": "not-run-health-failed",
                            "model_id": planned_call.canonical_model_id,
                            "call_id": planned_call.call_id,
                        }
                    )
            recomputed_outcome_evidence = digest(
                {
                    "campaign_digest": str(campaign_row[12]),
                    "member_results": member_result_digests,
                    "aggregate_ids": aggregate_ids,
                    "failed_models": failed_models,
                    "provider_receipts": call_evidence,
                }
            )
            if recomputed_outcome_evidence != str(campaign_row[26]):
                return ["canonical-outcome-evidence-recomputed-digest-mismatch"]

            qualification_set_digest = digest(
                sorted(item["event_digest"] for item in normalized_members)
            )
            runtime_evidence_digest = digest(
                {
                    "campaign_id": ids["campaign_id"],
                    "outcome_id": ids["outcome_id"],
                    "job_id": ids["job_id"],
                    "attempt_id": ids["attempt_id"],
                    "claim_id": ids["campaign_claim_id"],
                    "receipt_id": ids["campaign_receipt_id"],
                    "checkpoint_id": ids["checkpoint_record_id"],
                    "outcome_digest": str(campaign_row[27]),
                }
            )
            canonical_verifier_digest = digest(
                {
                    "schema": "zekam-opencode-benchmark-campaign-acceptance/v2",
                    "campaign_digest": str(campaign_row[12]),
                    "outcome_digest": str(campaign_row[27]),
                    "qualification_set_digest": qualification_set_digest,
                    "runtime_evidence_digest": runtime_evidence_digest,
                    "calls": sorted(normalized_calls, key=lambda item: item["call_id"]),
                    "members": sorted(normalized_members, key=lambda item: item["member_id"]),
                    "trials": sorted(normalized_trials, key=lambda item: item["trial_id"]),
                    "verifier_identity": discovery.scope.verifier.execution_identity,
                    "verifier_provenance_digest": discovery.verifier_provenance_digest,
                }
            )
            if document.get("qualification_set_digest") != qualification_set_digest:
                return ["canonical-qualification-set-digest-mismatch"]
            if document.get("runtime_evidence_digest") != runtime_evidence_digest:
                return ["canonical-runtime-evidence-digest-mismatch"]
            verifier = document.get("verifier")
            if not isinstance(verifier, dict) or verifier != {
                "verified": True,
                "identity": "release-readiness-canonical-db/v2",
                "tested_model_verifier_identity": discovery.scope.verifier.execution_identity,
                "provenance_digest": discovery.verifier_provenance_digest,
                "evidence_digest": canonical_verifier_digest,
            }:
                return ["canonical-independent-verifier-evidence-mismatch"]
    except Exception:
        # Endpoint, secret veya DB hata ayrintisini release raporuna sizdirma.
        return ["canonical-campaign-verification-unavailable"]
    return []


def provider_acceptance_gate() -> dict[str, Any]:
    """ZEKAM-DOD-025'i verified OpenCode/AIHub campaign kaniti olmadan kapatmaz."""

    reasons: list[str] = []
    evidence_path = PROVIDER_ACCEPTANCE_PATH if PROVIDER_ACCEPTANCE_PATH.is_file() else None
    if evidence_path is None:
        return {"passed": False, "reasons": ["opencode-aihub-campaign-evidence-missing"]}
    try:
        document = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"passed": False, "reasons": ["live-evidence-invalid"]}
    if not isinstance(document, dict):
        return {"passed": False, "reasons": ["live-evidence-object-required"]}
    if document.get("schema") not in {
        "zekam-opencode-benchmark-campaign-acceptance/v2",
        "zekam-opencode-benchmark-campaign-acceptance/v3",
    }:
        reasons.append("schema-mismatch")
    if document.get("status") != "verified":
        reasons.append("acceptance-status-not-verified")
    if document.get("campaign_key") != "opencode-aihub":
        reasons.append("campaign-key-mismatch")
    expected_counts = {
        "configured_model_count": 17,
        "canonical_target_count": 18,
        "audio_excluded_count": 1,
        "eligible_model_count": 17,
        "health_result_count": 17,
        "provider_call_budget": 102,
        "tested_call_budget": 85,
    }
    for key, expected in expected_counts.items():
        if document.get(key) != expected:
            reasons.append(f"{key}-mismatch")
    actual_calls = document.get("actual_provider_call_count")
    if isinstance(actual_calls, bool) or not isinstance(actual_calls, int):
        reasons.append("actual-provider-call-count-invalid")
        actual_calls = 0
    elif not 17 <= actual_calls <= 102:
        reasons.append("actual-provider-call-count-out-of-budget")
    if document.get("outcome_status") not in {"passed", "failed"}:
        reasons.append("terminal-outcome-required")
    calls = document.get("calls")
    if not isinstance(calls, list) or len(calls) != actual_calls:
        reasons.append("provider-call-record-count-mismatch")
        calls = []
    for key in ("call_id", "authorization_id", "claim_id", "receipt_id"):
        values = [row.get(key) for row in calls if isinstance(row, dict)]
        if (
            len(values) != actual_calls
            or len(set(values)) != actual_calls
            or any(not value for value in values)
        ):
            reasons.append(f"distinct-{key}-required")
    for row in calls:
        if not isinstance(row, dict):
            reasons.append("call-record-invalid")
            continue
        if row.get("receipt_status") != "completed":
            reasons.append("terminal-completed-receipt-required")
        for key in ("response_digest", "provider_evidence_digest", "plan_digest"):
            if not _DIGEST.fullmatch(str(row.get(key, ""))):
                reasons.append(f"{key}-invalid")
    members = document.get("members")
    if not isinstance(members, list) or len(members) != 17:
        reasons.append("exact-seventeen-member-results-required")
        members = []
    model_ids = [row.get("model_id") for row in members if isinstance(row, dict)]
    if len(model_ids) != 17 or len(set(model_ids)) != 17 or any(not value for value in model_ids):
        reasons.append("distinct-member-model-id-required")
    qualified = 0
    disqualified = 0
    health_passed = 0
    for row in members:
        if not isinstance(row, dict):
            reasons.append("member-record-invalid")
            continue
        health = row.get("health_status")
        benchmark = row.get("benchmark_status")
        qualification = row.get("qualification")
        repetitions = row.get("repetitions")
        valid = (health == "passed" and benchmark in {"passed", "failed"} and repetitions == 5) or (
            health == "failed" and benchmark == "not-run" and repetitions == 0
        )
        if not valid:
            reasons.append("member-health-benchmark-state-invalid")
        expected_qualification = "qualified" if health == benchmark == "passed" else "disqualified"
        if qualification != expected_qualification:
            reasons.append("member-qualification-mismatch")
        health_passed += health == "passed"
        qualified += qualification == "qualified"
        disqualified += qualification == "disqualified"
        for key in ("health_evidence_digest", "result_evidence_digest"):
            if not _DIGEST.fullmatch(str(row.get(key, ""))):
                reasons.append(f"member-{key}-invalid")
    if qualified < 1 or qualified + disqualified != 17:
        reasons.append("qualified-routing-coverage-invalid")
    if document.get("qualified_model_count") != qualified:
        reasons.append("qualified-model-count-mismatch")
    if document.get("disqualified_model_count") != disqualified:
        reasons.append("disqualified-model-count-mismatch")
    if actual_calls != 17 + 5 * health_passed:
        reasons.append("health-gated-call-count-mismatch")
    if (document.get("outcome_status") == "passed") != (disqualified == 0):
        reasons.append("outcome-qualification-count-mismatch")
    runtime = document.get("runtime", {})
    if not isinstance(runtime, dict) or any(
        runtime.get(key) != value
        for key, value in {
            "job_state": "completed",
            "attempt_outcome": "succeeded",
            "receiptless_claim_count": 0,
            "open_lease_count": 0,
            "open_resource_lock_count": 0,
            "checkpoint_complete": True,
        }.items()
    ):
        reasons.append("runtime-terminal-chain-invalid")
    for key in (
        "campaign_digest",
        "outcome_digest",
        "qualification_set_digest",
        "runtime_evidence_digest",
    ):
        if not _DIGEST.fullmatch(str(document.get(key, ""))):
            reasons.append(f"{key}-invalid")
    verifier = document.get("verifier", {})
    if (
        not isinstance(verifier, dict)
        or verifier.get("verified") is not True
        or not _DIGEST.fullmatch(str(verifier.get("evidence_digest", "")))
    ):
        reasons.append("independent-verifier-evidence-required")
    if not reasons:
        reasons.extend(_canonical_provider_acceptance(document))
    return {"passed": not reasons, "reasons": sorted(set(reasons))}


def load_criteria() -> list[dict[str, Any]]:
    document = yaml.safe_load(DOD_PATH.read_text(encoding="utf-8"))
    criteria = document.get("criteria", [])
    if not isinstance(criteria, list):
        raise SystemExit("GLOBAL_DOD.yaml criteria listesi bekleniyor")
    return criteria


def phase_evidence() -> list[dict[str, Any]]:
    """Uretilmis faz kanitlarini okur. Dizin yoksa bos liste doner."""

    found: dict[str, dict[str, Any]] = {}
    for evidence_dir in reversed(EVIDENCE_DIRS):
        if not evidence_dir.is_dir():
            continue
        for path in sorted(evidence_dir.glob("*-kanit.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            phase = str(document.get("phase", path.stem))
            found[phase] = document
    return [found[key] for key in sorted(found)]


def build_sbom() -> list[dict[str, str | None]]:
    """Kurulu dagitimlardan malzeme listesi uretir."""

    entries: dict[str, dict[str, str | None]] = {}
    for distribution in metadata.distributions():
        name = distribution.metadata["Name"]
        if not name:
            continue
        entries[name.lower()] = {
            "name": name,
            "version": distribution.version,
            "license": distribution.metadata.get("License") or None,
        }
    return [entries[key] for key in sorted(entries)]


def count_tests() -> int:
    """Toplanabilen test sayisi. Calistirma yapmaz."""

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    for line in reversed(completed.stdout.splitlines()):
        if "test" in line and "collected" in line:
            return int(line.split()[0])
        if line.strip().endswith("tests collected"):
            return int(line.split()[0])
    return sum(1 for line in completed.stdout.splitlines() if "::" in line)


def migration_count() -> int:
    return len(list((ROOT / "migrations").glob("[0-9]*.sql"))) - len(
        list((ROOT / "migrations").glob("*.down.sql"))
    )


def assess() -> dict[str, Any]:
    criteria = load_criteria()
    provider_gate = provider_acceptance_gate()
    states: dict[str, int] = {"passed": 0, "pending": 0, "failed": 0, "blocked": 0}
    incomplete: list[dict[str, str]] = []
    for item in criteria:
        state = str(item.get("state", "pending"))
        if item.get("id") == "ZEKAM-DOD-025" and not provider_gate["passed"]:
            state = "pending"
        states[state] = states.get(state, 0) + 1
        if state != "passed":
            incomplete.append(
                {
                    "id": str(item.get("id")),
                    "category": str(item.get("category", "")),
                    "criterion": str(item.get("criterion", "")),
                    "state": state,
                }
            )

    phases = phase_evidence()
    return {
        "assessed_at": dt.datetime.now(dt.UTC).isoformat(),
        "criterion_count": len(criteria),
        "states": states,
        "completion_ratio": round(states["passed"] / len(criteria), 6) if criteria else 0.0,
        "is_complete": states["passed"] == len(criteria),
        "incomplete": incomplete,
        "phase_evidence_count": len(phases),
        "phases_with_passing_gates": [
            item["phase"]
            for item in phases
            if item.get("quality_evidence", {}).get("passed") is True
        ],
        "migration_count": migration_count(),
        "sbom_entry_count": len(build_sbom()),
        "provider_acceptance_gate": provider_gate,
    }


def render(summary: dict[str, Any]) -> str:
    states = summary["states"]
    lines = [
        "# Zekam Global DoD durum raporu",
        "",
        "Bu rapor `scripts/surum_hazirligi.py` tarafindan uretilir ve iddia degil",
        "**olcum** tasir. Bir kriter yalnizca `kalite/GLOBAL_DOD.yaml` icinde",
        "`passed` isaretliyse kapali sayilir.",
        "",
        "## Ozet",
        "",
        "| Alan | Deger |",
        "|---|---|",
        f"| Kriter sayisi | {summary['criterion_count']} |",
        f"| Passed | {states.get('passed', 0)} |",
        f"| Pending | {states.get('pending', 0)} |",
        f"| Failed | {states.get('failed', 0)} |",
        f"| Blocked | {states.get('blocked', 0)} |",
        f"| Tamamlanma orani | {summary['completion_ratio']:.1%} |",
        f"| Migration sayisi | {summary['migration_count']} |",
        f"| SBOM girdi sayisi | {summary['sbom_entry_count']} |",
        f"| Kapisi gecen faz sayisi | {len(summary['phases_with_passing_gates'])} |",
        "",
    ]
    if summary["is_complete"]:
        lines += ["Butun kriterler kanitla kapanmistir.", ""]
    else:
        lines += [
            "## Kapanmamis kriterler",
            "",
            f"Toplam {len(summary['incomplete'])} kriter halen acik.",
            "",
        ]
        current = ""
        for item in summary["incomplete"]:
            if item["category"] != current:
                current = item["category"]
                lines += ["", f"### {current}", ""]
            lines.append(f"- `{item['id']}` ({item['state']}): {item['criterion']}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    # Windows konsolu varsayilan olarak cp1254 kullanir; kriter metinleri UTF-8'dir.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Global DoD ve release hazirligi")
    parser.add_argument("--json", action="store_true", help="JSON cikti")
    parser.add_argument("--yaz", action="store_true", help="GLOBAL_DOD_DURUM.md uret")
    parser.add_argument("--sbom", action="store_true", help="SBOM'u JSON olarak yaz")
    parser.add_argument(
        "--provider-evidence",
        type=UUID,
        metavar="CAMPAIGN_UUID",
        help="Kanonik continuation campaign icin sanitize ZEKAM-DOD-025 kaniti yaz",
    )
    arguments = parser.parse_args()

    if arguments.sbom:
        print(json.dumps(build_sbom(), ensure_ascii=False, indent=2))
        return 0

    if arguments.provider_evidence is not None:
        evidence = _build_canonical_provider_acceptance_v3(arguments.provider_evidence)
        PROVIDER_ACCEPTANCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROVIDER_ACCEPTANCE_PATH.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(PROVIDER_ACCEPTANCE_PATH.relative_to(ROOT).as_posix())
        return 0

    summary = assess()
    if arguments.yaz:
        REPORT_PATH.write_text(render(summary), encoding="utf-8", newline="\n")
        print(REPORT_PATH.relative_to(ROOT).as_posix())
        return 0
    if arguments.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    print(render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
