"""`zekam model capability`: uzun gorev benchmark plan ve skor karti yuzeyi."""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer
from psycopg import Error as PsycopgError

from zekam.application.config import core_root
from zekam.application.execution import ExecutionHost
from zekam.application.governance import DEFAULT_POLICY_NAME, GovernanceService
from zekam.application.model_capability_benchmark import (
    CapabilityVerifier,
    load_capability_registry,
)
from zekam.application.model_capability_live import (
    CapabilityEpisodeClassification,
    CapabilityLiveTurnResult,
    PreparedCapabilityLiveManifest,
    capability_derivation_attestation_digest,
    classify_capability_episode,
    execute_capability_episode,
    prepare_capability_live_manifest,
)
from zekam.application.model_gateway import ModelGateway
from zekam.application.opencode_benchmark_campaign import BENCHMARK_SECRET_REF_NAME
from zekam.application.opencode_embedding import OpenCodeCredentialStore
from zekam.application.provider_adapter import AuthorizedProviderClient
from zekam.application.provider_contract_execution import (
    ProviderExecutionManifest,
    build_provider_policy_candidate,
)
from zekam.application.provider_contract_runner import RuntimeProviderContractRunner
from zekam.application.secret_broker import SecretBroker
from zekam.application.work_graph import WorkGraphService
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import Checkpoint
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.domain.model_capability_benchmark import (
    CapabilityCohortPlan,
    CapabilityEpisodeResult,
    aggregate_capability_episodes,
)
from zekam.domain.model_capability_runtime import (
    CapabilityRuntimeApprovalManifest,
    CapabilityRuntimeCallOutcome,
    CapabilityRuntimeCallStatus,
    CapabilityRuntimeContinuityState,
    CapabilityRuntimeDerivedAuthorization,
    CapabilityRuntimeEpisodeOutcome,
    CapabilityRuntimeEpisodeStatus,
    CapabilityRuntimeOutcome,
    CapabilityRuntimeSkippedSlot,
    CapabilityRuntimeSlot,
    CapabilityRuntimeStatus,
    CapabilityRuntimeTurnCheckpoint,
)
from zekam.domain.model_invocation import GatewaySourceLabel
from zekam.domain.realm import DEFAULT_REALM_SLUG
from zekam.domain.resources import parse_requests
from zekam.domain.runtime import AttemptOutcome, FailureCategory, Job, JobKind
from zekam.domain.security import (
    Authorization,
    AuthorizationScope,
    AuthorizationState,
    DataClassification,
    SecretBackend,
)
from zekam.domain.work import EffectKind, PlanStep
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.model_capability_repository import ModelCapabilityRepository
from zekam.infrastructure.postgres.model_capability_runtime_repository import (
    ModelCapabilityRuntimeRepository,
)
from zekam.infrastructure.postgres.model_invocation_repository import ModelInvocationRepository
from zekam.infrastructure.postgres.runtime_repository import JobRepository
from zekam.infrastructure.postgres.security_repository import (
    AuthorizationRepository,
    SecretRefRepository,
)
from zekam.infrastructure.process.capability_worker import (
    ProcessIsolatedJsonProviderTransport,
)
from zekam.interfaces.cli.model_campaign import (
    _authorizations_for_plan,
    _CampaignEndpointResolver,
    _source_revision,
)
from zekam.interfaces.cli.model_campaign import (
    _load_manifest as _load_campaign_manifest,
)
from zekam.interfaces.cli.session import HOME_HELP, REALM_HELP, RealmSession, fail, fail_from

app = typer.Typer(
    name="capability",
    help="Süreli, paralel ve rol-bazli model yetenek benchmark'i",
    no_args_is_help=True,
)

# E2E testleri gercek aga cikmadan ayni authority/claim/receipt zincirini kullanabilir.
CAPABILITY_TRANSPORT_FACTORY: Any = ProcessIsolatedJsonProviderTransport


def _plan(home: str | None, realm: str) -> CapabilityCohortPlan:
    root = core_root()
    registry, profile, _ = load_capability_registry(
        root / "config" / "model_capability_benchmark.yaml",
        repository_root=root,
    )
    with RealmSession(home, realm) as context:
        source = ModelCapabilityRepository(context.connection, context.realm_id).latest_source()
    return CapabilityCohortPlan(
        source_campaign_id=source.campaign_id,
        source_revision=source.source_revision,
        inventory_digest=source.inventory_digest,
        policy_digest=source.policy_digest,
        verifier_provenance_digest=source.verifier_provenance_digest,
        model_ids=source.model_ids,
        registry=registry,
        execution_profile=profile,
        max_parallelism=len(source.model_ids),
    )


def _episode_key(model_id: str, task_digest: str) -> str:
    return digest({"model_id": model_id, "task_digest": task_digest}).removeprefix("sha256:")[:24]


def _episode_resource(model_id: str, task_digest: str) -> str:
    return f"model-benchmark:{model_id}:capability-{task_digest.removeprefix('sha256:')}"


def _cohort_resource(cohort_id: UUID) -> str:
    return f"model-benchmark:capability-cohort:{cohort_id}"


def _capability_chain_seed_digest(*, plan_digest: str, model_id: str, task_digest: str) -> str:
    return digest(
        {
            "schema": "zekam-capability-chain-seed/v1",
            "plan_digest": plan_digest,
            "model_id": model_id,
            "task_digest": task_digest,
        }
    )


def _runtime_steps(
    plan: CapabilityCohortPlan,
    live_manifest: PreparedCapabilityLiveManifest | None = None,
    cohort_id: UUID | None = None,
) -> tuple[PlanStep, ...]:
    provider_steps = (
        ()
        if live_manifest is None
        else tuple(
            PlanStep(
                step_id=slot.prepared.plan.call_id,
                title=f"Capability call {slot.model_id} / {slot.phase}",
                effect=EffectKind.PROVIDER_CALL,
                logical_resources=(slot.prepared.plan.call_resource,),
                depends_on=(
                    ()
                    if slot.turn_index == 1
                    else (
                        next(
                            previous.prepared.plan.call_id
                            for previous in live_manifest.slots
                            if previous.model_id == slot.model_id
                            and previous.task_digest == slot.task_digest
                            and previous.turn_index == slot.turn_index - 1
                        ),
                    )
                ),
                risk="critical",
            )
            for slot in live_manifest.slots
        )
    )
    episode_steps = tuple(
        PlanStep(
            step_id=f"capability-episode-{_episode_key(model_id, task.task_digest)}",
            title=f"Capability episode {model_id} / {task.task_id}",
            effect=(
                EffectKind.PROVIDER_CALL if live_manifest is None else EffectKind.DATABASE_WRITE
            ),
            logical_resources=(
                (_episode_resource(model_id, task.task_digest),)
                if live_manifest is None
                else tuple(
                    sorted(
                        (
                            *(
                                slot.prepared.plan.call_resource
                                for slot in live_manifest.slots
                                if slot.model_id == model_id
                                and slot.task_digest == task.task_digest
                            ),
                            _episode_resource(model_id, task.task_digest),
                        )
                    )
                )
            ),
            depends_on=(
                ()
                if live_manifest is None
                else tuple(
                    slot.prepared.plan.call_id
                    for slot in live_manifest.slots
                    if slot.model_id == model_id and slot.task_digest == task.task_digest
                )
            ),
            risk="critical" if live_manifest is None else "high",
        )
        for task in plan.registry.tasks
        for model_id in plan.model_ids
    )
    return (
        *provider_steps,
        *episode_steps,
        PlanStep(
            step_id="capability-finalize",
            title="Capability cohort scorecard finalization",
            effect=EffectKind.DATABASE_WRITE,
            logical_resources=(
                (
                    _cohort_resource(cohort_id)
                    if cohort_id is not None
                    else (
                        "model-benchmark:capability-plan:"
                        f"{plan.plan_digest.removeprefix('sha256:')}"
                    )
                ),
            ),
            depends_on=tuple(row.step_id for row in episode_steps),
            risk="high",
        ),
    )


def _issue_authorization(
    *,
    realm_id: UUID,
    actor_id: UUID,
    work_id: UUID,
    task_plan_id: UUID,
    plan_digest: str,
    effect_digest: str,
    scope: AuthorizationScope,
    risk: str,
) -> Authorization:
    return Authorization.issue(
        realm_id=realm_id,
        actor_id=actor_id,
        work_item_id=work_id,
        plan_id=task_plan_id,
        plan_digest=plan_digest,
        effect_digest=effect_digest,
        scope=scope,
        risk=risk,
        lifetime=dt.timedelta(hours=4),
    )


def _token_usage(response: Any) -> tuple[int, int]:
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        raise PolicyViolation("Capability provider token usage kaniti eksik")
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if (
        not isinstance(prompt, int)
        or isinstance(prompt, bool)
        or prompt < 0
        or not isinstance(completion, int)
        or isinstance(completion, bool)
        or completion < 0
    ):
        raise PolicyViolation("Capability provider token usage kaniti gecersiz")
    return prompt, completion


def _episode_authorization_digest(
    plan: CapabilityCohortPlan, result: CapabilityEpisodeResult
) -> str:
    return digest(
        {
            "plan_digest": plan.plan_digest,
            "model_id": result.model_id,
            "task_digest": result.task_digest,
            "effect": "episode-ledger",
        }
    )


def _execute_live_episode(
    *,
    home: str | None,
    realm: str,
    project_id: UUID,
    manifest_id: UUID,
    approval_manifest: CapabilityRuntimeApprovalManifest,
    plan: CapabilityCohortPlan,
    live_manifest: PreparedCapabilityLiveManifest,
    fixtures: dict[str, Any],
    model_id: str,
    task_digest: str,
    wave_barrier: threading.Barrier,
    wave_start_ns: list[int],
    wave_start_lock: threading.Lock,
) -> CapabilityEpisodeResult:
    task = next(row for row in plan.registry.tasks if row.task_digest == task_digest)
    prepared_slots = tuple(
        row
        for row in live_manifest.slots
        if row.model_id == model_id and row.task_digest == task_digest
    )
    with RealmSession(home, realm) as context:
        runtime_repository = ModelCapabilityRuntimeRepository(context.connection, context.realm_id)
        stored = {
            row.slot.turn_number: row
            for row in runtime_repository.slots(manifest_id)
            if row.slot.model_id == model_id and row.slot.task_digest == task_digest
        }
        if len(stored) != 8 or any(
            row.terminal_status is not None or row.derived_authorization is not None
            for row in stored.values()
        ):
            raise PolicyViolation("Capability episode replay veya eksik slot reddedildi")
        authorizations = AuthorizationRepository(context.connection, context.realm_id)
        episode_plan_digest = digest(
            {
                "plan_digest": plan.plan_digest,
                "model_id": model_id,
                "task_digest": task_digest,
                "effect": "episode-ledger",
            }
        )
        ledger_authorization = next(
            row
            for row in _authorizations_for_plan(
                context.connection, authorizations, plan_id=approval_manifest.task_plan_id
            )
            if row.plan_digest == episode_plan_digest
        )
        actor_id = ledger_authorization.actor_id
        governance = GovernanceService(context.connection, context.realm, actor_id=actor_id)
        secret_ref = SecretRefRepository(context.connection, context.realm_id).current_by_name(
            BENCHMARK_SECRET_REF_NAME
        )
        if secret_ref is None:
            raise PolicyViolation("Capability runtime SecretRef bulunamadi")
        endpoint_resolver = _CampaignEndpointResolver(live_manifest.endpoint_mapping)
        secret_broker = SecretBroker(
            {
                SecretBackend.ENVIRONMENT: OpenCodeCredentialStore(
                    provider_id=secret_ref.provider,
                    credential_locator=live_manifest.credential_locator,
                )
            }
        )
        episode_key = _episode_key(model_id, task_digest)
        host = ExecutionHost(
            context.connection,
            context.realm_id,
            worker_label=f"capability-{episode_key}",
        )
        claimed = host.acquire_work(
            capabilities=(f"provider.capability.{episode_key}",),
            lease_seconds=max(300, task.max_duration_seconds + 15),
        )
        expected_job_id = stored[1].slot.job_id
        if claimed is None or claimed.job.id != expected_job_id:
            raise PolicyViolation("Capability episode exact job claim edilemedi")
        episode_checkpoint = Checkpoint(
            checkpoint_id=f"capability-{manifest_id}-{episode_key}",
            project_id=str(project_id),
            work_item_id=str(approval_manifest.work_item_id),
            plan_revision_id=str(approval_manifest.task_plan_id),
            source_revision=approval_manifest.source_revision,
            plan_steps=tuple(row.step_id for row in _runtime_steps(plan, live_manifest)),
            completed_steps=(),
            pending_steps=tuple(row.step_id for row in _runtime_steps(plan, live_manifest)),
            step_results=(),
            context_manifest_digest=live_manifest.manifest_digest,
            journal_head_digest=digest(
                {"manifest_id": manifest_id, "job_id": claimed.job.id, "state": "started"}
            ),
            next_safe_action="capability-next-jit-turn",
            created_at=dt.datetime.now(dt.UTC),
        )
        ContextContinuityRepository(
            context.connection,
            context.realm_id,
            project_id,
            approval_manifest.work_item_id,
        ).store_checkpoint(
            episode_checkpoint,
            task_plan_id=approval_manifest.task_plan_id,
            job_id=claimed.job.id,
        )
        executions: dict[int, Any] = {}
        provider_authorizations: dict[int, Authorization] = {}
        persisted_outcome_turns: set[int] = set()
        continuity_state_ids: dict[int, UUID] = {}
        turn_checkpoint_ids: dict[int, UUID] = {}
        prior_response_chain_digest = stored[1].slot.chain_seed_digest
        episode_deadline_ns = time.monotonic_ns() + task.max_duration_seconds * 1_000_000_000

        def invoke(slot: Any, concrete: Any, prior_state: Any) -> CapabilityLiveTurnResult:
            nonlocal prior_response_chain_digest
            persisted = stored[slot.turn_index].slot
            if (
                persisted.provider_ref != slot.prepared.plan.provider_ref
                or persisted.endpoint_resource != slot.prepared.plan.target
                or persisted.endpoint_identity_digest != slot.prepared.plan.endpoint_binding_digest
                or persisted.operation != slot.prepared.plan.operation
                or persisted.request_template_digest != slot.template_digest
                or persisted.derivation_rule_digest != slot.derivation_digest
                or persisted.chain_seed_digest
                != _capability_chain_seed_digest(
                    plan_digest=plan.plan_digest,
                    model_id=model_id,
                    task_digest=task_digest,
                )
            ):
                raise PolicyViolation("Capability runtime prepared/persisted slot drift")
            expected_prior = (
                persisted.chain_seed_digest if slot.turn_index == 1 else prior_response_chain_digest
            )
            derived_plan = concrete.plan
            continuity_state = dict(prior_state)
            continuity_digest = digest(continuity_state)
            continuity_event_digest = digest(
                {
                    "schema": "zekam-capability-turn-checkpoint/v1",
                    "slot_digest": persisted.slot_digest,
                    "continuity_state_digest": continuity_digest,
                    "prior_result_digest": expected_prior,
                    "completed_turns": list(range(1, slot.turn_index)),
                    "pending_turns": list(range(slot.turn_index, 9)),
                }
            )
            with context.connection.transaction():
                continuity_state_ids[slot.turn_index] = runtime_repository.persist_continuity_state(
                    manifest_id,
                    stored[slot.turn_index].slot_id,
                    CapabilityRuntimeContinuityState(
                        continuity_state=continuity_state,
                        continuity_state_digest=continuity_digest,
                        prior_result_digest=expected_prior,
                        derivation_attestation_digest=(
                            capability_derivation_attestation_digest(slot, continuity_state)
                        ),
                        checkpoint_id=turn_checkpoint_ids.get(slot.turn_index - 1),
                        event_digest=continuity_event_digest,
                    ),
                )
            db_derivation = runtime_repository.derive_slot_authorization(
                stored[slot.turn_index].slot_id
            )
            derivation_drift = tuple(
                label
                for label, matches in (
                    (
                        "request-body",
                        db_derivation.request_body == dict(concrete.call.payload),
                    ),
                    (
                        "request-digest",
                        db_derivation.request_body_digest == derived_plan.payload_digest,
                    ),
                    (
                        "plan-digest",
                        db_derivation.authorization_plan_digest
                        == derived_plan.authorization_plan_digest,
                    ),
                    (
                        "effect-digest",
                        db_derivation.effect_digest == derived_plan.effect_request.effect_digest,
                    ),
                    (
                        "effect-action",
                        db_derivation.effect_action == derived_plan.effect_action,
                    ),
                    (
                        "claim-operation",
                        db_derivation.claim_operation
                        == f"provider-contract:{derived_plan.call_id}",
                    ),
                )
                if not matches
            )
            if derivation_drift:
                raise PolicyViolation(
                    "Capability DB/Python derived authority drift: " + ",".join(derivation_drift)
                )
            provider_authorization = _issue_authorization(
                realm_id=context.realm_id,
                actor_id=actor_id,
                work_id=approval_manifest.work_item_id,
                task_plan_id=approval_manifest.task_plan_id,
                plan_digest=db_derivation.authorization_plan_digest,
                effect_digest=db_derivation.effect_digest,
                scope=AuthorizationScope(
                    allowed_resources=(derived_plan.target, derived_plan.call_resource),
                    allowed_effects=(EffectKind.PROVIDER_CALL.value,),
                    provider_refs=(derived_plan.provider_ref,),
                    secret_ref_ids=(secret_ref.id,),
                    data_classifications=(DataClassification.PUBLIC,),
                ),
                risk="critical",
            )
            with context.connection.transaction():
                authorizations.issue(provider_authorization)
                runtime_repository.bind_slot_authorization(
                    manifest_id,
                    stored[slot.turn_index].slot_id,
                    CapabilityRuntimeDerivedAuthorization(
                        authorization_id=provider_authorization.id,
                        authorization_plan_digest=db_derivation.authorization_plan_digest,
                        authorization_digest=provider_authorization.authorization_digest,
                        request_body_digest=db_derivation.request_body_digest,
                        effect_digest=db_derivation.effect_digest,
                        prior_response_chain_digest=expected_prior,
                    ),
                )
            provider_authorizations[slot.turn_index] = provider_authorization
            remaining_seconds = (episode_deadline_ns - time.monotonic_ns()) / 1_000_000_000
            if remaining_seconds <= 0.2:
                raise PolicyViolation("Capability episode hard deadline asildi")
            grace_seconds = max(0.1, min(10.0, remaining_seconds / 3))
            timeout_seconds = max(0.1, min(30.0, remaining_seconds - grace_seconds))
            client = AuthorizedProviderClient(
                governance,
                endpoint_resolver,
                secret_broker,
                CAPABILITY_TRANSPORT_FACTORY(
                    timeout_seconds=timeout_seconds,
                    cancellation_grace_seconds=grace_seconds,
                ),
            )
            execution = RuntimeProviderContractRunner(
                host=host,
                work=claimed,
                client=client,
                gateway=ModelGateway(
                    ModelInvocationRepository(context.connection, context.realm_id),
                    GatewaySourceLabel.MODEL_CAPABILITY,
                ),
                defer_job_recovery=True,
            ).invoke(
                concrete,
                secret_ref=secret_ref,
                authorization=provider_authorization,
                consumed_by=f"cli:model-capability:{slot.slot_key}",
            )
            executions[slot.turn_index] = execution
            input_tokens, output_tokens = _token_usage(execution.provider_result.response)
            call_evidence = digest(
                {
                    "slot_digest": stored[slot.turn_index].slot.slot_digest,
                    "claim_id": execution.claim.id,
                    "receipt_id": execution.receipt.id,
                    "result_digest": execution.provider_result.response_digest,
                }
            )
            completed_turns = tuple(range(1, slot.turn_index + 1))
            pending_turns = tuple(range(slot.turn_index + 1, 9))
            turn_checkpoint = CapabilityRuntimeTurnCheckpoint(
                continuity_state_id=continuity_state_ids[slot.turn_index],
                completed_turns=completed_turns,
                pending_turns=pending_turns,
                result_digest=execution.provider_result.response_digest,
                checkpoint_digest=digest(
                    {
                        "schema": "zekam-capability-turn-checkpoint/v1",
                        "slot_digest": stored[slot.turn_index].slot.slot_digest,
                        "continuity_state_id": str(continuity_state_ids[slot.turn_index]),
                        "completed_turns": list(completed_turns),
                        "pending_turns": list(pending_turns),
                        "result_digest": execution.provider_result.response_digest,
                    }
                ),
            )
            with context.connection.transaction():
                turn_checkpoint_ids[slot.turn_index] = runtime_repository.persist_turn_checkpoint(
                    manifest_id,
                    stored[slot.turn_index].slot_id,
                    claimed.job.id,
                    turn_checkpoint,
                )
                runtime_repository.record_call_outcome(
                    stored[slot.turn_index].slot_id,
                    CapabilityRuntimeCallOutcome(
                        status=CapabilityRuntimeCallStatus.COMPLETED,
                        claim_id=execution.claim.id,
                        checkpoint_id=turn_checkpoint_ids[slot.turn_index],
                        receipt_id=execution.receipt.id,
                        result_digest=execution.provider_result.response_digest,
                        failure_category=None,
                        evidence_digest=call_evidence,
                        completed_at=dt.datetime.now(dt.UTC),
                    ),
                )
            persisted_outcome_turns.add(slot.turn_index)
            prior_response_chain_digest = execution.provider_result.response_digest
            return CapabilityLiveTurnResult(
                response=execution.provider_result.response,
                response_digest=execution.provider_result.response_digest,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )

        verifier = CapabilityVerifier(
            model_id="independent-capability-verifier",
            execution_identity=f"capability-verifier:{episode_key}",
            provenance_digest=plan.execution_profile.evaluator_provenance_digest,
        )
        try:
            # No provider effect begins until all seven lanes own their exact
            # jobs.  Every lane observes the same complete timestamp set.
            wave_barrier.wait(timeout=30)
            with wave_start_lock:
                wave_start_ns.append(time.monotonic_ns())
            wave_barrier.wait(timeout=30)
            skew_ms = (max(wave_start_ns) - min(wave_start_ns)) // 1_000_000
            if skew_ms > plan.start_skew_budget_ms:
                raise PolicyViolation("Capability wave start skew budget asildi")
            result = execute_capability_episode(
                plan=plan,
                task=task,
                fixture=fixtures[task_digest],
                model_id=model_id,
                slots=prepared_slots,
                invoke=invoke,
                verifier=verifier,
            )
            if _episode_authorization_digest(plan, result) != episode_plan_digest:
                raise PolicyViolation("Capability episode ledger plan binding drift")
            resource = _episode_resource(model_id, task_digest)
            ledger_claim = host.claim_effect(
                claimed,
                operation="capability-episode-ledger",
                effect_digest=ledger_authorization.effect_digest,
                authorization_digest=ledger_authorization.authorization_digest,
                authorization_id=ledger_authorization.id,
                resources=parse_requests(write=(resource,)),
                adapter_digest=digest("capability-episode-ledger/v1"),
            )
            consumed = authorizations.consume(
                ledger_authorization.id,
                effect_digest=ledger_authorization.effect_digest,
                consumed_by=f"cli:model-capability:episode:{episode_key}",
            )
            if not consumed.consumed:
                raise PolicyViolation("Capability episode ledger authority tuketilemedi")
            with context.connection.transaction():
                ModelCapabilityRepository(context.connection, context.realm_id).record_episode(
                    approval_manifest.cohort_id, result
                )
                host.record_success(
                    ledger_claim,
                    result_digest=result.evidence_digest,
                    adapter_evidence_digest=result.acceptance_evidence_digest,
                )
                if not host.finish(
                    claimed,
                    outcome=AttemptOutcome.SUCCEEDED,
                    result_digest=result.evidence_digest,
                ):
                    raise PolicyViolation("Capability episode job finish reddedildi")
            classification = classify_capability_episode(result)
            if classification is CapabilityEpisodeClassification.MODEL_CONTRACT_FAILED:
                attempted_calls = result.model_turn_count
                reason_code = "model-response-contract"
                terminal_status = CapabilityRuntimeEpisodeStatus.MODEL_CONTRACT_FAILED
                skipped_slots = tuple(
                    CapabilityRuntimeSkippedSlot(
                        slot_id=stored[turn].slot_id,
                        reason_code=reason_code,
                        evidence_digest=digest(
                            {
                                "schema": "zekam-capability-skipped-slot/v1",
                                "episode_evidence_digest": result.evidence_digest,
                                "slot_digest": stored[turn].slot.slot_digest,
                                "reason_code": reason_code,
                            }
                        ),
                    )
                    for turn in range(attempted_calls + 1, 9)
                )
            else:
                attempted_calls = 8
                reason_code = None
                terminal_status = CapabilityRuntimeEpisodeStatus.SUCCESSFUL
                skipped_slots = ()
            runtime_repository.record_episode_outcome(
                manifest_id,
                CapabilityRuntimeEpisodeOutcome(
                    model_id=model_id,
                    task_digest=task_digest,
                    job_id=claimed.job.id,
                    status=terminal_status,
                    attempted_calls=attempted_calls,
                    successful_calls=attempted_calls,
                    failure_turn=(
                        attempted_calls
                        if terminal_status is CapabilityRuntimeEpisodeStatus.MODEL_CONTRACT_FAILED
                        else None
                    ),
                    reason_code=reason_code,
                    evidence_digest=digest(
                        {
                            "schema": "zekam-capability-runtime-episode-outcome/v1",
                            "manifest_id": str(manifest_id),
                            "job_id": str(claimed.job.id),
                            "episode_evidence_digest": result.evidence_digest,
                            "status": terminal_status.value,
                            "attempted_calls": attempted_calls,
                        }
                    ),
                    completed_at=dt.datetime.now(dt.UTC),
                ),
                skipped_slots,
            )
            return result
        except Exception as episode_exc:
            recovery_digest = digest(
                {
                    "manifest_id": manifest_id,
                    "job_id": claimed.job.id,
                    "model_id": model_id,
                    "task_digest": task_digest,
                    "error_type": type(episode_exc).__name__,
                }
            )
            for claim in host.ledger.claims_for_job(claimed.job.id):
                if host.ledger.receipt_for_claim(claim.id) is None:
                    host.record_failure(
                        claim,
                        category=FailureCategory.ADAPTER,
                        failure_digest=recovery_digest,
                    )
            with context.connection.transaction():
                if not host.finish(
                    claimed,
                    outcome=AttemptOutcome.RECOVERY_REQUIRED,
                    result_digest=recovery_digest,
                    failure_category=FailureCategory.ADAPTER,
                ):
                    raise PolicyViolation(
                        "Capability recovery job finish reddedildi"
                    ) from episode_exc
                call_id_to_turn = {
                    row.prepared.plan.call_id: row.turn_index for row in prepared_slots
                }
                for claim in host.ledger.claims_for_job(claimed.job.id):
                    prefix = "provider-contract:"
                    if not claim.operation.startswith(prefix):
                        continue
                    recovery_turn = call_id_to_turn.get(claim.operation.removeprefix(prefix))
                    receipt = host.ledger.receipt_for_claim(claim.id)
                    if recovery_turn is None or receipt is None:
                        raise PolicyViolation(
                            "Capability recovery call/receipt binding eksik"
                        ) from episode_exc
                    if recovery_turn in persisted_outcome_turns:
                        continue
                    completed = receipt.status.value == "completed"
                    checkpoint_result_digest = (
                        receipt.result_digest
                        if completed and receipt.result_digest is not None
                        else recovery_digest
                    )
                    completed_turns = tuple(range(1, recovery_turn + 1))
                    pending_turns = tuple(range(recovery_turn + 1, 9))
                    recovery_turn_checkpoint = CapabilityRuntimeTurnCheckpoint(
                        continuity_state_id=continuity_state_ids[recovery_turn],
                        completed_turns=completed_turns,
                        pending_turns=pending_turns,
                        result_digest=checkpoint_result_digest,
                        checkpoint_digest=digest(
                            {
                                "schema": "zekam-capability-turn-checkpoint/v1",
                                "slot_digest": stored[recovery_turn].slot.slot_digest,
                                "continuity_state_id": str(continuity_state_ids[recovery_turn]),
                                "completed_turns": list(completed_turns),
                                "pending_turns": list(pending_turns),
                                "result_digest": checkpoint_result_digest,
                                "recovery": recovery_digest,
                            }
                        ),
                    )
                    recovery_checkpoint_id = runtime_repository.persist_turn_checkpoint(
                        manifest_id,
                        stored[recovery_turn].slot_id,
                        claimed.job.id,
                        recovery_turn_checkpoint,
                    )
                    call_evidence = digest(
                        {
                            "slot_digest": stored[recovery_turn].slot.slot_digest,
                            "claim_id": claim.id,
                            "receipt_id": receipt.id,
                            "receipt_status": receipt.status.value,
                            "result_digest": receipt.result_digest,
                            "failure_category": (
                                None
                                if receipt.failure_category is None
                                else receipt.failure_category.value
                            ),
                            "recovery": recovery_digest,
                        }
                    )
                    runtime_repository.record_call_outcome(
                        stored[recovery_turn].slot_id,
                        CapabilityRuntimeCallOutcome(
                            status=(
                                CapabilityRuntimeCallStatus.COMPLETED
                                if completed
                                else CapabilityRuntimeCallStatus.FAILED
                            ),
                            claim_id=claim.id,
                            checkpoint_id=recovery_checkpoint_id,
                            receipt_id=receipt.id,
                            result_digest=receipt.result_digest if completed else None,
                            failure_category=(
                                None
                                if completed
                                else (
                                    receipt.failure_category.value
                                    if receipt.failure_category is not None
                                    else FailureCategory.ADAPTER.value
                                )
                            ),
                            evidence_digest=call_evidence,
                            completed_at=receipt.completed_at,
                        ),
                    )
            episode_plan_digest = digest(
                {
                    "plan_digest": plan.plan_digest,
                    "model_id": model_id,
                    "task_digest": task_digest,
                    "effect": "episode-ledger",
                }
            )
            revocation_candidates = [*provider_authorizations.values()]
            revocation_candidates.extend(
                row
                for row in _authorizations_for_plan(
                    context.connection, authorizations, plan_id=approval_manifest.task_plan_id
                )
                if row.plan_digest == episode_plan_digest
            )
            for authorization in revocation_candidates:
                current = authorizations.get(authorization.id)
                if current.state is AuthorizationState.ISSUED:
                    governance.revoke_authorization(current.id, "capability-aborted-no-retry")
            raise


def _seal_runtime_failure(
    *,
    home: str | None,
    realm: str,
    manifest_id: UUID,
    approval_manifest: CapabilityRuntimeApprovalManifest,
    error: Exception,
) -> None:
    """Seal the whole reviewed run without retrying or inventing provider effects."""

    with RealmSession(home, realm) as context:
        runtime_repository = ModelCapabilityRuntimeRepository(context.connection, context.realm_id)
        with context.connection.cursor() as cursor:
            cursor.execute(
                "select 1 from models.capability_runtime_outcome"
                " where realm_id=%s and manifest_id=%s",
                (context.realm_id, manifest_id),
            )
            if cursor.fetchone() is not None:
                return
        authorizations = AuthorizationRepository(context.connection, context.realm_id)
        plan_authorizations = _authorizations_for_plan(
            context.connection,
            authorizations,
            plan_id=approval_manifest.task_plan_id,
        )
        for authorization in plan_authorizations:
            current = authorizations.get(authorization.id)
            if current.state is AuthorizationState.ISSUED:
                GovernanceService(
                    context.connection,
                    context.realm,
                    actor_id=current.actor_id,
                ).revoke_authorization(current.id, "capability-run-aborted-no-retry")

        failure_digest = digest(
            {
                "manifest_digest": approval_manifest.manifest_digest,
                "error_type": type(error).__name__,
                "effect": "capability-runtime-sealed-failure",
            }
        )
        host = ExecutionHost(
            context.connection,
            context.realm_id,
            worker_label="capability-recovery-sealer",
        )
        with context.connection.transaction(), context.connection.cursor() as cursor:
            cursor.execute(
                "select id from runtime.job where realm_id=%s and work_item_id=%s"
                " and plan_id=%s and state not in ('completed','failed','recovery-required')"
                " for update",
                (
                    context.realm_id,
                    approval_manifest.work_item_id,
                    approval_manifest.task_plan_id,
                ),
            )
            nonterminal_jobs = tuple(UUID(str(row[0])) for row in cursor.fetchall())
            for job_id in nonterminal_jobs:
                for claim in host.ledger.claims_for_job(job_id):
                    if host.ledger.receipt_for_claim(claim.id) is None:
                        host.record_failure(
                            claim,
                            category=FailureCategory.ADAPTER,
                            failure_digest=failure_digest,
                        )
                cursor.execute(
                    "update runtime.job_attempt set outcome='recovery-required',"
                    " failure_category='adapter',result_digest=%s,finished_at=now()"
                    " where realm_id=%s and job_id=%s and outcome is null",
                    (failure_digest, context.realm_id, job_id),
                )
                cursor.execute(
                    "update runtime.job set state='recovery-required' where realm_id=%s and id=%s",
                    (context.realm_id, job_id),
                )
                cursor.execute(
                    "delete from runtime.resource_lock where realm_id=%s and job_id=%s",
                    (context.realm_id, job_id),
                )
                cursor.execute(
                    "delete from runtime.lease where realm_id=%s and job_id=%s",
                    (context.realm_id, job_id),
                )
            cursor.execute(
                "select c.evidence_digest"
                " from models.capability_runtime_approval_slot s"
                " join models.capability_runtime_call_outcome c"
                " on c.realm_id=s.realm_id and c.slot_id=s.id"
                " where s.realm_id=%s and s.manifest_id=%s order by c.evidence_digest",
                (context.realm_id, manifest_id),
            )
            call_evidence = tuple(str(row[0]) for row in cursor.fetchall())
            runtime_evidence = digest(
                {
                    "manifest_digest": approval_manifest.manifest_digest,
                    "failure_digest": failure_digest,
                    "call_evidence": call_evidence,
                    "actual_provider_calls": len(call_evidence),
                    "actual_retries": 0,
                    "routing_eligible": False,
                }
            )
            runtime_repository.finalize_outcome(
                manifest_id,
                CapabilityRuntimeOutcome(
                    status=CapabilityRuntimeStatus.RECOVERY_REQUIRED,
                    actual_provider_calls=len(call_evidence),
                    actual_retries=0,
                    call_evidence_digests=call_evidence,
                    evidence_digest=runtime_evidence,
                    completed_at=dt.datetime.now(dt.UTC),
                ),
            )


@app.command("plan")
def plan_command(
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Exact call/wall/concurrency planini gösterir; kayit veya provider cagrisi yapmaz."""
    try:
        plan = _plan(home, realm)
        with RealmSession(home, realm) as context:
            labels = ModelCapabilityRepository(context.connection, context.realm_id).model_labels(
                plan.source_campaign_id, plan.model_ids
            )
        payload = {
            "schema": "zekam-capability-benchmark-plan/v1",
            "status": "calibration-plan-ready",
            "runtime_available": True,
            "routing_qualification_granted": False,
            "source_campaign_id": str(plan.source_campaign_id),
            "source_revision": plan.source_revision,
            "plan_digest": plan.plan_digest,
            "registry_digest": plan.registry.registry_digest,
            "execution_profile_digest": plan.execution_profile.profile_digest,
            "model_count": len(plan.model_ids),
            "model_ids": list(plan.model_ids),
            "models": [
                {"model_id": model_id, "name": labels[model_id]} for model_id in plan.model_ids
            ],
            "task_count": len(plan.registry.tasks),
            "tasks": [
                {
                    "task_id": task.task_id,
                    "role": task.role.value,
                    "workload": task.workload,
                    "max_duration_seconds": task.max_duration_seconds,
                    "max_output_tokens": task.max_output_tokens,
                    "task_digest": task.task_digest,
                }
                for task in plan.registry.tasks
            ],
            "parallelism": plan.max_parallelism,
            "start_skew_budget_ms": plan.start_skew_budget_ms,
            "provider_call_budget": plan.provider_call_budget,
            "maximum_wall_seconds": plan.maximum_wall_seconds,
            "max_retries": plan.execution_profile.max_retries,
            "authority_records_created": 0,
            "provider_calls_made": 0,
            "network_calls_made": 0,
            "grants_authority": False,
            "next_action": "Fresh current-source base campaign sonrasi authorize/run",
        }
        typer.echo(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) if json_output else payload
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    except PsycopgError as exc:
        raise fail(f"Capability database operation failed [{exc.sqlstate or 'unknown'}]") from exc


@app.command("authorize")
def authorize_command(
    project_id: Annotated[UUID, typer.Option("--project-uuid")],
    work_id: Annotated[UUID, typer.Option("--work")],
    actor_id: Annotated[UUID, typer.Option("--actor")],
    config_file: Annotated[Path | None, typer.Option("--config")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Exact 21x8 runtime manifestini ve one-shot authority setini kaydeder."""

    if not apply:
        raise fail("Capability authorize --uygula ister; provider cagrisi yapmaz", 64)
    del json_output
    try:
        root = core_root()
        plan = _plan(home, realm)
        registry, profile, fixtures = load_capability_registry(
            root / "config" / "model_capability_benchmark.yaml",
            repository_root=root,
        )
        if registry.registry_digest != plan.registry.registry_digest or (
            profile.profile_digest != plan.execution_profile.profile_digest
        ):
            raise ZekamError("Capability registry plan hazirlandiktan sonra degisti")
        _, campaign_manifest = _load_campaign_manifest(
            config_file=config_file, scope_file=scope_file
        )
        live_manifest = prepare_capability_live_manifest(plan, fixtures, campaign_manifest)
        source_head, current_source_revision = _source_revision()
        if plan.source_revision != current_source_revision:
            raise ZekamError(
                "Capability kaynak kampanyasi current HEAD'e bagli degil; once fresh base "
                "campaign gerekir"
            )
        provider_execution_manifest = ProviderExecutionManifest(
            binding_set_digest=campaign_manifest.discovery.discovery_digest,
            fixture_digest=registry.registry_digest,
            calls=tuple(row.prepared.plan for row in live_manifest.slots),
        )
        with RealmSession(home, realm) as context:
            graph = WorkGraphService(context.connection, context.realm, actor_id=actor_id)
            work = graph.items.get(work_id)
            if work.project_id != project_id:
                raise ZekamError("Capability Work/project binding mismatch")
            capability_repository = ModelCapabilityRepository(context.connection, context.realm_id)
            capability_repository.require_current_source(plan)
            governance = GovernanceService(context.connection, context.realm, actor_id=actor_id)
            current_policy = governance.policies.current(DEFAULT_POLICY_NAME)
            if current_policy is None:
                raise ZekamError("Kanonik varsayilan policy bulunamadi")
            candidate_policy = build_provider_policy_candidate(
                current_policy, provider_execution_manifest
            )
            secret_ref = SecretRefRepository(context.connection, context.realm_id).current_by_name(
                BENCHMARK_SECRET_REF_NAME
            )
            if secret_ref is None or not secret_ref.is_usable():
                raise ZekamError("Capability runtime SecretRef metadata hazir degil")
            authorizations = AuthorizationRepository(context.connection, context.realm_id)
            jobs = JobRepository(context.connection, context.realm_id)
            runtime_repository = ModelCapabilityRuntimeRepository(
                context.connection, context.realm_id
            )
            episode_jobs: dict[tuple[str, str], Job] = {}
            episode_authorizations: list[Authorization] = []
            with context.connection.transaction():
                governance.policies.append(candidate_policy)
                _, cohort_id, cohort_created = capability_repository.ensure_plan(plan)
                steps = _runtime_steps(plan, live_manifest, cohort_id)
                task_plan = graph.create_plan(
                    work_id,
                    source_revision=current_source_revision,
                    policy_digest=candidate_policy.policy_digest,
                    steps=steps,
                )
                for task in plan.registry.tasks:
                    for model_id in plan.model_ids:
                        episode_key = _episode_key(model_id, task.task_digest)
                        resource = _episode_resource(model_id, task.task_digest)
                        slot_resources = tuple(
                            row.prepared.plan.call_resource
                            for row in live_manifest.slots
                            if row.model_id == model_id and row.task_digest == task.task_digest
                        )
                        job, created = jobs.enqueue(
                            Job.create(
                                realm_id=context.realm_id,
                                project_id=project_id,
                                kind=JobKind.PROVIDER_CALL,
                                idempotency_key=(f"capability:{plan.plan_digest}:{episode_key}"),
                                resources=parse_requests(write=(*slot_resources, resource)),
                                required_capabilities=(f"provider.capability.{episode_key}",),
                                max_attempts=1,
                                work_item_id=work_id,
                                plan_id=task_plan.id,
                                step_id=f"capability-episode-{episode_key}",
                                payload={
                                    "cohort_id": str(cohort_id),
                                    "model_id": model_id,
                                    "task_digest": task.task_digest,
                                    "plan_digest": plan.plan_digest,
                                },
                            )
                        )
                        if not created:
                            raise ZekamError("Capability episode job replay reddedildi")
                        episode_jobs[(model_id, task.task_digest)] = job
                        episode_plan_digest = digest(
                            {
                                "plan_digest": plan.plan_digest,
                                "model_id": model_id,
                                "task_digest": task.task_digest,
                                "effect": "episode-ledger",
                            }
                        )
                        episode_effect_digest = digest(
                            {
                                "cohort_id": cohort_id,
                                "resource": resource,
                                "effect": "episode-ledger-write",
                            }
                        )
                        authorization = _issue_authorization(
                            realm_id=context.realm_id,
                            actor_id=actor_id,
                            work_id=work_id,
                            task_plan_id=task_plan.id,
                            plan_digest=episode_plan_digest,
                            effect_digest=episode_effect_digest,
                            scope=AuthorizationScope(
                                allowed_resources=(resource,),
                                allowed_effects=(EffectKind.DATABASE_WRITE.value,),
                                data_classifications=(DataClassification.PUBLIC,),
                            ),
                            risk="high",
                        )
                        authorizations.issue(authorization)
                        episode_authorizations.append(authorization)

                coordinator_resource = _cohort_resource(cohort_id)
                coordinator, coordinator_created = jobs.enqueue(
                    Job.create(
                        realm_id=context.realm_id,
                        project_id=project_id,
                        kind=JobKind.VERIFICATION,
                        idempotency_key=f"capability:{plan.plan_digest}:finalize",
                        resources=parse_requests(write=(coordinator_resource,)),
                        required_capabilities=("model.capability.finalize",),
                        max_attempts=1,
                        work_item_id=work_id,
                        plan_id=task_plan.id,
                        step_id="capability-finalize",
                        payload={
                            "cohort_id": str(cohort_id),
                            "plan_digest": plan.plan_digest,
                        },
                    )
                )
                if not coordinator_created:
                    raise ZekamError("Capability coordinator job replay reddedildi")
                finalize_plan_digest = digest(
                    {"plan_digest": plan.plan_digest, "effect": "cohort-finalize"}
                )
                finalize_effect_digest = digest(
                    {"cohort_id": cohort_id, "effect": "scorecard-finalize-write"}
                )
                finalize_authorization = _issue_authorization(
                    realm_id=context.realm_id,
                    actor_id=actor_id,
                    work_id=work_id,
                    task_plan_id=task_plan.id,
                    plan_digest=finalize_plan_digest,
                    effect_digest=finalize_effect_digest,
                    scope=AuthorizationScope(
                        allowed_resources=(coordinator_resource,),
                        allowed_effects=(EffectKind.DATABASE_WRITE.value,),
                        data_classifications=(DataClassification.PUBLIC,),
                    ),
                    risk="high",
                )
                authorizations.issue(finalize_authorization)

                runtime_slots: list[CapabilityRuntimeSlot] = []
                for ordinal, slot in enumerate(live_manifest.slots, start=1):
                    endpoint_identity_digest = slot.prepared.plan.endpoint_binding_digest
                    if endpoint_identity_digest is None:
                        raise ZekamError("Capability slot endpoint binding eksik")
                    runtime_slots.append(
                        CapabilityRuntimeSlot(
                            model_id=slot.model_id,
                            task_digest=slot.task_digest,
                            turn_number=slot.turn_index,
                            ordinal=ordinal,
                            job_id=episode_jobs[(slot.model_id, slot.task_digest)].id,
                            provider_ref=slot.prepared.plan.provider_ref,
                            backend_model=slot.backend_model,
                            endpoint_resource=slot.prepared.plan.target,
                            call_resource=slot.prepared.plan.call_resource,
                            endpoint_identity_digest=endpoint_identity_digest,
                            operation=slot.prepared.plan.operation,
                            call_id=slot.prepared.plan.call_id,
                            fixture_digest=slot.prepared.plan.fixture_digest,
                            fixture_identity_digest=(
                                slot.prepared.plan.fixture_identity_digest
                                or slot.prepared.plan.fixture_digest
                            ),
                            max_output_tokens=slot.output_cap,
                            request_template=dict(slot.template_material),
                            request_template_digest=slot.template_digest,
                            derivation_rule_digest=slot.derivation_digest,
                            chain_seed_digest=_capability_chain_seed_digest(
                                plan_digest=plan.plan_digest,
                                model_id=slot.model_id,
                                task_digest=slot.task_digest,
                            ),
                        )
                    )
                approval_evidence_digest = digest(
                    {
                        "plan_digest": plan.plan_digest,
                        "live_manifest_digest": live_manifest.manifest_digest,
                        "task_plan_digest": task_plan.plan_digest,
                        "policy_digest": candidate_policy.policy_digest,
                        "provider_authorizations": [],
                        "runtime_slot_digests": [row.slot_digest for row in runtime_slots],
                        "episode_authorizations": [
                            row.authorization_digest for row in episode_authorizations
                        ],
                        "finalize_authorization": (finalize_authorization.authorization_digest),
                    }
                )
                approval_manifest = CapabilityRuntimeApprovalManifest(
                    cohort_id=cohort_id,
                    work_item_id=work_id,
                    task_plan_id=task_plan.id,
                    coordinator_job_id=coordinator.id,
                    source_revision=current_source_revision,
                    model_ids=plan.model_ids,
                    task_digests=tuple(sorted(task.task_digest for task in plan.registry.tasks)),
                    approval_evidence_digest=approval_evidence_digest,
                )
                manifest_id, manifest_created = runtime_repository.ensure_manifest(
                    approval_manifest, tuple(runtime_slots)
                )
        document: dict[str, Any] = {
            "schema": "zekam-capability-runtime-authorization/v1",
            "status": "authorized",
            "manifest_id": str(manifest_id),
            "manifest_created": manifest_created,
            "manifest_digest": approval_manifest.manifest_digest,
            "live_manifest_digest": live_manifest.manifest_digest,
            "cohort_id": str(cohort_id),
            "cohort_created": cohort_created,
            "project_id": str(project_id),
            "work_id": str(work_id),
            "task_plan_id": str(task_plan.id),
            "source_head": source_head,
            "source_revision": current_source_revision,
            "episode_job_count": len(episode_jobs),
            "coordinator_job_id": str(coordinator.id),
            "provider_authorization_count": 0,
            "episode_ledger_authorization_count": len(episode_authorizations),
            "finalize_authorization_count": 1,
            "maximum_provider_calls": len(runtime_slots),
            "max_retries": 0,
            "provider_calls_made": 0,
            "network_calls_made": 0,
            "grants_authority": False,
        }
        typer.echo(json.dumps(document, ensure_ascii=False, sort_keys=True))
    except ZekamError as exc:
        raise fail_from(exc) from exc
    except PsycopgError as exc:
        raise fail(f"Capability database operation failed [{exc.sqlstate or 'unknown'}]") from exc


@app.command("status")
def status_command(
    cohort_id: Annotated[UUID, typer.Option("--cohort-id")],
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Kalici skor kartlarini gecikme siralamasina zorlamadan listeler."""
    try:
        with RealmSession(home, realm) as context:
            rows = ModelCapabilityRepository(context.connection, context.realm_id).scorecards(
                cohort_id
            )
        payload = {"cohort_id": str(cohort_id), "scorecards": list(rows), "count": len(rows)}
        typer.echo(
            json.dumps(payload, ensure_ascii=False, sort_keys=True) if json_output else payload
        )
    except ZekamError as exc:
        raise fail_from(exc) from exc
    except PsycopgError as exc:
        raise fail(f"Capability database operation failed [{exc.sqlstate or 'unknown'}]") from exc


@app.command("run")
def run_command(
    manifest_id: Annotated[UUID, typer.Option("--manifest-id")],
    cohort_id: Annotated[UUID, typer.Option("--cohort-id")],
    project_id: Annotated[UUID, typer.Option("--project-uuid")],
    work_id: Annotated[UUID, typer.Option("--work")],
    plan_id: Annotated[UUID, typer.Option("--plan-id")],
    config_file: Annotated[Path | None, typer.Option("--config")] = None,
    scope_file: Annotated[Path | None, typer.Option("--scope")] = None,
    apply: Annotated[bool, typer.Option("--uygula")] = False,
    home: Annotated[str | None, typer.Option("--home", help=HOME_HELP)] = None,
    realm: Annotated[str, typer.Option("--realm", help=REALM_HELP)] = DEFAULT_REALM_SLUG,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Onceden authorize edilmis exact 168 slotu uc yedi-model dalgada yurutur."""

    if not apply:
        raise fail("Capability run --uygula ister", 64)
    del json_output
    approval_manifest: CapabilityRuntimeApprovalManifest | None = None
    try:
        plan = _plan(home, realm)
        root = core_root()
        _, _, fixtures = load_capability_registry(
            root / "config" / "model_capability_benchmark.yaml",
            repository_root=root,
        )
        _, campaign_manifest = _load_campaign_manifest(
            config_file=config_file, scope_file=scope_file
        )
        live_manifest = prepare_capability_live_manifest(plan, fixtures, campaign_manifest)
        _, current_source_revision = _source_revision()
        if plan.source_revision != current_source_revision:
            raise PolicyViolation("Capability live run source HEAD drift")
        with RealmSession(home, realm) as context:
            repository = ModelCapabilityRuntimeRepository(context.connection, context.realm_id)
            approval_manifest = repository.manifest(manifest_id)
            if (
                approval_manifest.cohort_id != cohort_id
                or approval_manifest.work_item_id != work_id
                or approval_manifest.task_plan_id != plan_id
                or approval_manifest.source_revision != current_source_revision
                or approval_manifest.manifest_digest
                != repository.manifest_for_cohort(cohort_id)[1].manifest_digest
                or live_manifest.plan_digest != plan.plan_digest
            ):
                raise PolicyViolation("Capability run exact manifest/cohort/work/plan drift")
            graph = WorkGraphService(context.connection, context.realm)
            work = graph.items.get(work_id)
            if work.project_id != project_id:
                raise PolicyViolation("Capability run project/work drift")
            with context.connection.cursor() as cursor:
                cursor.execute(
                    "select 1 from models.capability_runtime_outcome"
                    " where realm_id=%s and manifest_id=%s",
                    (context.realm_id, manifest_id),
                )
                terminal_runtime = cursor.fetchone()
            if terminal_runtime is not None:
                raise PolicyViolation("Capability completed/recovery manifest replay reddedildi")

        episode_results: list[CapabilityEpisodeResult] = []
        for task in plan.registry.tasks:
            # Barrier: bir sonraki role/task dalgasi ancak yedi lane terminal olunca baslar.
            wave_barrier = threading.Barrier(7)
            wave_start_ns: list[int] = []
            wave_start_lock = threading.Lock()
            with ThreadPoolExecutor(max_workers=7) as pool:
                futures = tuple(
                    pool.submit(
                        _execute_live_episode,
                        home=home,
                        realm=realm,
                        project_id=project_id,
                        manifest_id=manifest_id,
                        approval_manifest=approval_manifest,
                        plan=plan,
                        live_manifest=live_manifest,
                        fixtures=fixtures,
                        model_id=model_id,
                        task_digest=task.task_digest,
                        wave_barrier=wave_barrier,
                        wave_start_ns=wave_start_ns,
                        wave_start_lock=wave_start_lock,
                    )
                    for model_id in plan.model_ids
                )
                wave = [future.result() for future in futures]
            if len(wave) != 7:
                raise PolicyViolation("Capability wave exact yedi terminal episode ister")
            episode_results.extend(wave)

        if len(episode_results) != 21:
            raise PolicyViolation("Capability run exact 21 episode ister")
        with RealmSession(home, realm) as context:
            runtime_repository = ModelCapabilityRuntimeRepository(
                context.connection, context.realm_id
            )
            authorizations = AuthorizationRepository(context.connection, context.realm_id)
            all_authorizations = _authorizations_for_plan(
                context.connection, authorizations, plan_id=plan_id
            )
            finalize_plan_digest = digest(
                {"plan_digest": plan.plan_digest, "effect": "cohort-finalize"}
            )
            finalize_authorization = next(
                row for row in all_authorizations if row.plan_digest == finalize_plan_digest
            )
            host = ExecutionHost(
                context.connection,
                context.realm_id,
                worker_label="capability-finalize",
            )
            claimed = host.acquire_work(
                capabilities=("model.capability.finalize",), lease_seconds=300
            )
            if claimed is None or claimed.job.plan_id != plan_id:
                raise PolicyViolation("Capability finalize exact coordinator claim edilemedi")
            coordinator_resource = _cohort_resource(cohort_id)
            claim = host.claim_effect(
                claimed,
                operation="capability-scorecard-finalize",
                effect_digest=finalize_authorization.effect_digest,
                authorization_digest=finalize_authorization.authorization_digest,
                authorization_id=finalize_authorization.id,
                resources=parse_requests(write=(coordinator_resource,)),
                adapter_digest=digest("capability-finalize/v1"),
            )
            consumed = authorizations.consume(
                finalize_authorization.id,
                effect_digest=finalize_authorization.effect_digest,
                consumed_by="cli:model-capability:finalize",
            )
            if not consumed.consumed:
                raise PolicyViolation("Capability finalize authority tuketilemedi")
            scorecards = []
            capability_repository = ModelCapabilityRepository(context.connection, context.realm_id)
            for model_id in plan.model_ids:
                model_result = aggregate_capability_episodes(
                    plan,
                    model_id,
                    tuple(row for row in episode_results if row.model_id == model_id),
                )
                scorecards.append(model_result)
            with context.connection.cursor() as cursor:
                cursor.execute(
                    "select s.model_id,s.task_digest,s.turn_number,c.evidence_digest"
                    " from models.capability_runtime_approval_slot s"
                    " join models.capability_runtime_call_outcome c"
                    " on c.realm_id=s.realm_id and c.slot_id=s.id"
                    " where s.realm_id=%s and s.manifest_id=%s order by s.ordinal",
                    (context.realm_id, manifest_id),
                )
                call_rows = cursor.fetchall()
                call_evidence = tuple(sorted(str(row[3]) for row in call_rows))
                call_result_by_key = {
                    (str(row[0]), str(row[1]), int(row[2])): str(row[3]) for row in call_rows
                }
            actual_provider_calls = len(call_evidence)
            if not 21 <= actual_provider_calls <= 168:
                raise PolicyViolation("Capability finalize provider evidence siniri gecersiz")
            failed_episode_by_key = {
                (row.model_id, row.task_digest): row
                for row in episode_results
                if row.status.value != "passed"
            }
            contract_failed_episode_count = len(failed_episode_by_key)
            skipped_slot_count = 168 - actual_provider_calls
            result_digest = digest(
                {
                    "manifest_digest": approval_manifest.manifest_digest,
                    "episode_evidence": sorted(row.evidence_digest for row in episode_results),
                    "scorecards": sorted(row.evidence_digest for row in scorecards),
                    "call_evidence": call_evidence,
                }
            )
            plan_steps = tuple(row.step_id for row in _runtime_steps(plan, live_manifest))
            final_checkpoint = Checkpoint(
                checkpoint_id=f"capability-final-{manifest_id}",
                project_id=str(project_id),
                work_item_id=str(work_id),
                plan_revision_id=str(plan_id),
                source_revision=approval_manifest.source_revision,
                plan_steps=plan_steps,
                completed_steps=plan_steps,
                pending_steps=(),
                step_results=(
                    *(
                        (
                            slot.prepared.plan.call_id,
                            call_result_by_key[(slot.model_id, slot.task_digest, slot.turn_index)]
                            if (slot.model_id, slot.task_digest, slot.turn_index)
                            in call_result_by_key
                            else digest(
                                {
                                    "status": "skipped-model-contract",
                                    "slot_key": slot.slot_key,
                                    "episode_evidence_digest": failed_episode_by_key[
                                        (slot.model_id, slot.task_digest)
                                    ].evidence_digest,
                                }
                            ),
                        )
                        for slot in live_manifest.slots
                    ),
                    *(
                        (
                            f"capability-episode-{_episode_key(row.model_id, row.task_digest)}",
                            row.evidence_digest,
                        )
                        for row in episode_results
                    ),
                    ("capability-finalize", result_digest),
                ),
                context_manifest_digest=live_manifest.manifest_digest,
                journal_head_digest=result_digest,
                next_safe_action="capability-independent-verification",
                created_at=dt.datetime.now(dt.UTC),
            )
            with context.connection.transaction():
                host.record_success(
                    claim,
                    result_digest=result_digest,
                    adapter_evidence_digest=result_digest,
                )
                ContextContinuityRepository(
                    context.connection,
                    context.realm_id,
                    project_id,
                    work_id,
                ).store_checkpoint(
                    final_checkpoint,
                    task_plan_id=plan_id,
                    job_id=claimed.job.id,
                )
                if not host.finish(
                    claimed,
                    outcome=AttemptOutcome.SUCCEEDED,
                    result_digest=result_digest,
                ):
                    raise PolicyViolation("Capability finalize job finish reddedildi")
                runtime_outcome = CapabilityRuntimeOutcome(
                    status=CapabilityRuntimeStatus.COMPLETED,
                    actual_provider_calls=actual_provider_calls,
                    actual_retries=0,
                    call_evidence_digests=call_evidence,
                    evidence_digest=result_digest,
                    completed_at=dt.datetime.now(dt.UTC),
                    successful_episode_count=21 - contract_failed_episode_count,
                    contract_failed_episode_count=contract_failed_episode_count,
                    skipped_slot_count=skipped_slot_count,
                )
                runtime_repository.finalize_outcome(manifest_id, runtime_outcome)
                for model_result in scorecards:
                    capability_repository.record_scorecard(cohort_id, model_result)
        document = {
            "schema": "zekam-capability-runtime-run/v1",
            "status": "completed-calibration",
            "manifest_id": str(manifest_id),
            "cohort_id": str(cohort_id),
            "episode_count": 21,
            "provider_calls_made": actual_provider_calls,
            "network_calls_made": actual_provider_calls,
            "actual_retries": 0,
            "score_eligible": True,
            "routing_eligible": False,
            "scorecards": [
                {
                    "model_id": row.model_id,
                    "general_score": row.general_score,
                    "role_scores": {role.value: score for role, score in row.role_scores},
                    "completion_rate": row.completion_rate,
                    "disqualified": row.completion_rate < 1,
                    "evidence_digest": row.evidence_digest,
                }
                for row in sorted(scorecards, key=lambda item: (-item.general_score, item.model_id))
            ],
            "raw_prompt_values_reported": 0,
            "raw_response_values_reported": 0,
            "secret_values_reported": 0,
            "grants_authority": False,
        }
        typer.echo(json.dumps(document, ensure_ascii=False, sort_keys=True))
    except ZekamError as exc:
        if approval_manifest is not None:
            _seal_runtime_failure(
                home=home,
                realm=realm,
                manifest_id=manifest_id,
                approval_manifest=approval_manifest,
                error=exc,
            )
        raise fail_from(exc) from exc
    except PsycopgError as exc:
        if approval_manifest is not None:
            _seal_runtime_failure(
                home=home,
                realm=realm,
                manifest_id=manifest_id,
                approval_manifest=approval_manifest,
                error=exc,
            )
        raise fail(f"Capability database operation failed [{exc.sqlstate or 'unknown'}]") from exc
    except Exception as exc:
        if approval_manifest is not None:
            _seal_runtime_failure(
                home=home,
                realm=realm,
                manifest_id=manifest_id,
                approval_manifest=approval_manifest,
                error=exc,
            )
        raise fail("Capability runtime execution failed") from exc
