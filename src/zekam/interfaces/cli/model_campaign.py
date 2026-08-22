"""OpenCode/AIHub benchmark campaign CLI.

The commands in this module separate planning, authority issuance and execution.
Planning is read-only.  ``authorize`` persists an exact Work-bound plan and one-shot
authorizations but never calls a provider.  ``run`` is implemented below as the only
consumer of that exact authority set.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer
from rich.console import Console

from zekam.application.environment import environment_value
from zekam.application.execution import ExecutionHost
from zekam.application.governance import DEFAULT_POLICY_NAME, GovernanceService
from zekam.application.model_benchmark_service import (
    BenchmarkExecutionService,
    RuntimeBenchmarkClaimGateway,
    default_fixture_file,
    load_fixture_registry,
)
from zekam.application.opencode_benchmark_campaign import (
    BENCHMARK_SECRET_REF_NAME,
    CampaignCallKind,
    CampaignDiscovery,
    PreparedCampaignManifest,
    default_scope_file,
    discover_campaign,
    normalize_provider_response,
    prepare_campaign_manifest,
)
from zekam.application.opencode_embedding import (
    OpenCodeCredentialStore,
    default_opencode_config_file,
)
from zekam.application.opencode_remote_benchmark import (
    EVALUATOR_PROVENANCE_DIGEST,
    DeterministicProviderNeutralVerifier,
    OpenCodeDeterministicBenchmarkVerifier,
    OpenCodeRemoteBenchmarkAdapter,
    ProcessMemoryResponseStore,
    RemoteProviderInvocation,
    RemoteProviderResponse,
    embedding_repetitions_are_deterministic,
    load_remote_fixture,
)
from zekam.application.provider_adapter import (
    AuthorizedProviderClient,
    EndpointResolver,
    UrllibJsonProviderTransport,
)
from zekam.application.provider_contract_execution import (
    ProviderExecutionManifest,
    build_provider_policy_candidate,
)
from zekam.application.provider_contract_runner import (
    RuntimeProviderContractRunner,
    verify_exact_provider_authorization,
)
from zekam.application.secret_broker import SecretBroker
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import Checkpoint
from zekam.domain.errors import PolicyViolation, ValidationFailed, ZekamError
from zekam.domain.model_benchmark import (
    BenchmarkPlan,
    BenchmarkSuite,
    SuiteKind,
    TrialResult,
    VerifierIdentity,
)
from zekam.domain.model_campaign import (
    CampaignContinuation,
    CampaignMember,
    CampaignMemberDisposition,
    CampaignMemberPlan,
    CampaignMemberResult,
    CampaignMemberResultRecord,
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
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import AttemptOutcome, FailureCategory, Job, JobKind
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
    AuthorizationState,
    DataClassification,
    SecretBackend,
    SecretRef,
)
from zekam.domain.work import EffectKind, PlanStep
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.model_benchmark_repository import BenchmarkRepository
from zekam.infrastructure.postgres.model_campaign_repository import ModelCampaignRepository
from zekam.infrastructure.postgres.security_repository import (
    AuthorizationRepository,
    SecretRefRepository,
)
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, RealmSession, fail, fail_from

app = typer.Typer(
    name="campaign",
    help="OpenCode/AIHub configured model benchmark kampanyasi",
    no_args_is_help=True,
)
console = Console()

_SOURCE_FILES = (
    "config/model_benchmark_fixtures.yaml",
    "config/opencode_benchmark_scope.yaml",
    "src/zekam/application/model_benchmark_service.py",
    "src/zekam/application/execution.py",
    "src/zekam/application/opencode_benchmark_campaign.py",
    "src/zekam/application/opencode_embedding.py",
    "src/zekam/application/opencode_remote_benchmark.py",
    "src/zekam/application/provider_adapter.py",
    "src/zekam/application/provider_contract_execution.py",
    "src/zekam/application/provider_contract_runner.py",
    "src/zekam/domain/model_benchmark.py",
    "src/zekam/domain/model_campaign.py",
    "src/zekam/infrastructure/postgres/model_benchmark_repository.py",
    "src/zekam/infrastructure/postgres/model_campaign_repository.py",
    "src/zekam/infrastructure/postgres/context_continuity_repository.py",
    "src/zekam/infrastructure/postgres/runtime_repository.py",
    "src/zekam/interfaces/cli/model.py",
    "src/zekam/interfaces/cli/model_campaign.py",
    "migrations/0018_opencode_benchmark_campaign.sql",
    "migrations/0018_opencode_benchmark_campaign.down.sql",
    "migrations/0019_opencode_campaign_continuation.sql",
    "migrations/0019_opencode_campaign_continuation.down.sql",
    "modeller/KANONIK_MODEL_ENVANTERI.yaml",
    "modeller/BENCHMARK_SUITE_KATALOGU.md",
)


def _source_revision() -> tuple[str, str]:
    root = Path(__file__).resolve().parents[4]
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    head = completed.stdout.strip()
    if completed.returncode != 0 or len(head) != 40:
        raise PolicyViolation("Campaign source HEAD okunamadi")
    file_digests: dict[str, str] = {}
    for relative in _SOURCE_FILES:
        source = root / relative
        if not source.is_file():
            raise PolicyViolation(f"Campaign source dosya seti eksik: {relative}")
        file_digests[relative] = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    source_digest = digest(file_digests).removeprefix("sha256:")
    segmented = "-".join(
        source_digest[index : index + 8] for index in range(0, len(source_digest), 8)
    )
    return head, f"{head}:sha256:{segmented}"


def _load_manifest(
    *, config_file: Path | None, scope_file: Path | None
) -> tuple[CampaignDiscovery, PreparedCampaignManifest]:
    discovery = discover_campaign(
        config_file=config_file or default_opencode_config_file(),
        scope_file=scope_file or default_scope_file(),
        verifier_provenance_digest=EVALUATOR_PROVENANCE_DIGEST,
    )
    return discovery, prepare_campaign_manifest(
        discovery,
        config_file=config_file or default_opencode_config_file(),
    )


def _campaign_steps(
    manifest: PreparedCampaignManifest,
    *,
    project_id: UUID,
    work_id: UUID,
    active_calls: tuple[Any, ...] | None = None,
    suite_version: int = 1,
) -> tuple[PlanStep, ...]:
    orchestration = "opencode-aihub-campaign"
    steps: list[PlanStep] = [
        PlanStep(
            step_id=orchestration,
            title=f"OpenCode AIHub campaign {manifest.manifest_digest}",
            effect=EffectKind.NONE,
            logical_resources=manifest.execution_manifest.policy_resources,
            risk="critical",
        )
    ]
    previous = orchestration
    for item in manifest.calls if active_calls is None else active_calls:
        steps.append(
            PlanStep(
                step_id=item.call_id,
                title=f"Exact {item.kind.value} provider call {item.call_id}",
                effect=EffectKind.PROVIDER_CALL,
                logical_resources=(item.prepared.plan.target, item.prepared.plan.call_resource),
                depends_on=(previous,),
                risk="critical",
            )
        )
        previous = item.call_id
    for target in manifest.discovery.targets:
        if target.excluded_reason is not None:
            continue
        suite = BenchmarkSuite(
            suite_id=(f"opencode-aihub:{target.modality.value}:{target.canonical_model_id}"),
            version=suite_version,
            kind=SuiteKind.GENERAL,
            fixture_digests=target.fixture_digests,
        )
        step_id = f"member-finalize-{target.canonical_model_id}"
        steps.append(
            PlanStep(
                step_id=step_id,
                title=f"Persist verified member result {target.canonical_model_id}",
                effect=EffectKind.DATABASE_WRITE,
                logical_resources=tuple(
                    sorted(
                        (
                            f"model-benchmark:{target.canonical_model_id}:"
                            f"{suite.suite_digest.removeprefix('sha256:')}",
                            f"model-benchmark:{target.canonical_model_id}:campaign-ledger",
                        )
                    )
                ),
                depends_on=(previous,),
                risk="high",
            )
        )
        previous = step_id
    steps.append(
        PlanStep(
            step_id="campaign-finalize",
            title="Publish terminal campaign outcome and runtime evidence",
            effect=EffectKind.DATABASE_WRITE,
            logical_resources=(f"work:{project_id}:{work_id}",),
            depends_on=(previous,),
            risk="critical",
        )
    )
    return tuple(steps)


def _domain_campaign(
    discovery: CampaignDiscovery,
    manifest: PreparedCampaignManifest,
    *,
    work_id: UUID,
    task_plan_id: UUID,
    source_revision: str,
    policy_digest: str,
    revision: int,
    continuation: CampaignContinuation | None = None,
) -> OpenCodeBenchmarkCampaign:
    members = tuple(
        CampaignMember(
            configured_model_id=item.configured_model_id,
            canonical_model_id=item.canonical_model_id,
            modality=item.modality.value,
            disposition=(
                CampaignMemberDisposition.EXCLUDED_AUDIO
                if item.excluded_reason is not None
                else CampaignMemberDisposition.HEALTH_PENDING
            ),
            fixture_digests=item.fixture_digests,
            exclusion_reason=item.excluded_reason,
        )
        for item in discovery.targets
    )
    return OpenCodeBenchmarkCampaign(
        campaign_key="opencode-aihub",
        revision=revision,
        work_item_id=work_id,
        task_plan_id=task_plan_id,
        source_revision=source_revision,
        provider_ref=discovery.scope.provider_id,
        catalog_digest=digest(discovery.catalog.sanitized()),
        endpoint_identity_digest=discovery.catalog.endpoint_identity_digest,
        inventory_digest=discovery.inventory_digest,
        policy_digest=policy_digest,
        fixture_registry_digest=discovery.fixture_registry_digest,
        verifier_identity=discovery.scope.verifier.execution_identity,
        verifier_provenance_digest=discovery.verifier_provenance_digest,
        source_digest=manifest.manifest_digest,
        repetitions=discovery.scope.repetitions,
        verifier_provider_calls_per_trial=0,
        members=members,
        benchmark_suite_version=1,
        continuation=continuation,
    )


def _benchmark_plan(
    campaign: OpenCodeBenchmarkCampaign,
    member: CampaignMember,
) -> tuple[BenchmarkSuite, BenchmarkPlan]:
    if member.canonical_model_id is None or member.suite_digest is None:
        raise PolicyViolation("Excluded campaign member benchmark plan tasiyamaz")
    suite = BenchmarkSuite(
        suite_id=f"opencode-aihub:{member.modality}:{member.canonical_model_id}",
        version=campaign.benchmark_suite_version,
        kind=SuiteKind.GENERAL,
        fixture_digests=member.fixture_digests,
    )
    plan = BenchmarkPlan(
        model_id=member.canonical_model_id,
        suite_digest=suite.suite_digest,
        inventory_digest=campaign.inventory_digest,
        policy_digest=campaign.policy_digest,
        fixture_registry_digest=campaign.fixture_registry_digest,
        repetitions=campaign.repetitions,
        remote_execution=True,
    )
    return suite, plan


@dataclass(frozen=True, slots=True)
class _CampaignEndpointResolver(EndpointResolver):
    endpoints: Mapping[tuple[str, str], str] = field(repr=False)

    def resolve(self, endpoint_ref: str, operation: str) -> str:
        endpoint = self.endpoints.get((endpoint_ref, operation))
        if endpoint is None:
            raise PolicyViolation("Campaign endpoint exact reviewed mapping disinda")
        return endpoint


@dataclass(slots=True)
class _OneShotResponseInvoker:
    response: RemoteProviderResponse = field(repr=False)
    expected_model_id: str
    expected_fixture_digest: str
    expected_repetition: int
    consumed: bool = False

    def invoke(self, request: RemoteProviderInvocation) -> RemoteProviderResponse:
        if self.consumed:
            raise PolicyViolation("Campaign provider response replay reddedildi")
        if (
            request.model_id != self.expected_model_id
            or request.fixture_digest != self.expected_fixture_digest
            or request.repetition != self.expected_repetition
        ):
            raise PolicyViolation("Campaign provider response benchmark binding drift")
        self.consumed = True
        return self.response


def _remote_response(
    call: Any,
    *,
    raw_response: Mapping[str, Any],
    artifact: Any,
    latency_ms: int,
) -> RemoteProviderResponse:
    try:
        normalized = normalize_provider_response(
            call.modality,
            raw_response,
            artifact=artifact,
        )
    except ValidationFailed:
        # The provider effect completed, but the model response did not satisfy
        # the reviewed modality contract. This is model-quality evidence rather
        # than a transport/recovery failure. A secret-free empty payload lets the
        # provider-neutral evaluator disqualify this member and continue safely.
        normalized = {}
    usage = raw_response.get("usage")
    prompt_tokens = 0
    completion_tokens = 0
    if isinstance(usage, Mapping):
        prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
        completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
        if isinstance(prompt, int) and not isinstance(prompt, bool) and prompt >= 0:
            prompt_tokens = prompt
        if isinstance(completion, int) and not isinstance(completion, bool) and completion >= 0:
            completion_tokens = completion
    return RemoteProviderResponse(
        payload=normalized,
        latency_ms=latency_ms,
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        estimated_cost=0.0,
        actual_cost=None,
    )


@dataclass(frozen=True, slots=True)
class _RecoveredHealthEffect:
    model_id: str
    call_id: str
    claim_id: UUID
    receipt_id: UUID
    receipt_evidence_digest: str


@dataclass(frozen=True, slots=True)
class _ContinuationRuntime:
    continuation: CampaignContinuation
    active_calls: tuple[Any, ...]
    adopted_results: Mapping[str, tuple[CampaignMemberResultRecord, ...]]
    recovered_health: _RecoveredHealthEffect
    parent_campaign_digest: str
    parent_outcome_digest: str


def _continuation_runtime(
    connection: Any,
    repository: ModelCampaignRepository,
    *,
    parent_campaign_id: UUID,
    manifest: PreparedCampaignManifest,
    work_id: UUID,
    revision: int,
    current_source_revision: str,
    current_policy_digest: str,
) -> _ContinuationRuntime:
    with connection.cursor() as cursor:
        cursor.execute(
            "select c.source_revision, c.work_item_id, c.task_plan_id, c.provider_ref,"
            " c.catalog_digest, c.endpoint_identity_digest, c.inventory_digest,"
            " c.policy_digest, c.fixture_registry_digest, c.verifier_identity,"
            " c.verifier_provenance_digest, c.source_digest, c.campaign_digest,"
            " c.revision, c.benchmark_suite_version, o.outcome_digest, o.status,"
            " o.actual_provider_call_count"
            " from models.opencode_benchmark_campaign c"
            " join models.opencode_benchmark_campaign_outcome o"
            "   on o.realm_id = c.realm_id and o.campaign_id = c.id"
            " where c.id = %s",
            (parent_campaign_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise PolicyViolation("Continuation parent campaign bulunamadi")
    (
        parent_source_revision,
        parent_work_id,
        parent_plan_id,
        parent_provider_ref,
        parent_catalog_digest,
        parent_endpoint_digest,
        parent_inventory_digest,
        parent_policy_digest,
        parent_fixture_digest,
        parent_verifier_identity,
        parent_verifier_digest,
        parent_source_digest,
        parent_campaign_digest,
        parent_revision,
        benchmark_suite_version,
        parent_outcome_digest,
        parent_outcome_status,
        parent_actual_provider_calls,
    ) = row
    expected_bindings = (
        str(parent_provider_ref),
        str(parent_catalog_digest),
        str(parent_endpoint_digest),
        str(parent_inventory_digest),
        str(parent_policy_digest),
        str(parent_fixture_digest),
        str(parent_verifier_identity),
        str(parent_verifier_digest),
        str(parent_source_digest),
    )
    current_bindings = (
        manifest.discovery.scope.provider_id,
        digest(manifest.discovery.catalog.sanitized()),
        manifest.discovery.catalog.endpoint_identity_digest,
        manifest.discovery.inventory_digest,
        current_policy_digest,
        manifest.discovery.fixture_registry_digest,
        manifest.discovery.scope.verifier.execution_identity,
        manifest.discovery.verifier_provenance_digest,
        manifest.manifest_digest,
    )
    if (
        UUID(str(parent_work_id)) != work_id
        or str(parent_outcome_status) != CampaignOutcomeStatus.RECOVERY_REQUIRED.value
        or int(parent_revision) + 1 != revision
        or int(benchmark_suite_version) != 1
        or expected_bindings != current_bindings
    ):
        raise PolicyViolation("Continuation parent Work/revision/provider binding drift")

    adopted_rows = repository.adoptable_results(parent_campaign_id)
    adopted_by_model: dict[str, list[CampaignMemberResultRecord]] = {}
    for result in adopted_rows:
        adopted_by_model.setdefault(result.canonical_model_id, []).append(result)
    terminal_models: set[str] = set()
    represented_call_ids: set[str] = set()
    calls_by_model: dict[str, tuple[Any, ...]] = {}
    for target in manifest.discovery.targets:
        if target.excluded_reason is None:
            calls_by_model[target.canonical_model_id] = tuple(
                item
                for item in manifest.calls
                if item.canonical_model_id == target.canonical_model_id
            )
    for model_id, results in adopted_by_model.items():
        health = next(
            (item for item in results if item.stage is CampaignMemberResultStage.HEALTH), None
        )
        benchmark = next(
            (item for item in results if item.stage is CampaignMemberResultStage.BENCHMARK), None
        )
        if health is None:
            raise PolicyViolation("Continuation parent terminal model health result ister")
        if health.status is CampaignMemberResultStatus.FAILED:
            if benchmark is not None:
                raise PolicyViolation("Failed health parent benchmark result tasiyamaz")
            terminal_models.add(model_id)
            represented_call_ids.add(
                next(
                    item.call_id
                    for item in calls_by_model[model_id]
                    if item.kind is CampaignCallKind.HEALTH
                )
            )
        elif health.status is CampaignMemberResultStatus.PASSED and benchmark is not None:
            terminal_models.add(model_id)
            represented_call_ids.update(item.call_id for item in calls_by_model[model_id])
        else:
            raise PolicyViolation("Continuation parent result set terminal degil")

    with connection.cursor() as cursor:
        cursor.execute(
            "select c.id, r.id, c.operation, r.status, r.adapter_evidence_digest,"
            " j.state, j.max_attempts, a.outcome"
            " from runtime.effect_claim c"
            " join runtime.effect_receipt r on r.realm_id = c.realm_id and r.claim_id = c.id"
            " join runtime.job j on j.realm_id = c.realm_id and j.id = c.job_id"
            " join runtime.job_attempt a on a.realm_id = c.realm_id and a.id = c.attempt_id"
            " where j.plan_id = %s and c.operation like 'provider-contract:%%'"
            " order by c.claimed_at",
            (UUID(str(parent_plan_id)),),
        )
        attempted_rows = cursor.fetchall()
    attempted: dict[str, tuple[UUID, UUID, str, str]] = {}
    for (
        claim_id,
        receipt_id,
        operation,
        receipt_status,
        evidence,
        job_state,
        max_attempts,
        outcome,
    ) in attempted_rows:
        call_id = str(operation).removeprefix("provider-contract:")
        if (
            call_id in attempted
            or str(job_state) != "recovery-required"
            or int(max_attempts) != 1
            or str(outcome) != AttemptOutcome.RECOVERY_REQUIRED.value
            or evidence is None
        ):
            raise PolicyViolation("Continuation parent runtime evidence drift")
        attempted[call_id] = (
            UUID(str(claim_id)),
            UUID(str(receipt_id)),
            str(receipt_status),
            str(evidence),
        )
    if len(attempted) != int(parent_actual_provider_calls):
        raise PolicyViolation("Continuation parent provider call accounting drift")
    extra_attempts = set(attempted) - represented_call_ids
    if len(extra_attempts) != 1:
        raise PolicyViolation("Continuation exact bir projected-olmayan health effect ister")
    repair_call_id = next(iter(extra_attempts))
    repair_call = next((item for item in manifest.calls if item.call_id == repair_call_id), None)
    if repair_call is None or repair_call.kind is not CampaignCallKind.HEALTH:
        raise PolicyViolation("Continuation recovery effect exact health call olmali")
    claim_id, receipt_id, receipt_status, receipt_evidence = attempted[repair_call_id]
    if receipt_status != "completed":
        raise PolicyViolation("Continuation recovered health completed receipt ister")

    eligible_models = set(calls_by_model)
    recovery_models = eligible_models - terminal_models
    if repair_call.canonical_model_id not in recovery_models:
        raise PolicyViolation("Continuation recovered health terminal parent result ile cakisti")
    active_models = recovery_models - {repair_call.canonical_model_id}
    active_calls = tuple(
        item for item in manifest.calls if item.canonical_model_id in active_models
    )
    if any(item.call_id in attempted for item in active_calls):
        raise PolicyViolation("Continuation attempted provider effect'i yeniden cagirmaz")
    tested_budget = sum(item.kind is CampaignCallKind.BENCHMARK for item in active_calls)
    if len(active_calls) > 54 or len(attempted) + len(active_calls) > 102:
        raise PolicyViolation("Continuation approved cumulative provider budgetini asiyor")

    compatibility_evidence = digest(
        {
            "schema": "zekam-opencode-campaign-source-compatibility/v1",
            "parent_campaign_id": parent_campaign_id,
            "parent_source_revision": str(parent_source_revision),
            "current_source_revision": current_source_revision,
            "compatible_change": "response-shape-validation-to-member-failure/v1",
            "provider_bindings": current_bindings,
        }
    )
    continuation_provenance = digest(
        {
            "schema": "zekam-opencode-campaign-continuation/v1",
            "parent_campaign_digest": str(parent_campaign_digest),
            "parent_outcome_digest": str(parent_outcome_digest),
            "compatibility_evidence_digest": compatibility_evidence,
            "adopted_result_digests": sorted(item.result_digest for item in adopted_rows),
            "recovered_call_id": repair_call_id,
            "recovered_claim_id": claim_id,
            "recovered_receipt_id": receipt_id,
            "active_call_ids": sorted(item.call_id for item in active_calls),
        }
    )
    return _ContinuationRuntime(
        continuation=CampaignContinuation(
            parent_campaign_id=parent_campaign_id,
            parent_source_revision=str(parent_source_revision),
            compatibility_evidence_digest=compatibility_evidence,
            continuation_provenance_digest=continuation_provenance,
            maximum_tested_call_count=tested_budget,
            maximum_provider_call_count=len(active_calls),
        ),
        active_calls=active_calls,
        adopted_results={key: tuple(value) for key, value in adopted_by_model.items()},
        recovered_health=_RecoveredHealthEffect(
            model_id=repair_call.canonical_model_id,
            call_id=repair_call_id,
            claim_id=claim_id,
            receipt_id=receipt_id,
            receipt_evidence_digest=receipt_evidence,
        ),
        parent_campaign_digest=str(parent_campaign_digest),
        parent_outcome_digest=str(parent_outcome_digest),
    )


def _authorizations_for_plan(
    connection: Any,
    repository: AuthorizationRepository,
    *,
    plan_id: UUID,
) -> tuple[Authorization, ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            "select id from security.authorization where plan_id = %s order by id",
            (plan_id,),
        )
        ids = tuple(UUID(str(row[0])) for row in cursor.fetchall())
    return tuple(repository.get(item) for item in ids)


def _aggregate_id(connection: Any, *, plan_id: UUID) -> UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            "select id from models.benchmark_aggregate where plan_id = %s",
            (plan_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise PolicyViolation("Campaign benchmark aggregate kaydi bulunamadi")
    return UUID(str(row[0]))


def _existing_benchmark_plan_id(connection: Any, *, plan_digest: str) -> UUID:
    with connection.cursor() as cursor:
        cursor.execute(
            "select id from models.benchmark_plan where plan_digest = %s",
            (plan_digest,),
        )
        row = cursor.fetchone()
    if row is None:
        raise PolicyViolation("Campaign authorize edilmis benchmark plan bulunamadi")
    return UUID(str(row[0]))


def _revoke_issued(
    governance: GovernanceService,
    authorizations: tuple[Authorization, ...],
    *,
    reason: str,
) -> None:
    for authorization in authorizations:
        if authorization.state is AuthorizationState.ISSUED:
            governance.revoke_authorization(authorization.id, reason)


def _assert_terminal_runtime_evidence(
    connection: Any,
    *,
    campaign_id: UUID,
    campaign_digest: str,
    outcome_digest: str,
    outcome_status: CampaignOutcomeStatus | str,
) -> None:
    """Terminal campaign'i exact completed runtime zincirine baglar."""

    effect_digest = digest(
        {
            "campaign_digest": campaign_digest,
            "effect": "campaign-outcome-qualification-ledger",
        }
    )
    status = (
        outcome_status
        if isinstance(outcome_status, CampaignOutcomeStatus)
        else CampaignOutcomeStatus(outcome_status)
    )
    with connection.cursor() as cursor:
        if status is CampaignOutcomeStatus.RECOVERY_REQUIRED:
            cursor.execute(
                "select count(*)"
                " from models.opencode_benchmark_campaign c"
                " join models.opencode_benchmark_campaign_outcome o"
                "   on o.realm_id = c.realm_id and o.campaign_id = c.id"
                "  and o.status = 'recovery-required' and o.outcome_digest = %s"
                " join runtime.job j"
                "   on j.realm_id = c.realm_id and j.work_item_id = c.work_item_id"
                "  and j.plan_id = c.task_plan_id and j.step_id = 'campaign-finalize'"
                "  and j.state = 'recovery-required' and j.max_attempts = 1"
                " join runtime.job_attempt a"
                "   on a.realm_id = j.realm_id and a.job_id = j.id"
                "  and a.outcome = 'recovery-required'"
                " join runtime.effect_claim ec"
                "   on ec.realm_id = j.realm_id and ec.job_id = j.id"
                "  and ec.operation = 'model-campaign-outcome-ledger'"
                "  and ec.effect_digest = %s"
                " join runtime.effect_receipt er"
                "   on er.realm_id = ec.realm_id and er.claim_id = ec.id"
                "  and er.status = 'failed' and er.failure_digest = o.evidence_digest"
                " where c.id = %s and c.campaign_digest = %s"
                "   and not exists (select 1 from runtime.lease l where l.job_id = j.id)"
                "   and not exists (select 1 from runtime.resource_lock l where l.job_id = j.id)"
                "   and not exists ("
                "       select 1 from runtime.claim_without_receipt p where p.job_id = j.id"
                "   )",
                (outcome_digest, effect_digest, campaign_id, campaign_digest),
            )
            count = int(cursor.fetchone()[0])
            if count != 1:
                raise PolicyViolation(
                    "Recovery campaign failed receipt/job cleanup kaniti eksik veya ambiguous"
                )
            return
        cursor.execute(
            "select count(*)"
            " from models.opencode_benchmark_campaign c"
            " join runtime.job j"
            "   on j.realm_id = c.realm_id and j.work_item_id = c.work_item_id"
            "  and j.plan_id = c.task_plan_id and j.step_id = 'campaign-finalize'"
            "  and j.state = 'completed' and j.max_attempts = 1"
            " join runtime.job_attempt a"
            "   on a.realm_id = j.realm_id and a.job_id = j.id"
            "  and a.outcome = 'succeeded' and a.result_digest = %s"
            " join runtime.effect_claim ec"
            "   on ec.realm_id = j.realm_id and ec.job_id = j.id"
            "  and ec.operation = 'model-campaign-outcome-ledger'"
            "  and ec.effect_digest = %s"
            " join runtime.effect_receipt er"
            "   on er.realm_id = ec.realm_id and er.claim_id = ec.id"
            "  and er.status = 'completed' and er.result_digest = %s"
            " join work.checkpoint cp"
            "   on cp.realm_id = c.realm_id and cp.project_id = j.project_id"
            "  and cp.work_item_id = c.work_item_id and cp.task_plan_id = c.task_plan_id"
            "  and cp.job_id = j.id and cp.source_revision = c.source_revision"
            "  and cardinality(cp.pending_steps) = 0"
            "  and 'campaign-finalize' = any(cp.completed_steps)"
            "  and cp.journal_head_digest = %s"
            " where c.id = %s and c.campaign_digest = %s",
            (
                outcome_digest,
                effect_digest,
                outcome_digest,
                outcome_digest,
                campaign_id,
                campaign_digest,
            ),
        )
        count = int(cursor.fetchone()[0])
    if count != 1:
        raise PolicyViolation(
            "Terminal campaign completed job/receipt/checkpoint kaniti eksik veya ambiguous"
        )


def _attempted_provider_call_ids(host: ExecutionHost, job_id: UUID) -> set[str]:
    prefix = "provider-contract:"
    return {
        claim.operation.removeprefix(prefix)
        for claim in host.ledger.claims_for_job(job_id)
        if claim.operation.startswith(prefix)
    }


def _metric_mean(metrics: Mapping[str, Any], name: str) -> float:
    row = metrics.get(name)
    if not isinstance(row, Mapping):
        raise PolicyViolation("Campaign aggregate metric shape gecersiz")
    value = row.get("mean")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PolicyViolation("Campaign aggregate metric mean gecersiz")
    return float(value)


def _resolve_rows(
    connection: Any,
    *,
    campaign_key: str,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "select c.id, c.campaign_digest, c.source_revision, c.source_digest,"
            " c.catalog_digest, c.endpoint_identity_digest, c.inventory_digest,"
            " c.policy_digest, c.fixture_registry_digest, c.verifier_provenance_digest,"
            " o.id, o.status, o.outcome_digest, o.evidence_digest"
            " from models.opencode_benchmark_campaign c"
            " join models.opencode_benchmark_campaign_outcome o"
            "   on o.realm_id = c.realm_id and o.campaign_id = c.id"
            " where c.campaign_key = %s order by c.revision desc, c.created_at desc limit 1",
            (campaign_key,),
        )
        campaign_row = cursor.fetchone()
        if campaign_row is None:
            raise PolicyViolation("Terminal OpenCode benchmark campaign bulunamadi")
        cursor.execute(
            "select m.configured_model_id, m.canonical_model_id, m.modality,"
            " q.evidence_digest, q.aggregate_id, ba.metrics, ba.evidence_digest,"
            " ba.approved, ba.unsafe"
            " from models.opencode_model_qualification_event q"
            " join models.opencode_benchmark_campaign_member m"
            "   on m.realm_id = q.realm_id and m.campaign_id = q.campaign_id"
            "  and m.id = q.member_id"
            " join models.benchmark_aggregate ba"
            "   on ba.realm_id = q.realm_id and ba.id = q.aggregate_id"
            " where q.campaign_id = %s and q.action = 'qualified'"
            " order by m.configured_model_id, m.canonical_model_id",
            (campaign_row[0],),
        )
        qualification_rows = cursor.fetchall()
    campaign = {
        "id": UUID(str(campaign_row[0])),
        "campaign_digest": str(campaign_row[1]),
        "source_revision": str(campaign_row[2]),
        "source_digest": str(campaign_row[3]),
        "catalog_digest": str(campaign_row[4]),
        "endpoint_identity_digest": str(campaign_row[5]),
        "inventory_digest": str(campaign_row[6]),
        "policy_digest": str(campaign_row[7]),
        "fixture_registry_digest": str(campaign_row[8]),
        "verifier_provenance_digest": str(campaign_row[9]),
        "outcome_id": UUID(str(campaign_row[10])),
        "outcome_status": str(campaign_row[11]),
        "outcome_digest": str(campaign_row[12]),
        "outcome_evidence_digest": str(campaign_row[13]),
    }
    rows = tuple(
        {
            "configured_model_id": str(row[0]),
            "canonical_model_id": str(row[1]),
            "modality": str(row[2]),
            "qualification_evidence_digest": str(row[3]),
            "aggregate_id": UUID(str(row[4])),
            "metrics": dict(row[5]),
            "aggregate_evidence_digest": str(row[6]),
            "approved": bool(row[7]),
            "unsafe": bool(row[8]),
        }
        for row in qualification_rows
    )
    return campaign, rows


def resolve_command(
    workload: Annotated[str, typer.Option("--workload")],
    modality: Annotated[str | None, typer.Option("--modality")] = None,
    client: Annotated[str, typer.Option("--client")] = "opencode",
    config_file: Annotated[Path | None, typer.Option("--config")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """En guncel current qualification kanitindan deterministik model secer."""

    del output_json
    try:
        if client != "opencode" or not workload.strip():
            raise PolicyViolation("Campaign resolve yalniz opencode ve exact workload destekler")
        discovery, manifest = _load_manifest(config_file=config_file, scope_file=scope_file)
        _, source_revision = _source_revision()
        workload_by_model = {item.canonical_model_id: item.workload for item in discovery.targets}
        with RealmSession(home, realm) as context:
            campaign, rows = _resolve_rows(
                context.connection,
                campaign_key="opencode-aihub",
            )
            _assert_terminal_runtime_evidence(
                context.connection,
                campaign_id=campaign["id"],
                campaign_digest=campaign["campaign_digest"],
                outcome_digest=campaign["outcome_digest"],
                outcome_status=campaign["outcome_status"],
            )
            current_policy = GovernanceService(context.connection, context.realm).policies.current(
                DEFAULT_POLICY_NAME
            )
            if current_policy is None:
                raise PolicyViolation("Campaign resolve current policy bulamadi")
            expected = {
                "source_revision": source_revision,
                "source_digest": manifest.manifest_digest,
                "catalog_digest": digest(discovery.catalog.sanitized()),
                "endpoint_identity_digest": discovery.catalog.endpoint_identity_digest,
                "inventory_digest": discovery.inventory_digest,
                "policy_digest": current_policy.policy_digest,
                "fixture_registry_digest": discovery.fixture_registry_digest,
                "verifier_provenance_digest": discovery.verifier_provenance_digest,
            }
            stale = sorted(key for key, value in expected.items() if campaign.get(key) != value)
            if stale:
                raise PolicyViolation(
                    "Campaign qualification current digest binding stale: " + ",".join(stale)
                )
            if campaign["outcome_status"] == CampaignOutcomeStatus.RECOVERY_REQUIRED.value:
                raise PolicyViolation("Recovery-required campaign model secimine acilamaz")
            ranked: list[dict[str, Any]] = []
            for row in rows:
                model_id = str(row["canonical_model_id"])
                if workload_by_model.get(model_id) != workload:
                    continue
                if modality is not None and row["modality"] != modality:
                    continue
                if not row["approved"] or row["unsafe"]:
                    continue
                metrics = row["metrics"]
                if not isinstance(metrics, Mapping):
                    raise PolicyViolation("Campaign aggregate metrics object olmali")
                quality = _metric_mean(metrics, "quality")
                reliability = _metric_mean(metrics, "reliability")
                latency_ms = _metric_mean(metrics, "latency_ms")
                token_count = _metric_mean(metrics, "token_count")
                cost = _metric_mean(metrics, "cost")
                latency_efficiency = 1.0 / (1.0 + latency_ms / 1000.0)
                token_efficiency = 1.0 / (1.0 + token_count / 4096.0)
                cost_efficiency = 1.0 / (1.0 + cost)
                score = (
                    0.45 * quality
                    + 0.30 * reliability
                    + 0.10 * latency_efficiency
                    + 0.08 * token_efficiency
                    + 0.07 * cost_efficiency
                )
                ranked.append(
                    {
                        "canonical_model_id": model_id,
                        "configured_model_id": row["configured_model_id"],
                        "modality": row["modality"],
                        "workload": workload,
                        "score": score,
                        "quality": quality,
                        "reliability": reliability,
                        "latency_ms": latency_ms,
                        "token_count": token_count,
                        "cost": cost,
                        "aggregate_id": str(row["aggregate_id"]),
                        "evidence_digests": [
                            row["qualification_evidence_digest"],
                            row["aggregate_evidence_digest"],
                            campaign["outcome_evidence_digest"],
                        ],
                    }
                )
            ranked.sort(key=lambda item: (-float(item["score"]), str(item["canonical_model_id"])))
            if not ranked:
                raise PolicyViolation("Workload icin current qualified model bulunamadi")
            selection_evidence_digest = digest(
                {
                    "campaign_digest": campaign["campaign_digest"],
                    "outcome_digest": campaign["outcome_digest"],
                    "workload": workload,
                    "client": client,
                    "modality": modality,
                    "ranking": [
                        {
                            "model_id": item["canonical_model_id"],
                            "score": item["score"],
                            "evidence": item["evidence_digests"],
                        }
                        for item in ranked
                    ],
                }
            )
            document = {
                "schema": "zekam-model-campaign-resolution/v1",
                "campaign_id": str(campaign["id"]),
                "campaign_digest": campaign["campaign_digest"],
                "workload": workload,
                "client": client,
                "modality": modality,
                "selected_model_id": ranked[0]["canonical_model_id"],
                "selected_configured_model_id": ranked[0]["configured_model_id"],
                "selected_score": ranked[0]["score"],
                "candidates": ranked,
                "selection_evidence_digest": selection_evidence_digest,
                "decision_recorded": False,
                "provider_calls_made": 0,
                "network_calls_made": 0,
                "grants_authority": False,
            }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


app.command("resolve")(resolve_command)


@app.command("plan")
def plan_command(
    revision: Annotated[int, typer.Option("--revision", min=1)] = 1,
    config_file: Annotated[Path | None, typer.Option("--config")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope")] = None,
    output_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Exact configured-model snapshot ve call budgetini salt okunur hazirlar."""

    del output_json
    try:
        discovery, manifest = _load_manifest(config_file=config_file, scope_file=scope_file)
        source_head, source_revision = _source_revision()
        document = manifest.sanitized() | {
            "campaign_key": "opencode-aihub",
            "campaign_revision": revision,
            "source_head": source_head,
            "source_revision": source_revision,
            "authority_records_created": 0,
            "provider_calls_made": 0,
            "network_calls_made": 0,
            "audio_provider_calls_made": 0,
        }
        if discovery.provider_call_budget != 102:
            raise PolicyViolation("Reviewed campaign exact 102 provider call ister")
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("authorize")
def authorize_command(
    project_id: Annotated[UUID, typer.Option("--project-uuid")],
    work_id: Annotated[UUID, typer.Option("--work")],
    actor_id: Annotated[UUID, typer.Option("--actor")],
    continue_from: Annotated[UUID | None, typer.Option("--continue-from")] = None,
    revision: Annotated[int, typer.Option("--revision", min=1)] = 1,
    config_file: Annotated[Path | None, typer.Option("--config")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Exact TaskPlan, campaign ve one-shot authorization setini kaydeder."""

    del output_json
    if not apply:
        raise fail("Campaign authorize --uygula ister; provider cagrisi yapmaz", 64)
    try:
        discovery, manifest = _load_manifest(config_file=config_file, scope_file=scope_file)
        if manifest.discovery.provider_call_budget != 102:
            raise PolicyViolation("Campaign exact provider call budget drift")
        credential = environment_value(os.environ, manifest.credential_locator)
        if credential is None or not credential.strip():
            raise PolicyViolation("OpenCode credential locator hazir degil")
        source_head, source_revision = _source_revision()
        with RealmSession(home, realm) as context:
            graph = WorkGraphService(context.connection, context.realm, actor_id=actor_id)
            work = graph.items.get(work_id)
            if work.project_id != project_id:
                raise PolicyViolation("Campaign Work/project binding mismatch")
            governance = GovernanceService(context.connection, context.realm, actor_id=actor_id)
            base_policy = governance.policies.current(DEFAULT_POLICY_NAME)
            if base_policy is None:
                raise PolicyViolation("Kanonik varsayilan policy bulunamadi")
            campaign_repository = ModelCampaignRepository(context.connection, context.realm_id)
            continuation_runtime = (
                None
                if continue_from is None
                else _continuation_runtime(
                    context.connection,
                    campaign_repository,
                    parent_campaign_id=continue_from,
                    manifest=manifest,
                    work_id=work_id,
                    revision=revision,
                    current_source_revision=source_revision,
                    current_policy_digest=base_policy.policy_digest,
                )
            )
            active_calls = (
                manifest.calls
                if continuation_runtime is None
                else continuation_runtime.active_calls
            )
            execution_manifest = (
                manifest.execution_manifest
                if continuation_runtime is None
                else ProviderExecutionManifest(
                    binding_set_digest=digest(
                        {
                            "discovery": discovery.discovery_digest,
                            "continuation": (
                                continuation_runtime.continuation.continuation_provenance_digest
                            ),
                        }
                    ),
                    fixture_digest=discovery.fixture_registry_digest,
                    calls=tuple(item.prepared.plan for item in active_calls),
                )
            )
            candidate_policy = (
                build_provider_policy_candidate(base_policy, execution_manifest)
                if continuation_runtime is None
                else base_policy
            )
            secret_repository = SecretRefRepository(context.connection, context.realm_id)
            secret_ref = secret_repository.current_by_name(BENCHMARK_SECRET_REF_NAME)
            if secret_ref is not None and (
                secret_ref.provider != discovery.scope.provider_id
                or secret_ref.store_backend is not SecretBackend.ENVIRONMENT
                or secret_ref.store_locator != manifest.credential_locator
                or not secret_ref.is_usable()
            ):
                raise PolicyViolation("Campaign SecretRef metadata drift")

            with context.connection.transaction():
                if secret_ref is None:
                    secret_ref = SecretRef.create(
                        realm_id=context.realm_id,
                        name=BENCHMARK_SECRET_REF_NAME,
                        provider=discovery.scope.provider_id,
                        purpose="public OpenCode AIHub benchmark campaign",
                        allowed_operations=("chat-completions", "embeddings", "rerank"),
                        store_backend=SecretBackend.ENVIRONMENT,
                        store_locator=manifest.credential_locator,
                    )
                    secret_repository.add(secret_ref)
                if continuation_runtime is None:
                    governance.policies.append(candidate_policy)
                task_plan = graph.create_plan(
                    work_id,
                    source_revision=source_revision,
                    policy_digest=candidate_policy.policy_digest,
                    steps=_campaign_steps(
                        manifest,
                        project_id=project_id,
                        work_id=work_id,
                        active_calls=active_calls,
                        suite_version=1,
                    ),
                )
                campaign = _domain_campaign(
                    discovery,
                    manifest,
                    work_id=work_id,
                    task_plan_id=task_plan.id,
                    source_revision=source_revision,
                    policy_digest=candidate_policy.policy_digest,
                    revision=revision,
                    continuation=None
                    if continuation_runtime is None
                    else continuation_runtime.continuation,
                )
                campaign_id, campaign_created = (
                    campaign_repository.ensure_campaign(campaign)
                    if continuation_runtime is None
                    else campaign_repository.ensure_continuation_campaign(campaign)
                )
                authorization_repository = AuthorizationRepository(
                    context.connection, context.realm_id
                )
                provider_authorizations: list[tuple[str, Authorization]] = []
                for call in active_calls:
                    plan = call.prepared.plan
                    authorization = Authorization.issue(
                        realm_id=context.realm_id,
                        actor_id=actor_id,
                        work_item_id=work_id,
                        plan_id=task_plan.id,
                        plan_digest=plan.authorization_plan_digest,
                        effect_digest=plan.effect_request.effect_digest,
                        scope=AuthorizationScope(
                            allowed_resources=(plan.target, plan.call_resource),
                            allowed_effects=(EffectKind.PROVIDER_CALL.value,),
                            provider_refs=(plan.provider_ref,),
                            secret_ref_ids=(secret_ref.id,),
                            data_classifications=(DataClassification.PUBLIC,),
                        ),
                        risk="critical",
                        lifetime=dt.timedelta(hours=4),
                    )
                    authorization_repository.issue(authorization)
                    provider_authorizations.append((call.call_id, authorization))

                registry = load_fixture_registry()
                benchmark_repository = BenchmarkRepository(context.connection, context.realm_id)
                member_authorizations: list[tuple[str, UUID, Authorization]] = []
                for member_record in campaign_repository.list_members(campaign_id):
                    member = member_record.member
                    if member.disposition is CampaignMemberDisposition.EXCLUDED_AUDIO:
                        continue
                    suite, benchmark_plan = _benchmark_plan(campaign, member)
                    benchmark_plan_id, _ = benchmark_repository.ensure_plan(
                        registry=registry, suite=suite, plan=benchmark_plan
                    )
                    member_resource = (
                        f"model-benchmark:{member.canonical_model_id}:"
                        f"{suite.suite_digest.removeprefix('sha256:')}"
                    )
                    effect_digest = digest(
                        {
                            "campaign_digest": campaign.campaign_digest,
                            "member_id": member_record.id,
                            "benchmark_plan_digest": benchmark_plan.plan_digest,
                            "effect": "benchmark-ledger-write",
                        }
                    )
                    authorization = Authorization.issue(
                        realm_id=context.realm_id,
                        actor_id=actor_id,
                        work_item_id=work_id,
                        plan_id=task_plan.id,
                        plan_digest=benchmark_plan.plan_digest,
                        effect_digest=effect_digest,
                        scope=AuthorizationScope(
                            allowed_resources=(
                                member_resource,
                                f"model-benchmark:{member.canonical_model_id}:campaign-ledger",
                            ),
                            allowed_effects=(EffectKind.DATABASE_WRITE.value,),
                            data_classifications=(DataClassification.PUBLIC,),
                        ),
                        risk="high",
                        lifetime=dt.timedelta(hours=4),
                    )
                    authorization_repository.issue(authorization)
                    member_authorizations.append(
                        (str(member.canonical_model_id), benchmark_plan_id, authorization)
                    )
                campaign_authorization = Authorization.issue(
                    realm_id=context.realm_id,
                    actor_id=actor_id,
                    work_item_id=work_id,
                    plan_id=task_plan.id,
                    plan_digest=campaign.campaign_digest,
                    effect_digest=digest(
                        {
                            "campaign_digest": campaign.campaign_digest,
                            "effect": "campaign-outcome-qualification-ledger",
                        }
                    ),
                    scope=AuthorizationScope(
                        allowed_resources=(f"work:{project_id}:{work_id}",),
                        allowed_effects=(EffectKind.DATABASE_WRITE.value,),
                        data_classifications=(DataClassification.PUBLIC,),
                    ),
                    risk="high",
                    lifetime=dt.timedelta(hours=4),
                )
                authorization_repository.issue(campaign_authorization)

        authority_set_digest = digest(
            {
                "provider": [item.authorization_digest for _, item in provider_authorizations],
                "member": [item.authorization_digest for _, _, item in member_authorizations],
                "campaign": campaign_authorization.authorization_digest,
            }
        )
        document: dict[str, Any] = {
            "schema": "zekam-opencode-benchmark-authorization-set/v1",
            "campaign_id": str(campaign_id),
            "campaign_created": campaign_created,
            "campaign_digest": campaign.campaign_digest,
            "campaign_revision": campaign.revision,
            "project_id": str(project_id),
            "work_id": str(work_id),
            "task_plan_id": str(task_plan.id),
            "task_plan_digest": task_plan.plan_digest,
            "source_head": source_head,
            "source_revision": source_revision,
            "policy_digest": candidate_policy.policy_digest,
            "manifest_digest": manifest.manifest_digest,
            "parent_campaign_id": None if continue_from is None else str(continue_from),
            "continuation_provenance_digest": None
            if continuation_runtime is None
            else continuation_runtime.continuation.continuation_provenance_digest,
            "maximum_current_provider_calls": len(active_calls),
            "provider_authorization_count": len(provider_authorizations),
            "member_authorization_count": len(member_authorizations),
            "campaign_authorization_count": 1,
            "authorization_set_digest": authority_set_digest,
            "max_uses_per_provider_call": 1,
            "provider_calls_made": 0,
            "network_calls_made": 0,
            "audio_provider_calls_made": 0,
            "grants_authority": False,
        }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("status")
def status_command(
    campaign_id: Annotated[UUID | None, typer.Option("--campaign-id")] = None,
    campaign_key: Annotated[str, typer.Option("--campaign-key")] = "opencode-aihub",
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Kampanya durumunu provider cagrisi yapmadan raporlar."""

    del output_json
    try:
        with RealmSession(home, realm) as context:
            repository = ModelCampaignRepository(context.connection, context.realm_id)
            status = (
                repository.status(campaign_id)
                if campaign_id is not None
                else repository.latest_terminal(campaign_key)
            )
            document = {
                "schema": "zekam-opencode-benchmark-campaign-status/v1",
                "found": status is not None,
                "status": None
                if status is None
                else {
                    "campaign_id": str(status.campaign_id),
                    "campaign_key": status.campaign_key,
                    "revision": status.revision,
                    "campaign_digest": status.campaign_digest,
                    "terminal": status.terminal,
                    "outcome_id": None if status.outcome_id is None else str(status.outcome_id),
                    "outcome_status": None
                    if status.outcome_status is None
                    else status.outcome_status.value,
                    "tested_call_budget": status.tested_call_budget,
                    "provider_call_budget": status.provider_call_budget,
                    "actual_tested_call_count": status.actual_tested_call_count,
                    "actual_provider_call_count": status.actual_provider_call_count,
                    "parent_campaign_id": None
                    if status.parent_campaign_id is None
                    else str(status.parent_campaign_id),
                    "current_tested_call_budget": status.current_tested_call_budget,
                    "current_provider_call_budget": status.current_provider_call_budget,
                    "continuation_provenance_digest": status.continuation_provenance_digest,
                },
                "provider_calls_made": 0,
                "network_calls_made": 0,
                "grants_authority": False,
            }
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))


@app.command("run")
def run_command(
    campaign_id: Annotated[UUID, typer.Option("--campaign-id")],
    project_id: Annotated[UUID, typer.Option("--project-uuid")],
    work_id: Annotated[UUID, typer.Option("--work")],
    plan_id: Annotated[UUID, typer.Option("--plan-id")],
    continue_from: Annotated[UUID | None, typer.Option("--continue-from")] = None,
    revision: Annotated[int, typer.Option("--revision", min=1)] = 1,
    config_file: Annotated[Path | None, typer.Option("--config")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    output_json: Annotated[bool, typer.Option("--json")] = False,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
) -> None:
    """Onceden issued exact authority setini bir kez tuketip kampanyayi yurutur."""

    del output_json
    if not apply:
        raise fail("Campaign run --uygula ve exact campaign/Work/plan ister", 64)
    try:
        discovery, manifest = _load_manifest(config_file=config_file, scope_file=scope_file)
        if manifest.discovery.provider_call_budget != 102:
            raise PolicyViolation("Campaign run exact 102 provider call manifesti ister")
        credential = environment_value(os.environ, manifest.credential_locator)
        if credential is None or not credential.strip():
            raise PolicyViolation("OpenCode credential locator run oncesi hazir degil")
        _, source_revision = _source_revision()
        registry = load_fixture_registry()
        fixtures_by_digest = {item.fixture_digest: item for item in registry.fixtures}
        fixture_root = default_fixture_file().parent.resolve(strict=True)

        with RealmSession(home, realm) as context:
            graph = WorkGraphService(context.connection, context.realm)
            work_item = graph.items.get(work_id)
            if work_item.project_id != project_id:
                raise PolicyViolation("Campaign run Work/project binding mismatch")
            governance = GovernanceService(context.connection, context.realm)
            current_policy = governance.policies.current(DEFAULT_POLICY_NAME)
            if current_policy is None:
                raise PolicyViolation("Campaign current policy bulunamadi")
            campaign_repository = ModelCampaignRepository(context.connection, context.realm_id)
            continuation_runtime = (
                None
                if continue_from is None
                else _continuation_runtime(
                    context.connection,
                    campaign_repository,
                    parent_campaign_id=continue_from,
                    manifest=manifest,
                    work_id=work_id,
                    revision=revision,
                    current_source_revision=source_revision,
                    current_policy_digest=current_policy.policy_digest,
                )
            )
            active_calls = (
                manifest.calls
                if continuation_runtime is None
                else continuation_runtime.active_calls
            )
            task_plan = graph.assert_plan_is_current(
                work_id,
                source_revision=source_revision,
                policy_digest=current_policy.policy_digest,
            )
            if task_plan.id != plan_id or task_plan.steps != _campaign_steps(
                manifest,
                project_id=project_id,
                work_id=work_id,
                active_calls=active_calls,
                suite_version=1,
            ):
                raise PolicyViolation("Campaign exact TaskPlan/manifest binding mismatch")
            campaign = _domain_campaign(
                discovery,
                manifest,
                work_id=work_id,
                task_plan_id=task_plan.id,
                source_revision=source_revision,
                policy_digest=current_policy.policy_digest,
                revision=revision,
                continuation=None
                if continuation_runtime is None
                else continuation_runtime.continuation,
            )
            existing_status = campaign_repository.status(campaign_id)
            if (
                existing_status.campaign_digest != campaign.campaign_digest
                or existing_status.campaign_key != campaign.campaign_key
                or existing_status.revision != campaign.revision
            ):
                raise PolicyViolation("Campaign run authorize edilmis exact campaign ister")
            if existing_status.terminal:
                if existing_status.outcome_status is None:
                    raise PolicyViolation("Terminal campaign outcome status ister")
                if existing_status.outcome_digest is None:
                    raise PolicyViolation("Terminal campaign outcome digest ister")
                _assert_terminal_runtime_evidence(
                    context.connection,
                    campaign_id=campaign_id,
                    campaign_digest=campaign.campaign_digest,
                    outcome_digest=existing_status.outcome_digest,
                    outcome_status=existing_status.outcome_status,
                )
                document = {
                    "schema": "zekam-opencode-benchmark-campaign-run/v1",
                    "status": existing_status.outcome_status.value,
                    "campaign_id": str(campaign_id),
                    "campaign_digest": existing_status.campaign_digest,
                    "replay": True,
                    "provider_calls_made": 0,
                    "network_calls_made": 0,
                    "grants_authority": False,
                }
                console.print_json(json.dumps(document, ensure_ascii=False, default=str))
                return

            secret_repository = SecretRefRepository(context.connection, context.realm_id)
            secret_ref = secret_repository.current_by_name(BENCHMARK_SECRET_REF_NAME)
            if secret_ref is None:
                raise PolicyViolation("Campaign exact SecretRef metadata bulunamadi")
            authorization_repository = AuthorizationRepository(context.connection, context.realm_id)
            all_authorizations = _authorizations_for_plan(
                context.connection,
                authorization_repository,
                plan_id=plan_id,
            )
            provider_by_digest = {
                item.plan_digest: item
                for item in all_authorizations
                if item.scope.allowed_effects == (EffectKind.PROVIDER_CALL.value,)
            }
            member_by_digest = {
                item.plan_digest: item
                for item in all_authorizations
                if item.scope.allowed_effects == (EffectKind.DATABASE_WRITE.value,)
            }
            if len(provider_by_digest) != len(active_calls) or len(member_by_digest) != 18:
                raise PolicyViolation(
                    "Campaign exact active provider + 17 member + 1 campaign authority ister"
                )
            campaign_authorization = member_by_digest.get(campaign.campaign_digest)
            if campaign_authorization is None or (
                campaign_authorization.work_item_id != work_id
                or campaign_authorization.plan_id != plan_id
                or campaign_authorization.state is not AuthorizationState.ISSUED
            ):
                raise PolicyViolation("Campaign outcome authorization eksik/stale")
            exact_provider_authorizations: dict[str, Authorization] = {}
            for call in active_calls:
                authorization = provider_by_digest.get(call.prepared.plan.authorization_plan_digest)
                if authorization is None:
                    raise PolicyViolation("Campaign provider authorization seti eksik")
                if (
                    authorization.work_item_id != work_id
                    or authorization.plan_id != plan_id
                    or authorization.state is not AuthorizationState.ISSUED
                ):
                    raise PolicyViolation("Campaign provider authorization stale/tuketilmis")
                verify_exact_provider_authorization(
                    call.prepared,
                    authorization,
                    secret_ref,
                )
                exact_provider_authorizations[call.call_id] = authorization

            member_records = {
                item.member.canonical_model_id: item
                for item in campaign_repository.list_members(campaign_id)
                if item.member.canonical_model_id is not None
                and item.member.disposition is CampaignMemberDisposition.HEALTH_PENDING
            }
            benchmark_plans: dict[str, tuple[BenchmarkSuite, BenchmarkPlan, UUID]] = {}
            benchmark_repository = BenchmarkRepository(context.connection, context.realm_id)
            for model_id, member_record in member_records.items():
                suite, benchmark_plan = _benchmark_plan(campaign, member_record.member)
                benchmark_plan_id = _existing_benchmark_plan_id(
                    context.connection,
                    plan_digest=benchmark_plan.plan_digest,
                )
                member_authorization = member_by_digest.get(benchmark_plan.plan_digest)
                if member_authorization is None or (
                    member_authorization.work_item_id != work_id
                    or member_authorization.plan_id != plan_id
                    or member_authorization.state is not AuthorizationState.ISSUED
                ):
                    raise PolicyViolation("Campaign member authorization eksik/stale")
                benchmark_plans[model_id] = (suite, benchmark_plan, benchmark_plan_id)

            client = AuthorizedProviderClient(
                governance,
                _CampaignEndpointResolver(manifest.endpoint_mapping),
                SecretBroker(
                    {
                        SecretBackend.ENVIRONMENT: OpenCodeCredentialStore(
                            provider_id=discovery.scope.provider_id,
                            credential_locator=manifest.credential_locator,
                        )
                    }
                ),
                UrllibJsonProviderTransport(),
            )
            capability = f"provider.campaign.{campaign.campaign_digest[-16:]}"
            host = ExecutionHost(
                context.connection,
                context.realm_id,
                worker_label="opencode-aihub-campaign",
            )
            job, job_created = host.jobs.enqueue(
                Job.create(
                    realm_id=context.realm_id,
                    project_id=project_id,
                    kind=JobKind.PROVIDER_CALL,
                    idempotency_key=f"opencode-aihub-campaign:{campaign.campaign_digest}",
                    resources=parse_requests(
                        write=tuple(item.prepared.plan.call_resource for item in active_calls)
                        + tuple(
                            f"model-benchmark:{model_id}:campaign-ledger"
                            for model_id in member_records
                        )
                        + tuple(
                            f"model-benchmark:{model_id}:"
                            f"{suite.suite_digest.removeprefix('sha256:')}"
                            for model_id, (suite, _, _) in benchmark_plans.items()
                        )
                        + (f"work:{project_id}:{work_id}",)
                    ),
                    required_capabilities=(capability,),
                    max_attempts=1,
                    work_item_id=work_id,
                    plan_id=plan_id,
                    step_id="campaign-finalize",
                )
            )
            if not job_created:
                raise PolicyViolation("Campaign runtime replay yeni provider cagrisi yapamaz")
            claimed = host.acquire_work(capabilities=(capability,))
            if claimed is None or claimed.job.id != job.id:
                raise PolicyViolation("Campaign runtime job claim edilemedi")
            runner = RuntimeProviderContractRunner(
                host=host,
                work=claimed,
                client=client,
                defer_job_recovery=True,
            )
            campaign_claim = host.claim_effect(
                claimed,
                operation="model-campaign-outcome-ledger",
                effect_digest=campaign_authorization.effect_digest,
                authorization_digest=campaign_authorization.authorization_digest,
                authorization_id=campaign_authorization.id,
                idempotency_key=campaign.campaign_digest,
                resources=parse_requests(write=(f"work:{project_id}:{work_id}",)),
                adapter_digest=digest({"adapter": "opencode-campaign-ledger", "version": 1}),
            )
            consumed_campaign = authorization_repository.consume(
                campaign_authorization.id,
                effect_digest=campaign_authorization.effect_digest,
                consumed_by="cli:model-campaign:outcome-ledger",
            )
            if not consumed_campaign.consumed:
                raise PolicyViolation("Campaign outcome authorization tuketilemedi")
            benchmark_service = BenchmarkExecutionService(benchmark_repository, registry)
            evaluator = DeterministicProviderNeutralVerifier()
            calls_by_model: dict[str, list[Any]] = {}
            for call in active_calls:
                calls_by_model.setdefault(call.canonical_model_id, []).append(call)

            executed_call_ids: set[str] = set()
            call_evidence: dict[str, str] = {}
            member_result_digests: dict[str, str] = {}
            aggregate_ids: dict[str, UUID] = {}
            failed_models: dict[str, str] = {}
            terminal_models: set[str] = set()
            actual_provider_calls = 0
            actual_tested_calls = 0
            current_model_id: str | None = None
            current_member_plan_id: UUID | None = None
            try:
                for model_id, member_record in member_records.items():
                    current_model_id = model_id
                    current_member_plan_id = None
                    suite, benchmark_plan, benchmark_plan_id = benchmark_plans[model_id]
                    member_authorization = member_by_digest[benchmark_plan.plan_digest]
                    member_resource = f"model-benchmark:{model_id}:campaign-ledger"
                    member_claim = host.claim_effect(
                        claimed,
                        operation="model-campaign-member-ledger",
                        effect_digest=member_authorization.effect_digest,
                        authorization_digest=member_authorization.authorization_digest,
                        authorization_id=member_authorization.id,
                        idempotency_key=digest(
                            {
                                "campaign": campaign.campaign_digest,
                                "member": model_id,
                            }
                        ),
                        resources=parse_requests(write=(member_resource,)),
                        adapter_digest=digest(
                            {"adapter": "opencode-campaign-member-ledger", "version": 1}
                        ),
                    )
                    consumed = authorization_repository.consume(
                        member_authorization.id,
                        effect_digest=member_authorization.effect_digest,
                        consumed_by=f"cli:model-campaign:member:{model_id}",
                    )
                    if not consumed.consumed or consumed.authorization is None:
                        raise PolicyViolation("Campaign member authorization tuketilemedi")
                    if (
                        continuation_runtime is not None
                        and model_id in continuation_runtime.adopted_results
                    ):
                        terminal_result: CampaignMemberResult | None = None
                        source_results = sorted(
                            continuation_runtime.adopted_results[model_id],
                            key=lambda item: (
                                0 if item.stage is CampaignMemberResultStage.HEALTH else 1
                            ),
                        )
                        for source_result in source_results:
                            adoption_provenance = digest(
                                {
                                    "schema": "zekam-opencode-result-adoption/v1",
                                    "continuation": (
                                        continuation_runtime.continuation.continuation_provenance_digest
                                    ),
                                    "parent_result_id": source_result.id,
                                    "parent_result_digest": source_result.result_digest,
                                    "model_id": model_id,
                                }
                            )
                            adopted_result = CampaignMemberResult(
                                stage=source_result.stage,
                                status=source_result.status,
                                evidence_digest=source_result.evidence_digest,
                                actual_tested_call_count=0,
                                actual_provider_call_count=0,
                                aggregate_id=source_result.aggregate_id,
                                failure_category=source_result.failure_category,
                                adoption=ResultAdoption(
                                    adopted_from_result_id=source_result.id,
                                    adoption_provenance_digest=adoption_provenance,
                                ),
                            )
                            campaign_repository.record_adopted_result(
                                campaign_id=campaign_id,
                                member_id=member_record.id,
                                result=adopted_result,
                            )
                            terminal_result = adopted_result
                            if (
                                adopted_result.stage is CampaignMemberResultStage.BENCHMARK
                                and adopted_result.status is CampaignMemberResultStatus.PASSED
                                and adopted_result.aggregate_id is not None
                            ):
                                aggregate_ids[model_id] = adopted_result.aggregate_id
                            if adopted_result.status is CampaignMemberResultStatus.FAILED:
                                if adopted_result.failure_category is None:
                                    raise PolicyViolation("Adopted failed result category ister")
                                failed_models[model_id] = adopted_result.failure_category
                        if terminal_result is None:
                            raise PolicyViolation("Continuation adopted terminal result eksik")
                        member_result_digests[model_id] = terminal_result.result_digest
                        host.record_success(
                            member_claim,
                            result_digest=terminal_result.result_digest,
                            adapter_evidence_digest=terminal_result.evidence_digest,
                        )
                        terminal_models.add(model_id)
                        continue

                    if (
                        continuation_runtime is not None
                        and model_id == continuation_runtime.recovered_health.model_id
                    ):
                        recovery = continuation_runtime.recovered_health
                        recovery_provenance = digest(
                            {
                                "schema": "zekam-opencode-health-projection-recovery/v1",
                                "continuation": (
                                    continuation_runtime.continuation.continuation_provenance_digest
                                ),
                                "call_id": recovery.call_id,
                                "claim_id": recovery.claim_id,
                                "receipt_id": recovery.receipt_id,
                            }
                        )
                        recovered_result = CampaignMemberResult(
                            stage=CampaignMemberResultStage.HEALTH,
                            status=CampaignMemberResultStatus.FAILED,
                            evidence_digest=recovery.receipt_evidence_digest,
                            actual_tested_call_count=0,
                            actual_provider_call_count=0,
                            failure_category="health-contract-failed",
                            recovery_evidence=ResultRecoveryEvidence(
                                recovered_from_claim_id=recovery.claim_id,
                                recovered_from_receipt_id=recovery.receipt_id,
                                recovery_provenance_digest=recovery_provenance,
                            ),
                        )
                        campaign_repository.record_recovered_health_failure(
                            campaign_id=campaign_id,
                            member_id=member_record.id,
                            result=recovered_result,
                        )
                        failed_models[model_id] = "health-contract-failed"
                        member_result_digests[model_id] = recovered_result.result_digest
                        host.record_success(
                            member_claim,
                            result_digest=recovered_result.result_digest,
                            adapter_evidence_digest=recovered_result.evidence_digest,
                        )
                        terminal_models.add(model_id)
                        continue

                    member_calls = calls_by_model[model_id]
                    health_call = next(
                        item for item in member_calls if item.kind is CampaignCallKind.HEALTH
                    )
                    fixture = fixtures_by_digest[health_call.fixture_digest]
                    artifact = load_remote_fixture(fixture, allow_root=fixture_root)
                    started = time.perf_counter()
                    health_execution = runner.invoke(
                        health_call.prepared,
                        secret_ref=secret_ref,
                        authorization=exact_provider_authorizations[health_call.call_id],
                        consumed_by=f"cli:model-campaign:health:{health_call.call_id}",
                    )
                    latency_ms = max(0, round((time.perf_counter() - started) * 1000))
                    actual_provider_calls += 1
                    executed_call_ids.add(health_call.call_id)
                    health_receipt_evidence = health_execution.receipt.adapter_evidence_digest
                    if health_receipt_evidence is None:
                        raise PolicyViolation("Campaign health receipt evidence ister")
                    call_evidence[health_call.call_id] = health_receipt_evidence
                    health_response = _remote_response(
                        health_call,
                        raw_response=health_execution.provider_result.response,
                        artifact=artifact,
                        latency_ms=latency_ms,
                    )
                    health_evaluation = evaluator.verify(artifact, health_response.payload)
                    health_evidence = digest(
                        {
                            "call_id": health_call.call_id,
                            "receipt_id": health_execution.receipt.id,
                            "response_digest": health_response.response_digest,
                            "evaluation": health_evaluation.evidence_body(),
                            "verifier_provenance": evaluator.provenance_digest,
                        }
                    )
                    health_status = (
                        CampaignMemberResultStatus.PASSED
                        if health_evaluation.approved
                        else CampaignMemberResultStatus.FAILED
                    )
                    campaign_repository.record_member_result(
                        campaign_id=campaign_id,
                        member_id=member_record.id,
                        member_plan_id=None,
                        result=CampaignMemberResult(
                            stage=CampaignMemberResultStage.HEALTH,
                            status=health_status,
                            evidence_digest=health_evidence,
                            actual_tested_call_count=0,
                            actual_provider_call_count=1,
                            failure_category=(
                                None if health_evaluation.approved else "health-contract-failed"
                            ),
                        ),
                    )
                    if not health_evaluation.approved:
                        failed_models[model_id] = "health-contract-failed"
                        for call in member_calls:
                            if call.kind is CampaignCallKind.BENCHMARK:
                                governance.revoke_authorization(
                                    exact_provider_authorizations[call.call_id].id,
                                    "health-failed-benchmark-not-run",
                                )
                                call_evidence[call.call_id] = digest(
                                    {
                                        "status": "not-run-health-failed",
                                        "model_id": model_id,
                                        "call_id": call.call_id,
                                    }
                                )
                        governance.revoke_authorization(
                            member_authorization.id, "health-failed-member-ledger-not-run"
                        )
                        member_result_digests[model_id] = health_evidence
                        host.record_success(
                            member_claim,
                            result_digest=health_evidence,
                            adapter_evidence_digest=health_evidence,
                        )
                        terminal_models.add(model_id)
                        continue

                    authorization_manifest_digest = digest(
                        {
                            "member_authorization": consumed.authorization.authorization_digest,
                            "provider_authorizations": [
                                exact_provider_authorizations[item.call_id].authorization_digest
                                for item in member_calls
                            ],
                        }
                    )
                    member_plan = CampaignMemberPlan(
                        benchmark_plan_id=benchmark_plan_id,
                        benchmark_plan_digest=benchmark_plan.plan_digest,
                        health_evidence_digest=health_evidence,
                        authorization_manifest_digest=authorization_manifest_digest,
                        tested_call_budget=member_record.tested_call_budget,
                        provider_call_budget=member_record.provider_call_budget,
                    )
                    member_plan_id, _ = campaign_repository.store_member_plan(
                        campaign_id=campaign_id,
                        member_id=member_record.id,
                        plan=member_plan,
                    )
                    current_member_plan_id = member_plan_id
                    gateway = RuntimeBenchmarkClaimGateway(
                        host=host,
                        work=claimed,
                        authorization=consumed.authorization,
                        adapter_digest=digest(
                            {"adapter": "opencode-aihub-campaign", "model_id": model_id}
                        ),
                    )
                    store = ProcessMemoryResponseStore()
                    verifier = OpenCodeDeterministicBenchmarkVerifier(
                        identity=VerifierIdentity(
                            model_id=discovery.scope.verifier.model_id,
                            execution_identity=discovery.scope.verifier.execution_identity,
                            provenance_digest=discovery.verifier_provenance_digest,
                        ),
                        fixture_root=fixture_root,
                        response_store=store,
                    )
                    trials: list[TrialResult] = []
                    benchmark_calls = sorted(
                        (item for item in member_calls if item.kind is CampaignCallKind.BENCHMARK),
                        key=lambda item: (item.fixture_digest, item.repetition),
                    )
                    for call in benchmark_calls:
                        fixture = fixtures_by_digest[call.fixture_digest]
                        artifact = load_remote_fixture(fixture, allow_root=fixture_root)
                        tested_claim_id = gateway.claim_tested(
                            plan=benchmark_plan,
                            fixture=fixture,
                            repetition=call.repetition,
                        )
                        started = time.perf_counter()
                        execution = runner.invoke(
                            call.prepared,
                            secret_ref=secret_ref,
                            authorization=exact_provider_authorizations[call.call_id],
                            consumed_by=f"cli:model-campaign:benchmark:{call.call_id}",
                        )
                        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
                        actual_provider_calls += 1
                        actual_tested_calls += 1
                        executed_call_ids.add(call.call_id)
                        receipt_evidence = execution.receipt.adapter_evidence_digest
                        if receipt_evidence is None:
                            raise PolicyViolation("Campaign benchmark receipt evidence ister")
                        call_evidence[call.call_id] = receipt_evidence
                        remote_response = _remote_response(
                            call,
                            raw_response=execution.provider_result.response,
                            artifact=artifact,
                            latency_ms=latency_ms,
                        )
                        adapter = OpenCodeRemoteBenchmarkAdapter(
                            routed_model_id=model_id,
                            fixture_root=fixture_root,
                            invoker=_OneShotResponseInvoker(
                                response=remote_response,
                                expected_model_id=model_id,
                                expected_fixture_digest=fixture.fixture_digest,
                                expected_repetition=call.repetition,
                            ),
                            response_store=store,
                        )
                        result = adapter.invoke(
                            plan=benchmark_plan,
                            fixture=fixture,
                            repetition=call.repetition,
                        )
                        gateway.complete_tested(claim_id=tested_claim_id, result=result)
                        verifier_claim_id = gateway.claim_verifier(
                            plan=benchmark_plan,
                            fixture=fixture,
                            result=result,
                            verifier=verifier.verifier,
                        )
                        verdict = verifier.verify(
                            plan=benchmark_plan,
                            fixture=fixture,
                            result=result,
                        )
                        gateway.complete_verifier(
                            claim_id=verifier_claim_id,
                            verdict=verdict,
                        )
                        result = replace(
                            result,
                            verifier_approved=verdict.approved,
                            evidence_digest=digest(
                                {
                                    "tested": result.evidence_digest,
                                    "verifier": verdict.evidence_digest,
                                    "provider_receipt": execution.receipt.id,
                                }
                            ),
                        )
                        benchmark_service.record_trial(
                            benchmark_plan_id,
                            tested_claim_id=tested_claim_id,
                            verifier_claim_id=verifier_claim_id,
                            verdict=verdict,
                            result=result,
                        )
                        trials.append(result)

                    repetition_stable = not (
                        member_record.member.modality == "embedding"
                        and not embedding_repetitions_are_deterministic(tuple(trials))
                    )
                    if all(item.valid for item in trials) and repetition_stable:
                        aggregate = benchmark_service.aggregate(
                            benchmark_plan_id,
                            plan=benchmark_plan,
                            suite=suite,
                            tested_model_id=model_id,
                            verifier=verifier.verifier,
                        )
                        aggregate_id = _aggregate_id(
                            context.connection,
                            plan_id=benchmark_plan_id,
                        )
                        aggregate_ids[model_id] = aggregate_id
                        result_status = CampaignMemberResultStatus.PASSED
                        failure_category = None
                        result_evidence = aggregate.evidence_digest
                    else:
                        result_status = CampaignMemberResultStatus.FAILED
                        failure_category = (
                            "embedding-repetition-drift"
                            if not repetition_stable
                            else "benchmark-contract-failed"
                        )
                        result_evidence = digest(
                            {
                                "model_id": model_id,
                                "trial_evidence": [item.evidence_digest for item in trials],
                                "status": "failed",
                            }
                        )
                        failed_models[model_id] = failure_category
                    benchmark_result = CampaignMemberResult(
                        stage=CampaignMemberResultStage.BENCHMARK,
                        status=result_status,
                        evidence_digest=result_evidence,
                        actual_tested_call_count=len(trials),
                        actual_provider_call_count=len(trials),
                        aggregate_id=aggregate_ids.get(model_id),
                        failure_category=failure_category,
                    )
                    campaign_repository.record_member_result(
                        campaign_id=campaign_id,
                        member_id=member_record.id,
                        member_plan_id=member_plan_id,
                        result=benchmark_result,
                    )
                    member_result_digests[model_id] = benchmark_result.result_digest
                    host.record_success(
                        member_claim,
                        result_digest=benchmark_result.result_digest,
                        adapter_evidence_digest=benchmark_result.evidence_digest,
                    )
                    terminal_models.add(model_id)

                attempted_call_ids = _attempted_provider_call_ids(host, job.id)
                call_kind_by_id = {item.call_id: item.kind for item in manifest.calls}
                actual_provider_calls = len(attempted_call_ids)
                actual_tested_calls = sum(
                    call_kind_by_id[item] is CampaignCallKind.BENCHMARK
                    for item in attempted_call_ids
                )
                executed_call_ids.update(attempted_call_ids)
                passed_count = len(aggregate_ids)
                failed_count = len(failed_models)
                outcome_status = (
                    CampaignOutcomeStatus.PASSED
                    if failed_count == 0
                    else CampaignOutcomeStatus.FAILED
                )
                outcome_evidence = digest(
                    {
                        "campaign_digest": campaign.campaign_digest,
                        "member_results": member_result_digests,
                        "aggregate_ids": aggregate_ids,
                        "failed_models": failed_models,
                        "provider_receipts": call_evidence,
                    }
                )
                outcome = CampaignOutcome(
                    status=outcome_status,
                    passed_count=passed_count,
                    failed_count=failed_count,
                    recovery_required_count=0,
                    audio_excluded_count=campaign.audio_excluded_count,
                    actual_tested_call_count=actual_tested_calls,
                    actual_provider_call_count=actual_provider_calls,
                    evidence_digest=outcome_evidence,
                )
                # Outcome, qualification projection, checkpoint, terminal receipt and
                # job completion are one publish boundary. A late exception rolls every
                # terminal marker back before the recovery path runs.
                with context.connection.transaction():
                    outcome_id, _ = campaign_repository.record_outcome(
                        campaign_id=campaign_id,
                        outcome=outcome,
                    )
                    for model_id, member_record in member_records.items():
                        qualified_aggregate_id: UUID | None = aggregate_ids.get(model_id)
                        event = QualificationEvent(
                            action=(
                                QualificationAction.QUALIFIED
                                if qualified_aggregate_id is not None
                                else QualificationAction.DISQUALIFIED
                            ),
                            model_id=model_id,
                            outcome_id=outcome_id,
                            evidence_digest=digest(
                                {
                                    "campaign_outcome": outcome.outcome_digest,
                                    "model_id": model_id,
                                    "member_result": member_result_digests[model_id],
                                }
                            ),
                            aggregate_id=qualified_aggregate_id,
                            reason_code=failed_models.get(model_id),
                        )
                        campaign_repository.record_qualification(
                            campaign_id=campaign_id,
                            member_id=member_record.id,
                            event=event,
                        )
                    plan_step_ids = tuple(item.step_id for item in task_plan.steps)
                    step_results = []
                    for step_id in plan_step_ids:
                        if step_id in {"opencode-aihub-campaign", "campaign-finalize"}:
                            step_results.append((step_id, outcome.outcome_digest))
                        elif step_id.startswith("member-finalize-"):
                            model_id = step_id.removeprefix("member-finalize-")
                            step_results.append((step_id, member_result_digests[model_id]))
                        else:
                            step_results.append((step_id, call_evidence[step_id]))
                    checkpoint = Checkpoint(
                        checkpoint_id=f"opencode-aihub-campaign-{job.id}",
                        project_id=str(project_id),
                        work_item_id=str(work_id),
                        plan_revision_id=str(plan_id),
                        source_revision=source_revision,
                        plan_steps=plan_step_ids,
                        completed_steps=plan_step_ids,
                        pending_steps=(),
                        step_results=tuple(step_results),
                        context_manifest_digest=manifest.manifest_digest,
                        journal_head_digest=outcome.outcome_digest,
                        next_safe_action="model-campaign-independent-verification",
                        created_at=dt.datetime.now(dt.UTC),
                    )
                    ContextContinuityRepository(
                        context.connection,
                        context.realm_id,
                        project_id,
                        work_id,
                    ).store_checkpoint(checkpoint, task_plan_id=plan_id, job_id=job.id)
                    host.record_success(
                        campaign_claim,
                        result_digest=outcome.outcome_digest,
                        adapter_evidence_digest=outcome.evidence_digest,
                    )
                    if not host.finish(
                        claimed,
                        outcome=AttemptOutcome.SUCCEEDED,
                        result_digest=outcome.outcome_digest,
                    ):
                        raise PolicyViolation("Campaign terminal job completion reddedildi")
                document = {
                    "schema": "zekam-opencode-benchmark-campaign-run/v1",
                    "status": outcome.status.value,
                    "campaign_id": str(campaign_id),
                    "campaign_digest": campaign.campaign_digest,
                    "outcome_id": str(outcome_id),
                    "outcome_digest": outcome.outcome_digest,
                    "qualified_model_count": passed_count,
                    "disqualified_model_count": failed_count,
                    "audio_excluded_count": campaign.audio_excluded_count,
                    "provider_calls_made": actual_provider_calls,
                    "network_calls_made": actual_provider_calls,
                    "tested_call_count": actual_tested_calls,
                    "authorization_count_consumed": actual_provider_calls + len(member_records) + 1,
                    "claim_count": len(host.ledger.claims_for_job(job.id)),
                    "receipt_count": sum(
                        host.ledger.receipt_for_claim(item.id) is not None
                        for item in host.ledger.claims_for_job(job.id)
                    ),
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "raw_prompt_values_reported": 0,
                    "raw_response_values_reported": 0,
                    "endpoint_values_reported": 0,
                    "secret_values_reported": 0,
                    "grants_authority": False,
                }
            except Exception as exc:
                attempted_call_ids = _attempted_provider_call_ids(host, job.id)
                call_kind_by_id = {item.call_id: item.kind for item in manifest.calls}
                actual_provider_calls = len(attempted_call_ids)
                actual_tested_calls = sum(
                    call_kind_by_id[item] is CampaignCallKind.BENCHMARK
                    for item in attempted_call_ids
                )
                executed_call_ids.update(attempted_call_ids)
                recovery_evidence = digest(
                    {
                        "campaign_digest": campaign.campaign_digest,
                        "job_id": job.id,
                        "error_type": type(exc).__name__,
                        "executed_call_ids": sorted(executed_call_ids),
                    }
                )
                # Seal runtime state before best-effort recovery projection. This
                # ordering guarantees no receiptless claim/open lease survives even
                # if an immutable member/outcome replay detects drift.
                with context.connection.transaction():
                    for claim in host.ledger.claims_for_job(job.id):
                        if host.ledger.receipt_for_claim(claim.id) is None:
                            host.record_failure(
                                claim,
                                category=FailureCategory.ADAPTER,
                                failure_digest=recovery_evidence,
                            )
                    if not host.finish(
                        claimed,
                        outcome=AttemptOutcome.RECOVERY_REQUIRED,
                        result_digest=recovery_evidence,
                        failure_category=FailureCategory.ADAPTER,
                    ):
                        raise PolicyViolation(
                            "Campaign recovery job finalization reddedildi"
                        ) from exc
                _revoke_issued(
                    governance,
                    all_authorizations,
                    reason="campaign-aborted-no-silent-retry",
                )
                with context.connection.transaction():
                    for model_id, pending_member in member_records.items():
                        with context.connection.cursor() as cursor:
                            cursor.execute(
                                "select stage, status"
                                " from models.opencode_benchmark_campaign_member_result"
                                " where realm_id = %s and campaign_id = %s and member_id = %s",
                                (context.realm_id, campaign_id, pending_member.id),
                            )
                            existing_results = {
                                str(stage): str(status) for stage, status in cursor.fetchall()
                            }
                        if "benchmark" in existing_results or existing_results.get("health") in {
                            "failed",
                            "recovery-required",
                        }:
                            continue
                        pending_evidence = digest(
                            {
                                "campaign_recovery": recovery_evidence,
                                "model_id": model_id,
                                "status": "campaign-recovery",
                            }
                        )
                        if (
                            model_id == current_model_id
                            and current_member_plan_id is not None
                            and existing_results.get("health") == "passed"
                        ):
                            stage = CampaignMemberResultStage.BENCHMARK
                            member_plan_id = current_member_plan_id
                        elif "health" not in existing_results:
                            stage = CampaignMemberResultStage.HEALTH
                            member_plan_id = None
                        else:
                            # Health passed but benchmark plan/result was not published.
                            # The recovery outcome accounts for this incomplete member.
                            continue
                        campaign_repository.record_member_result(
                            campaign_id=campaign_id,
                            member_id=pending_member.id,
                            member_plan_id=member_plan_id,
                            result=CampaignMemberResult(
                                stage=stage,
                                status=CampaignMemberResultStatus.RECOVERY_REQUIRED,
                                evidence_digest=pending_evidence,
                                actual_tested_call_count=0,
                                actual_provider_call_count=0,
                                failure_category="campaign-recovery-not-run",
                            ),
                        )
                    campaign_repository.record_outcome(
                        campaign_id=campaign_id,
                        outcome=CampaignOutcome(
                            status=CampaignOutcomeStatus.RECOVERY_REQUIRED,
                            passed_count=len(aggregate_ids),
                            failed_count=len(failed_models),
                            recovery_required_count=max(
                                1,
                                len(member_records) - len(aggregate_ids) - len(failed_models),
                            ),
                            audio_excluded_count=campaign.audio_excluded_count,
                            actual_tested_call_count=actual_tested_calls,
                            actual_provider_call_count=actual_provider_calls,
                            evidence_digest=recovery_evidence,
                        ),
                    )
                raise
    except ZekamError as exc:
        raise fail_from(exc) from exc
    console.print_json(json.dumps(document, ensure_ascii=False, default=str))
