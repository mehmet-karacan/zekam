"""PostgreSQL acceptance tests for OpenCode benchmark campaign ledger."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from zekam.application.execution import ExecutionHost
from zekam.application.governance import EffectRequest, GovernanceService
from zekam.application.model_benchmark_service import load_fixture_registry
from zekam.application.project_integration import ProjectIntegrationService
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConcurrencyConflict
from zekam.domain.identifiers import new_uuid7
from zekam.domain.model_benchmark import BenchmarkPlan, BenchmarkSuite, SuiteKind
from zekam.domain.model_campaign import (
    AUDIO_EXCLUSION_REASON,
    CampaignContinuation,
    CampaignMember,
    CampaignMemberDisposition,
    CampaignMemberPlan,
    CampaignMemberResult,
    CampaignMemberResultStage,
    CampaignMemberResultStatus,
    CampaignOutcome,
    CampaignOutcomeStatus,
    OpenCodeBenchmarkCampaign,
    QualificationAction,
    QualificationEvent,
    ResultAdoption,
    ResultRecoveryEvidence,
)
from zekam.domain.realm import Actor, ActorKind
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import Job, JobKind
from zekam.domain.work import EffectKind, PlanStep, TaskPlan, WorkItem, WorkType
from zekam.infrastructure.postgres.core_repository import ActorRepository
from zekam.infrastructure.postgres.model_benchmark_repository import BenchmarkRepository
from zekam.infrastructure.postgres.model_campaign_repository import ModelCampaignRepository
from zekam.infrastructure.postgres.work_repository import TaskPlanRepository, WorkItemRepository

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def _work_binding(realm: Any, connection: Any, tmp_path: Path) -> tuple[WorkItem, TaskPlan]:
    source = tmp_path / "campaign-source"
    source.mkdir()
    project = ProjectIntegrationService(connection, realm).register(source_path=source)
    work = WorkItem.create(
        realm_id=realm.id,
        project_id=project.id,
        type=WorkType.TASK,
        title="OpenCode configured model campaign",
    )
    WorkItemRepository(connection, realm.id).add(work)
    policy_digest = digest("campaign-policy")
    plan = TaskPlan.create(
        work_item=work,
        revision=1,
        source_revision="source-revision-1",
        policy_digest=policy_digest,
        steps=(
            PlanStep(
                step_id="health-then-benchmark",
                title="Health passed modelleri exact benchmark et",
                effect=EffectKind.PROVIDER_CALL,
                logical_resources=("provider:aihub",),
            ),
        ),
    )
    TaskPlanRepository(connection, realm.id).append(plan)
    return work, plan


def _campaign(
    work: WorkItem, task_plan: TaskPlan, fixture_digest: str
) -> OpenCodeBenchmarkCampaign:
    return OpenCodeBenchmarkCampaign(
        campaign_key="opencode-aihub",
        revision=1,
        work_item_id=work.id,
        task_plan_id=task_plan.id,
        source_revision=task_plan.source_revision,
        provider_ref="aihub",
        catalog_digest=digest("catalog"),
        endpoint_identity_digest=digest("endpoint"),
        inventory_digest=digest("inventory"),
        policy_digest=task_plan.policy_digest,
        fixture_registry_digest=load_fixture_registry().registry_digest,
        verifier_identity="verifier:independent",
        verifier_provenance_digest=digest("verifier-provenance"),
        source_digest=digest("opencode-source"),
        repetitions=5,
        verifier_provider_calls_per_trial=1,
        members=(
            CampaignMember(
                configured_model_id="aihub/code-model",
                canonical_model_id="canonical-code-model",
                modality="code",
                disposition=CampaignMemberDisposition.HEALTH_PENDING,
                fixture_digests=(fixture_digest,),
            ),
            CampaignMember(
                configured_model_id="aihub/audio-model",
                canonical_model_id=None,
                modality="audio_transcription",
                disposition=CampaignMemberDisposition.EXCLUDED_AUDIO,
                exclusion_reason=AUDIO_EXCLUSION_REASON,
            ),
        ),
    )


def _store_approved_aggregate(connection: Any, *, realm_id: UUID, benchmark_plan_id: UUID) -> UUID:
    aggregate_id = new_uuid7()
    metric = {"mean": 1.0, "median": 1.0, "p95": 1.0, "variance": 0.0}
    with connection.cursor() as cursor:
        cursor.execute(
            "insert into models.benchmark_aggregate"
            " (id, realm_id, plan_id, tested_model_id, verifier_model_id,"
            "  verifier_execution_identity, verifier_provenance_digest, approved, unsafe,"
            "  metrics, evidence_digest)"
            " values (%s, %s, %s, %s, %s, %s, %s, true, false, %s::jsonb, %s)",
            (
                aggregate_id,
                realm_id,
                benchmark_plan_id,
                "canonical-code-model",
                "independent-verifier-model",
                "verifier:independent",
                digest("verifier-provenance"),
                canonical_json({"quality": metric}),
                digest("aggregate-evidence"),
            ),
        )
    return aggregate_id


def test_campaign_tables_are_append_only_and_rls_enabled(migrated_database: Any) -> None:
    expected = {
        "opencode_benchmark_campaign",
        "opencode_benchmark_campaign_member",
        "opencode_benchmark_campaign_member_plan",
        "opencode_benchmark_campaign_member_result",
        "opencode_benchmark_campaign_outcome",
        "opencode_model_qualification_event",
    }
    from zekam.infrastructure.postgres.connection import connect

    with connect(migrated_database) as connection, connection.cursor() as cursor:
        cursor.execute(
            "select tablename, rowsecurity from pg_tables where schemaname = 'models'"
            " and tablename = any(%s)",
            (list(expected),),
        )
        rows = cursor.fetchall()
        cursor.execute(
            "select count(*) from pg_trigger t"
            " join pg_class c on c.oid = t.tgrelid"
            " join pg_namespace n on n.oid = c.relnamespace"
            " where n.nspname = 'models' and c.relname = any(%s)"
            " and not t.tgisinternal and t.tgname in ('deny_update', 'deny_delete')",
            (list(expected),),
        )
        immutable_trigger_count = int(cursor.fetchone()[0])
    assert {str(row[0]) for row in rows} == expected
    assert all(bool(row[1]) for row in rows)
    assert immutable_trigger_count == len(expected) * 2


def test_campaign_replay_health_gate_terminal_outcome_and_qualification(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    work, task_plan = _work_binding(realm, connection, tmp_path)
    registry = load_fixture_registry()
    fixture = registry.fixtures[0]
    campaign = _campaign(work, task_plan, fixture.fixture_digest)
    repository = ModelCampaignRepository(connection, realm.id)

    campaign_id, created = repository.ensure_campaign(campaign)
    replay_id, replay_created = repository.ensure_campaign(campaign)
    assert created and not replay_created and replay_id == campaign_id
    assert not repository.status(campaign_id).terminal
    with pytest.raises(ConcurrencyConflict, match="payload drift"):
        repository.ensure_campaign(replace(campaign, provider_ref="aihub-reconfigured"))

    members = repository.list_members(campaign_id)
    code_member = next(
        item
        for item in members
        if item.member.disposition is CampaignMemberDisposition.HEALTH_PENDING
    )
    health_evidence = digest("health-passed")
    health_result = CampaignMemberResult(
        stage=CampaignMemberResultStage.HEALTH,
        status=CampaignMemberResultStatus.PASSED,
        evidence_digest=health_evidence,
        actual_tested_call_count=0,
        actual_provider_call_count=1,
    )
    health_id, health_created = repository.record_member_result(
        campaign_id=campaign_id,
        member_id=code_member.id,
        member_plan_id=None,
        result=health_result,
    )
    replay_health_id, replay_health_created = repository.record_member_result(
        campaign_id=campaign_id,
        member_id=code_member.id,
        member_plan_id=None,
        result=health_result,
    )
    assert health_created and not replay_health_created and replay_health_id == health_id

    suite = BenchmarkSuite(
        suite_id="opencode-campaign-code",
        version=1,
        kind=SuiteKind.GENERAL,
        fixture_digests=(fixture.fixture_digest,),
    )
    benchmark_plan = BenchmarkPlan(
        model_id="canonical-code-model",
        suite_digest=suite.suite_digest,
        inventory_digest=campaign.inventory_digest,
        policy_digest=campaign.policy_digest,
        fixture_registry_digest=registry.registry_digest,
        repetitions=campaign.repetitions,
        remote_execution=True,
    )
    benchmark_plan_id, _ = BenchmarkRepository(connection, realm.id).ensure_plan(
        registry=registry, suite=suite, plan=benchmark_plan
    )
    member_plan = CampaignMemberPlan(
        benchmark_plan_id=benchmark_plan_id,
        benchmark_plan_digest=benchmark_plan.plan_digest,
        health_evidence_digest=health_evidence,
        authorization_manifest_digest=digest("five-tested-five-verifier-authorizations"),
        tested_call_budget=5,
        provider_call_budget=10,
    )
    member_plan_id, plan_created = repository.store_member_plan(
        campaign_id=campaign_id, member_id=code_member.id, plan=member_plan
    )
    replay_plan_id, replay_plan_created = repository.store_member_plan(
        campaign_id=campaign_id, member_id=code_member.id, plan=member_plan
    )
    assert plan_created and not replay_plan_created and replay_plan_id == member_plan_id

    aggregate_id = _store_approved_aggregate(
        connection, realm_id=realm.id, benchmark_plan_id=benchmark_plan_id
    )
    benchmark_result = CampaignMemberResult(
        stage=CampaignMemberResultStage.BENCHMARK,
        status=CampaignMemberResultStatus.PASSED,
        evidence_digest=digest("benchmark-result"),
        actual_tested_call_count=5,
        actual_provider_call_count=10,
        aggregate_id=aggregate_id,
    )
    repository.record_member_result(
        campaign_id=campaign_id,
        member_id=code_member.id,
        member_plan_id=member_plan_id,
        result=benchmark_result,
    )
    outcome = CampaignOutcome(
        status=CampaignOutcomeStatus.PASSED,
        passed_count=1,
        failed_count=0,
        recovery_required_count=0,
        audio_excluded_count=1,
        actual_tested_call_count=5,
        actual_provider_call_count=11,
        evidence_digest=digest("terminal-outcome"),
    )
    outcome_id, outcome_created = repository.record_outcome(
        campaign_id=campaign_id, outcome=outcome
    )
    replay_outcome_id, replay_outcome_created = repository.record_outcome(
        campaign_id=campaign_id, outcome=outcome
    )
    assert outcome_created and not replay_outcome_created and replay_outcome_id == outcome_id

    event = QualificationEvent(
        action=QualificationAction.QUALIFIED,
        model_id="canonical-code-model",
        outcome_id=outcome_id,
        evidence_digest=digest("qualification"),
        aggregate_id=aggregate_id,
    )
    with pytest.raises(Exception, match="disqualification failed member result"):
        repository.record_qualification(
            campaign_id=campaign_id,
            member_id=code_member.id,
            event=QualificationEvent(
                action=QualificationAction.DISQUALIFIED,
                model_id="canonical-code-model",
                outcome_id=outcome_id,
                evidence_digest=digest("invalid-disqualification"),
                reason_code="not-a-failed-member",
            ),
        )
    event_id, event_created = repository.record_qualification(
        campaign_id=campaign_id, member_id=code_member.id, event=event
    )
    replay_event_id, replay_event_created = repository.record_qualification(
        campaign_id=campaign_id, member_id=code_member.id, event=event
    )
    assert event_created and not replay_event_created and replay_event_id == event_id
    with pytest.raises(Exception, match="opencode_qualification_member_unique"):
        repository.record_qualification(
            campaign_id=campaign_id,
            member_id=code_member.id,
            event=QualificationEvent(
                action=QualificationAction.QUALIFIED,
                model_id="canonical-code-model",
                outcome_id=outcome_id,
                evidence_digest=digest("conflicting-disqualification"),
                aggregate_id=aggregate_id,
            ),
        )

    status = repository.status(campaign_id)
    latest = repository.latest_terminal(campaign.campaign_key)
    assert status.terminal and status.outcome_status is CampaignOutcomeStatus.PASSED
    assert latest == status
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from models.opencode_benchmark_campaign_member_result"
            " where campaign_id = %s",
            (campaign_id,),
        )
        assert int(cursor.fetchone()[0]) == 2
    rejected_task_plan = TaskPlan.create(
        work_item=work,
        revision=2,
        source_revision="source-revision-after-passed-parent",
        policy_digest=campaign.policy_digest,
        steps=task_plan.steps,
    )
    TaskPlanRepository(connection, realm.id).append(rejected_task_plan)
    with pytest.raises(Exception, match="continuation parent/revision/binding mismatch"):
        repository.ensure_continuation_campaign(
            replace(
                campaign,
                revision=2,
                task_plan_id=rejected_task_plan.id,
                source_revision=rejected_task_plan.source_revision,
                source_digest=digest("changed-source"),
                continuation=CampaignContinuation(
                    parent_campaign_id=campaign_id,
                    parent_source_revision=campaign.source_revision,
                    compatibility_evidence_digest=digest("compatibility"),
                    continuation_provenance_digest=digest("rejected-continuation"),
                    maximum_tested_call_count=5,
                    maximum_provider_call_count=10,
                ),
            )
        )


def test_benchmark_plan_cannot_precede_passed_health(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    work, task_plan = _work_binding(realm, connection, tmp_path)
    registry = load_fixture_registry()
    fixture = registry.fixtures[0]
    campaign = _campaign(work, task_plan, fixture.fixture_digest)
    repository = ModelCampaignRepository(connection, realm.id)
    campaign_id, _ = repository.ensure_campaign(campaign)
    member = next(
        item
        for item in repository.list_members(campaign_id)
        if item.member.disposition is CampaignMemberDisposition.HEALTH_PENDING
    )
    suite = BenchmarkSuite("health-gated", 1, SuiteKind.GENERAL, (fixture.fixture_digest,))
    plan = BenchmarkPlan(
        "canonical-code-model",
        suite.suite_digest,
        campaign.inventory_digest,
        campaign.policy_digest,
        registry.registry_digest,
        remote_execution=True,
    )
    plan_id, _ = BenchmarkRepository(connection, realm.id).ensure_plan(
        registry=registry, suite=suite, plan=plan
    )
    with pytest.raises(Exception, match="exact binding mismatch"):
        repository.store_member_plan(
            campaign_id=campaign_id,
            member_id=member.id,
            plan=CampaignMemberPlan(
                benchmark_plan_id=plan_id,
                benchmark_plan_digest=plan.plan_digest,
                health_evidence_digest=digest("missing-health"),
                authorization_manifest_digest=digest("authorization"),
                tested_call_budget=5,
                provider_call_budget=10,
            ),
        )


def test_configured_route_expands_to_distinct_canonical_members(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    work, task_plan = _work_binding(realm, connection, tmp_path)
    fixture = load_fixture_registry().fixtures[0]
    base = _campaign(work, task_plan, fixture.fixture_digest)
    code_member = base.members[0]
    expanded = replace(
        base,
        campaign_key="opencode-aihub-expanded",
        members=(
            code_member,
            replace(code_member, canonical_model_id="canonical-code-model-alternative"),
            base.members[1],
        ),
    )
    repository = ModelCampaignRepository(connection, realm.id)
    campaign_id, created = repository.ensure_campaign(expanded)
    members = repository.list_members(campaign_id)
    assert created
    assert len(members) == 3
    assert [
        item.member.canonical_model_id
        for item in members
        if item.member.configured_model_id == "aihub/code-model"
    ] == ["canonical-code-model", "canonical-code-model-alternative"]
    with connection.cursor() as cursor:
        cursor.execute(
            "select configured_model_count, member_count, eligible_model_count"
            " from models.opencode_benchmark_campaign where id = %s",
            (campaign_id,),
        )
        assert tuple(int(value) for value in cursor.fetchone()) == (2, 3, 2)
    with (
        pytest.raises(Exception, match="opencode_campaign_member_target_unique"),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "insert into models.opencode_benchmark_campaign_member"
            " (id, realm_id, campaign_id, configured_model_id, canonical_model_id,"
            "  modality, disposition, fixture_digests, exclusion_reason, suite_digest,"
            "  tested_call_budget, provider_call_budget)"
            " select %s, realm_id, campaign_id, configured_model_id, canonical_model_id,"
            "  modality, disposition, fixture_digests, exclusion_reason, suite_digest,"
            "  tested_call_budget, provider_call_budget"
            " from models.opencode_benchmark_campaign_member"
            " where campaign_id = %s and configured_model_id = %s"
            " and canonical_model_id = %s",
            (
                new_uuid7(),
                campaign_id,
                "aihub/code-model",
                "canonical-code-model",
            ),
        )


def test_recovery_continuation_adopts_terminal_results_and_completed_health_claim(
    realm_session: tuple[Any, Any], tmp_path: Path
) -> None:
    realm, connection = realm_session
    work, parent_task_plan = _work_binding(realm, connection, tmp_path)
    registry = load_fixture_registry()
    fixture = registry.fixtures[0]
    base = _campaign(work, parent_task_plan, fixture.fixture_digest)
    second_member = replace(
        base.members[0],
        configured_model_id="aihub/vision-model",
        canonical_model_id="canonical-vision-model",
        modality="vision_language",
    )
    parent = replace(base, members=(base.members[0], second_member, base.members[1]))
    repository = ModelCampaignRepository(connection, realm.id)
    parent_id, _ = repository.ensure_campaign(parent)
    parent_members = repository.list_members(parent_id)
    first = next(
        item for item in parent_members if item.member.canonical_model_id == "canonical-code-model"
    )
    health_evidence = digest("parent-health-passed")
    repository.record_member_result(
        campaign_id=parent_id,
        member_id=first.id,
        member_plan_id=None,
        result=CampaignMemberResult(
            stage=CampaignMemberResultStage.HEALTH,
            status=CampaignMemberResultStatus.PASSED,
            evidence_digest=health_evidence,
            actual_tested_call_count=0,
            actual_provider_call_count=1,
        ),
    )
    suite = BenchmarkSuite(
        "continuation-reused-suite",
        parent.benchmark_suite_version,
        SuiteKind.GENERAL,
        (fixture.fixture_digest,),
    )
    benchmark_plan = BenchmarkPlan(
        "canonical-code-model",
        suite.suite_digest,
        parent.inventory_digest,
        parent.policy_digest,
        registry.registry_digest,
        repetitions=parent.repetitions,
        remote_execution=True,
    )
    benchmark_plan_id, _ = BenchmarkRepository(connection, realm.id).ensure_plan(
        registry=registry, suite=suite, plan=benchmark_plan
    )
    parent_member_plan_id, _ = repository.store_member_plan(
        campaign_id=parent_id,
        member_id=first.id,
        plan=CampaignMemberPlan(
            benchmark_plan_id=benchmark_plan_id,
            benchmark_plan_digest=benchmark_plan.plan_digest,
            health_evidence_digest=health_evidence,
            authorization_manifest_digest=digest("parent-authorizations"),
            tested_call_budget=5,
            provider_call_budget=10,
        ),
    )
    aggregate_id = _store_approved_aggregate(
        connection, realm_id=realm.id, benchmark_plan_id=benchmark_plan_id
    )
    benchmark_evidence = digest("parent-benchmark-passed")
    repository.record_member_result(
        campaign_id=parent_id,
        member_id=first.id,
        member_plan_id=parent_member_plan_id,
        result=CampaignMemberResult(
            stage=CampaignMemberResultStage.BENCHMARK,
            status=CampaignMemberResultStatus.PASSED,
            evidence_digest=benchmark_evidence,
            actual_tested_call_count=5,
            actual_provider_call_count=10,
            aggregate_id=aggregate_id,
        ),
    )

    actor = ActorRepository(connection, realm.id).add(
        Actor.create(realm=realm, kind=ActorKind.HUMAN, slug="campaign-recovery")
    )
    call_id = "health-canonical-vision-model-0123456789ab-0"
    call_resource = f"provider:canonical-vision-model:chat-completions:{call_id}"
    request = EffectRequest(
        action="provider-call",
        effects=(EffectKind.PROVIDER_CALL,),
        resources=(call_resource,),
        provider_refs=("litellm",),
        touches_external_system=True,
    )
    governance = GovernanceService(connection, realm, actor_id=actor.id)
    authorization = governance.issue_authorization(
        request=request,
        actor_id=actor.id,
        plan_digest=parent_task_plan.plan_digest,
        work_item_id=work.id,
        plan_id=parent_task_plan.id,
    )
    consumed = governance.consume_authorization(
        authorization.id, request=request, consumed_by=f"test:{call_id}"
    )
    assert consumed.consumed
    host = ExecutionHost(connection, realm.id, worker_label="campaign-continuation-test")
    job, _ = host.jobs.enqueue(
        Job.create(
            realm_id=realm.id,
            project_id=work.project_id,
            kind=JobKind.PROVIDER_CALL,
            idempotency_key=digest({"parent": str(parent_id), "call": call_id}),
            resources=parse_requests(write=(call_resource,)),
            required_capabilities=("provider.call",),
            max_attempts=1,
            work_item_id=work.id,
            plan_id=parent_task_plan.id,
            step_id=call_id,
        )
    )
    claimed = host.acquire_work(capabilities=("provider.call",))
    assert claimed is not None and claimed.job.id == job.id
    claim = host.claim_effect(
        claimed,
        operation=f"provider-contract:{call_id}",
        effect_digest=request.effect_digest,
        authorization_digest=authorization.authorization_digest,
        authorization_id=authorization.id,
        idempotency_key=digest({"provider-call": call_id}),
        resources=parse_requests(write=(call_resource,)),
        adapter_digest=digest("authorized-provider-client"),
    )
    receipt = host.record_success(
        claim,
        result_digest=digest("vision-provider-response"),
        adapter_evidence_digest=digest("vision-provider-receipt"),
    )
    parent_outcome = CampaignOutcome(
        status=CampaignOutcomeStatus.RECOVERY_REQUIRED,
        passed_count=1,
        failed_count=0,
        recovery_required_count=1,
        audio_excluded_count=1,
        actual_tested_call_count=5,
        actual_provider_call_count=12,
        evidence_digest=digest("parent-recovery-outcome"),
    )
    repository.record_outcome(campaign_id=parent_id, outcome=parent_outcome)

    child_task_plan = TaskPlan.create(
        work_item=work,
        revision=2,
        source_revision="source-revision-after-bugfix",
        policy_digest=parent.policy_digest,
        steps=parent_task_plan.steps,
    )
    TaskPlanRepository(connection, realm.id).append(child_task_plan)
    child = replace(
        parent,
        revision=2,
        task_plan_id=child_task_plan.id,
        source_revision=child_task_plan.source_revision,
        source_digest=digest("source-after-remote-response-fix"),
        continuation=CampaignContinuation(
            parent_campaign_id=parent_id,
            parent_source_revision=parent.source_revision,
            compatibility_evidence_digest=digest("source-compatibility-review"),
            continuation_provenance_digest=digest("continuation-plan"),
            maximum_tested_call_count=5,
            maximum_provider_call_count=10,
        ),
    )
    with pytest.raises(Exception, match="parent/revision/binding mismatch"):
        repository.ensure_continuation_campaign(
            replace(
                child,
                continuation=CampaignContinuation(
                    parent_campaign_id=parent_id,
                    parent_source_revision=parent.source_revision,
                    compatibility_evidence_digest=digest("source-compatibility-review"),
                    continuation_provenance_digest=digest("over-budget-continuation"),
                    maximum_tested_call_count=6,
                    maximum_provider_call_count=11,
                ),
            )
        )
    child_id, child_created = repository.ensure_continuation_campaign(child)
    assert child_created
    child_members = repository.list_members(child_id)
    child_first = next(
        item for item in child_members if item.member.canonical_model_id == "canonical-code-model"
    )
    child_second = next(
        item for item in child_members if item.member.canonical_model_id == "canonical-vision-model"
    )
    adoptable = repository.adoptable_results(parent_id)
    assert [(item.stage.value, item.status.value) for item in adoptable] == [
        ("benchmark", "passed"),
        ("health", "passed"),
    ]
    benchmark_source = next(
        item for item in adoptable if item.stage is CampaignMemberResultStage.BENCHMARK
    )
    with pytest.raises(Exception, match="parent/model/plan/suite mismatch"):
        repository.record_adopted_result(
            campaign_id=child_id,
            member_id=child_second.id,
            result=CampaignMemberResult(
                stage=benchmark_source.stage,
                status=benchmark_source.status,
                evidence_digest=benchmark_source.evidence_digest,
                actual_tested_call_count=0,
                actual_provider_call_count=0,
                aggregate_id=benchmark_source.aggregate_id,
                adoption=ResultAdoption(benchmark_source.id, digest("wrong-member-adoption")),
            ),
        )
    adopted_ids: list[UUID] = []
    for source in adoptable:
        adopted_id, created = repository.record_adopted_result(
            campaign_id=child_id,
            member_id=child_first.id,
            result=CampaignMemberResult(
                stage=source.stage,
                status=source.status,
                evidence_digest=source.evidence_digest,
                actual_tested_call_count=0,
                actual_provider_call_count=0,
                aggregate_id=source.aggregate_id,
                failure_category=source.failure_category,
                adoption=ResultAdoption(
                    source.id,
                    digest({"parent_result": str(source.id), "child": str(child_id)}),
                ),
            ),
        )
        assert created
        adopted_ids.append(adopted_id)
    repository.record_recovered_health_failure(
        campaign_id=child_id,
        member_id=child_second.id,
        result=CampaignMemberResult(
            stage=CampaignMemberResultStage.HEALTH,
            status=CampaignMemberResultStatus.FAILED,
            evidence_digest=digest("recovered-vision-health-failure"),
            actual_tested_call_count=0,
            actual_provider_call_count=0,
            failure_category="health-contract-failed",
            recovery_evidence=ResultRecoveryEvidence(
                claim.id, receipt.id, digest("vision-claim-recovery-provenance")
            ),
        ),
    )
    child_outcome = CampaignOutcome(
        status=CampaignOutcomeStatus.FAILED,
        passed_count=1,
        failed_count=1,
        recovery_required_count=0,
        audio_excluded_count=1,
        actual_tested_call_count=0,
        actual_provider_call_count=0,
        evidence_digest=digest("child-terminal-outcome"),
    )
    child_outcome_id, _ = repository.record_outcome(campaign_id=child_id, outcome=child_outcome)
    qualification_id, qualified = repository.record_qualification(
        campaign_id=child_id,
        member_id=child_first.id,
        event=QualificationEvent(
            action=QualificationAction.QUALIFIED,
            model_id="canonical-code-model",
            outcome_id=child_outcome_id,
            evidence_digest=digest("adopted-qualification"),
            aggregate_id=aggregate_id,
        ),
    )
    assert qualified and qualification_id
    chain = repository.continuation_chain(child_id)
    assert [item.campaign_id for item in chain] == [child_id, parent_id]
    assert chain[0].parent_campaign_id == parent_id
    assert chain[0].actual_provider_call_count == 0
    assert chain[0].current_provider_call_budget == 10
    assert len(adopted_ids) == 2
