"""Governed worker composition for one pre-claimed Codex spool delivery."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import UUID, uuid5

from zekam.application.client_lifecycle_bridge import (
    ClientLifecycleBridge,
    LifecycleClientContract,
    LifecycleRequest,
)
from zekam.application.client_lifecycle_continuity import (
    ClaimedLifecycleDelivery,
    LIFECYCLE_ADAPTER_DIGEST,
    LIFECYCLE_EFFECT_OPERATION,
    PostgresLifecycleContinuityAdmission,
)
from zekam.application.client_lifecycle_spool import (
    CONTINUITY_BINDING_SCHEMA,
    CanonicalLifecycleReceipt,
    ClientLifecycleSpool,
    LifecycleReplayResult,
    canonical_lifecycle_event,
    drain_to_postgres,
)
from zekam.application.execution import ExecutionHost
from zekam.application.hook_runtime import HookRuntime, HookSession
from zekam.application.memory_hooks import memory_hook_bundle
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.runtime import AttemptOutcome, EffectClaim
from zekam.domain.resources import LockMode, ResourceRequest
from zekam.domain.session_continuity import DataClassification
from zekam.infrastructure.postgres.client_lifecycle_repository import (
    ClientLifecycleRepository,
)
from zekam.infrastructure.clients.codex_lifecycle import (
    CODEX_CLIENT_ID,
    CODEX_EVENT_MAPPING,
    CODEX_REVIEWED_VERSION,
    codex_lifecycle_descriptor,
    load_codex_contract_evidence,
)
from zekam.infrastructure.postgres.hook_runtime_repository import HookRuntimeRepository
from zekam.infrastructure.postgres.memory_continuity_repository import (
    MemoryContinuityRepository,
)
from zekam.infrastructure.postgres.runtime_repository import ClaimedWork
from zekam.infrastructure.postgres.security_repository import AuthorizationRepository

_EVENT_NAMESPACE = UUID("81a59570-f0c1-4bd2-b6ce-62647df59e2f")


@dataclass(frozen=True, slots=True)
class LifecyclePlanInputs:
    """Current digests captured by the worker before issuing exact authority."""

    source_revision: str
    source_digest: str
    policy_digest: str
    migration_digest: str
    work_plan_digest: str
    checkpoint_ref: str | None
    context_ref: str | None

    def __post_init__(self) -> None:
        for value in (
            self.source_digest,
            self.policy_digest,
            self.migration_digest,
            self.work_plan_digest,
        ):
            parse_digest(value)


def drain_claimed_codex_delivery(
    *,
    spool: ClientLifecycleSpool,
    bridge: ClientLifecycleBridge,
    repository: ClientLifecycleRepository,
    work: ClaimedWork,
    claim: EffectClaim,
    authorization_id: UUID,
    contract: LifecycleClientContract,
    hook_session: HookSession,
    session_binding_id: UUID,
    inputs: LifecyclePlanInputs,
) -> tuple[LifecycleReplayResult, ...]:
    """Drain only the immutable head assigned to this exact ClaimedWork.

    Job acquisition, effect claim creation and authorization issuance happen in
    the canonical worker before this function is called.  Missing identities
    therefore fail before any spool or PostgreSQL mutation.
    """

    pending = spool.pending(limit=1)
    if not pending:
        host = ExecutionHost(
            repository.connection,
            repository.realm_id,
            worker_label=work.lease.worker_label,
        )
        if host.ledger.receipt_for_claim(claim.id) is None:
            finished = host.finish(
                work,
                outcome=AttemptOutcome.RECOVERY_REQUIRED,
                result_digest=digest(
                    {
                        "schema": "zekam-client-lifecycle-recovery-required/v1",
                        "claim_id": str(claim.id),
                        "reason": "claimed-delivery-missing-from-pending-spool",
                    }
                ),
            )
            if not finished:
                raise PolicyViolation("Missing delivery recovery finish reddedildi")
            raise PolicyViolation(
                "Receiptless claimed Codex delivery pending spool'da bulunamadi"
            )
        return ()
    entry = pending[0]
    if work.job.run_id is None or work.job.work_item_id is None or work.job.plan_id is None:
        raise PolicyViolation("Codex drain exact run/work/plan binding ister")
    if repository.current_work_plan_digest(
        work_item_id=work.job.work_item_id,
        plan_id=work.job.plan_id,
    ) != inputs.work_plan_digest:
        raise PolicyViolation("Codex drain current stored TaskPlan digest drift")
    previous = repository.previous_continuity_digest(
        client_id=entry.client_id,
        session_id=entry.session_id,
        sequence=entry.sequence,
    )
    request = LifecycleRequest(
        realm_id=repository.realm_id,
        project_id=work.job.project_id,
        work_item_id=work.job.work_item_id,
        run_id=work.job.run_id,
        session_id=entry.session_id,
        client_id=entry.client_id,
        event_id=uuid5(_EVENT_NAMESPACE, entry.entry_digest),
        external_event_type=entry.external_event_type,
        sequence=entry.sequence,
        previous_digest=previous,
        origin=f"client:{entry.client_id}",
        causation_id=f"delivery:{entry.delivery_id}",
        correlation_id=f"job:{work.job.id}",
        recursion_depth=0,
        max_recursion_depth=3,
        source_revision=inputs.source_revision,
        work_plan_ref=f"work-plan:{work.job.plan_id}",
        checkpoint_ref=inputs.checkpoint_ref,
        context_ref=inputs.context_ref,
        metadata=(),
        classification=DataClassification.INTERNAL,
        payload=entry.observation,
        idempotency_key=entry.delivery_id,
        occurred_at=entry.occurred_at,
        ingested_at=entry.occurred_at,
    )
    plan = bridge.prepare(
        request,
        contract,
        hook_session,
        source_digest=inputs.source_digest,
        policy_digest=inputs.policy_digest,
        migration_digest=inputs.migration_digest,
    )
    if (
        claim.job_id != work.job.id
        or claim.attempt_id != work.attempt_id
        or claim.fencing_token != work.lease.fencing_token
        or claim.operation != LIFECYCLE_EFFECT_OPERATION
        or claim.adapter_digest != LIFECYCLE_ADAPTER_DIGEST
        or claim.effect_digest != plan.effect_digest
    ):
        raise PolicyViolation("Codex drain exact ClaimedWork/claim/plan binding yok")
    admission = PostgresLifecycleContinuityAdmission(
        repository.connection,
        repository.realm_id,
        bridge,
        repository,
        ClaimedLifecycleDelivery(
            work,
            claim,
            authorization_id,
            plan,
            hook_session,
            session_binding_id,
            spool.client_instance_id(),
            inputs.work_plan_digest,
        ),
    )
    results = drain_to_postgres(
        spool,
        client_instance_id=spool.client_instance_id(),
        continuity_admission=admission,
        limit=1,
    )
    if (
        len(results) != 1
        or results[0].entry_digest != entry.entry_digest
        or results[0].outcome != "completed"
    ):
        host = ExecutionHost(
            repository.connection,
            repository.realm_id,
            worker_label=work.lease.worker_label,
        )
        if host.ledger.receipt_for_claim(claim.id) is None:
            finished = host.finish(
                work,
                outcome=AttemptOutcome.RECOVERY_REQUIRED,
                result_digest=digest(
                    {
                        "schema": "zekam-client-lifecycle-recovery-required/v1",
                        "claim_id": str(claim.id),
                        "reason": "delivery-did-not-complete",
                    }
                ),
            )
            if not finished:
                raise PolicyViolation("Incomplete delivery recovery finish reddedildi")
        raise PolicyViolation("Codex drain exact delivery terminal ACK uretmedi")
    return results


def recover_committed_codex_delivery(
    *,
    spool: ClientLifecycleSpool,
    repository: ClientLifecycleRepository,
) -> LifecycleReplayResult | None:
    """ACK one already-committed delivery without lease, claim, or effect retry."""

    pending = spool.pending(limit=1)
    if not pending:
        return None
    entry = pending[0]
    canonical_event = canonical_lifecycle_event(
        entry,
        client_instance_id=spool.client_instance_id(),
        previous_canonical_event_digest=spool.previous_canonical_event_digest(entry),
    )
    terminal = repository.resolve_committed_delivery(
        entry_digest=entry.entry_digest,
        idempotency_key=entry.delivery_id,
        canonical_event_digest=str(canonical_event["event_digest"]),
    )
    if terminal["client_id"] != "codex" or terminal["session_id"] != entry.session_id:
        raise PolicyViolation("Committed lifecycle terminal Codex session binding drift")
    if terminal["event_type"] != entry.internal_event_type:
        raise PolicyViolation("Committed lifecycle terminal event type drift")
    if terminal["operation"] != LIFECYCLE_EFFECT_OPERATION:
        raise PolicyViolation("Committed lifecycle terminal operation drift")
    if terminal["adapter_digest"] != LIFECYCLE_ADAPTER_DIGEST:
        raise PolicyViolation("Committed lifecycle terminal adapter drift")
    resources = list(terminal["resources"] or [])
    scope = dict(terminal["authorization_scope"] or {})
    effect_plan_body = dict(terminal["effect_plan_body"] or {})
    if (
        len(resources) != 1
        or resources[0].get("mode") != "write"
        or scope
        != {
            "allowed_resources": [resources[0].get("resource")],
            "allowed_effects": ["database-write"],
            "provider_refs": [],
            "secret_ref_ids": [],
            "data_classifications": ["internal"],
        }
    ):
        raise PolicyViolation("Committed lifecycle terminal exact authorization scope drift")
    if (
        digest(effect_plan_body) != terminal["effect_plan_digest"]
        or effect_plan_body.get("schema") != "zekam-lifecycle-bridge-plan/v1"
        or effect_plan_body.get("event_digest") != terminal["continuity_event_digest"]
        or effect_plan_body.get("idempotency_key") != entry.delivery_id
        or effect_plan_body.get("resource") != resources[0].get("resource")
        or effect_plan_body.get("source_digest") != terminal["source_digest"]
        or effect_plan_body.get("policy_digest") != terminal["policy_digest"]
        or effect_plan_body.get("migration_digest") != terminal["migration_digest"]
        or effect_plan_body.get("effect_digest") != terminal["effect_digest"]
        or effect_plan_body.get("grants_authority") is not False
    ):
        raise PolicyViolation("Committed lifecycle effect-plan stored recomputation drift")
    expected_claim = digest(
        {
            "job_id": str(terminal["job_id"]),
            "operation": terminal["operation"],
            "effect_digest": terminal["effect_digest"],
            "authorization_digest": terminal["authorization_digest"],
            "idempotency_key": terminal["claim_idempotency_key"],
            "resources": resources,
            "execution_identity": terminal["execution_identity"],
            "fencing_token": int(terminal["fencing_token"]),
            "adapter_digest": terminal["adapter_digest"],
        }
    )
    if (
        terminal["claim_idempotency_key"] != entry.delivery_id
        or expected_claim != terminal["claim_digest"]
        or terminal["execution_identity"]
        != f"{terminal['worker_label']}:{terminal['fencing_token']}"
        or terminal["work_plan_digest"] != terminal["stored_work_plan_digest"]
    ):
        raise PolicyViolation("Committed lifecycle claim/execution/Work plan recomputation drift")
    bridge_result = digest(
        {
            "schema": "zekam-client-lifecycle-bridge-result/v1",
            "plan_digest": terminal["effect_plan_digest"],
            "event_digest": terminal["continuity_event_digest"],
            "event_id": str(terminal["continuity_event_id"]),
            "outbox_id": str(terminal["delivery_outbox_id"]),
            "hook_receipt_id": str(terminal["hook_receipt_id"]),
            "hook_output_digest": terminal["hook_output_digest"],
            "grants_authority": False,
        }
    )
    generic = repository.lookup(str(canonical_event["event_digest"]))
    expected_result = digest(
        {
            "schema": "zekam-client-lifecycle-effect-result/v1",
            "entry_digest": entry.entry_digest,
            "canonical_ack_digest": generic.canonical_digest,
            "bridge_result_digest": bridge_result,
            "terminal_hook_receipt_digest": terminal["hook_output_digest"],
            "compiler_enqueue": terminal["compiler_enqueue"] is True,
            "grants_authority": False,
        }
    )
    expected_adapter_evidence = digest(
        {
            "adapter": "claimedwork-codex-lifecycle/v1",
            "entry_digest": entry.entry_digest,
            "plan_digest": terminal["effect_plan_digest"],
            "terminal_hook_receipt_digest": terminal["hook_output_digest"],
        }
    )
    if (
        expected_result != terminal["effect_result_digest"]
        or expected_result != terminal["result_formula_digest"]
        or expected_adapter_evidence != terminal["adapter_evidence_digest"]
        or terminal["effect_status"] != "completed"
    ):
        raise PolicyViolation("Committed lifecycle result/adapter evidence recomputation drift")
    effect_receipt_body = {
        "id": str(terminal["effect_receipt_id"]),
        "claim_id": str(terminal["claim_id"]),
        "status": "completed",
        "result_digest": expected_result,
        "failure_category": None,
        "failure_digest": None,
        "adapter_evidence_digest": terminal["adapter_evidence_digest"],
        "token_count": int(terminal["token_count"]),
        "cost_micros": int(terminal["cost_micros"]),
        "latency_ms": int(terminal["latency_ms"]),
        "completed_at": terminal["effect_completed_at"],
    }
    body = {
        "schema": CONTINUITY_BINDING_SCHEMA,
        "entry_digest": entry.entry_digest,
        "canonical_event_digest": str(canonical_event["event_digest"]),
        "realm_id": str(repository.realm_id),
        "project_id": str(terminal["project_id"]),
        "work_item_id": str(terminal["work_item_id"]),
        "run_id": str(terminal["run_id"]),
        "authorization_id": str(terminal["authorization_id"]),
        "job_id": str(terminal["job_id"]),
        "claim_id": str(terminal["claim_id"]),
        "plan_digest": str(terminal["effect_plan_digest"]),
        "effect_digest": str(terminal["effect_digest"]),
        "effect_receipt_id": str(terminal["effect_receipt_id"]),
        "effect_receipt_digest": digest(effect_receipt_body),
        "continuity_event_id": str(terminal["continuity_event_id"]),
        "continuity_event_digest": str(terminal["continuity_event_digest"]),
        "delivery_outbox_id": str(terminal["delivery_outbox_id"]),
        "terminal_receipt_digest": str(terminal["hook_output_digest"]),
        "event_type": entry.internal_event_type,
        "session_id": entry.session_id,
        "client_id": entry.client_id,
        "compiler_enqueue": terminal["compiler_enqueue"] is True,
        "status": "completed",
        "grants_authority": False,
    }
    receipt = CanonicalLifecycleReceipt.verified(
        entry, canonical_event, generic, repository.lookup(str(canonical_event["event_digest"]))
    ).bind_continuity(entry, body | {"binding_digest": digest(body)})
    return spool.acknowledge_committed_receipt(entry, receipt)


def compose_codex_lifecycle_handler(
    *,
    connection: Any,
    realm_id: UUID,
    home: Path,
) -> Callable[[ClaimedWork], str]:
    """Compose the only production queue handler for governed Codex delivery jobs."""

    repository = ClientLifecycleRepository(connection, realm_id)
    continuity = MemoryContinuityRepository(connection, realm_id)
    authorizations = AuthorizationRepository(connection, realm_id)
    hook_store = HookRuntimeRepository(connection, realm_id)
    runtime = HookRuntime(max_workers=1)
    bundle = memory_hook_bundle(realm_id)
    runtime.reconfigure(
        realm_id=realm_id,
        config_effective_digest=bundle.bundle_digest,
        specs=bundle.specs,
        runtimes=bundle.runtimes,
        profiles=(bundle.profile,),
        adapters=bundle.adapters,
        now=dt.datetime.now(dt.UTC),
    )
    bridge = ClientLifecycleBridge(runtime, continuity, authorizations, hook_store)
    evidence = load_codex_contract_evidence(
        Path(__file__).resolve().parents[3]
        / "config"
        / "client-lifecycle"
        / "codex-0.150.1.json"
    )
    contract = LifecycleClientContract.verified(
        descriptor=codex_lifecycle_descriptor(
            "codex", installed_version=CODEX_REVIEWED_VERSION
        ),
        installed_version=CODEX_REVIEWED_VERSION,
        event_mapping=CODEX_EVENT_MAPPING,
        contract_evidence_digest=str(evidence["file_digest"]),
    )
    from zekam.infrastructure.clients.codex_lifecycle import (
        CODEX_REVIEWED_CLIENT_CONTRACT_DIGEST,
    )

    if contract.contract_digest != CODEX_REVIEWED_CLIENT_CONTRACT_DIGEST:
        raise PolicyViolation("Codex reviewed client contract digest drift")
    spool = ClientLifecycleSpool(home, client_id=CODEX_CLIENT_ID)

    def handle(work: ClaimedWork) -> str:
        payload = dict(work.job.payload)
        if frozenset(payload) != frozenset({"schema", "authorization_id"}) or payload.get(
            "schema"
        ) != "zekam-codex-lifecycle-job/v1":
            raise PolicyViolation("Codex lifecycle worker exact immutable job payload ister")
        if work.job.kind.value != "mutation" or work.job.max_attempts != 1:
            raise PolicyViolation("Codex lifecycle job mutation ve max_attempts=1 olmali")
        try:
            authorization_id = UUID(str(payload["authorization_id"]))
        except (ValueError, TypeError) as exc:
            raise PolicyViolation("Codex lifecycle authorization_id UUID olmali") from exc
        entries = spool.pending(limit=1)
        if not entries:
            raise PolicyViolation("Codex lifecycle claimed job icin pending delivery yok")
        entry = entries[0]
        raw_inputs = repository.claimed_plan_inputs(
            job_id=work.job.id,
            attempt_id=work.attempt_id,
            lease_id=work.lease.id,
            owner_digest=work.lease.owner_digest,
            fencing_token=work.lease.fencing_token,
            session_id=entry.session_id,
            now=dt.datetime.now(dt.UTC),
        )
        inputs = LifecyclePlanInputs(
            source_revision=str(raw_inputs["source_revision"]),
            source_digest=str(raw_inputs["source_digest"]),
            policy_digest=str(raw_inputs["policy_digest"]),
            migration_digest=str(raw_inputs["migration_digest"]),
            work_plan_digest=str(raw_inputs["work_plan_digest"]),
            checkpoint_ref=(
                None if raw_inputs["checkpoint_ref"] is None else str(raw_inputs["checkpoint_ref"])
            ),
            context_ref=str(raw_inputs["context_ref"]),
        )
        request = LifecycleRequest(
            realm_id=realm_id,
            project_id=work.job.project_id,
            work_item_id=work.job.work_item_id,
            run_id=work.job.run_id,
            session_id=entry.session_id,
            client_id=entry.client_id,
            event_id=uuid5(_EVENT_NAMESPACE, entry.entry_digest),
            external_event_type=entry.external_event_type,
            sequence=entry.sequence,
            previous_digest=repository.previous_continuity_digest(
                client_id=entry.client_id,
                session_id=entry.session_id,
                sequence=entry.sequence,
            ),
            origin=f"client:{entry.client_id}",
            causation_id=f"delivery:{entry.delivery_id}",
            correlation_id=f"job:{work.job.id}",
            recursion_depth=0,
            max_recursion_depth=3,
            source_revision=inputs.source_revision,
            work_plan_ref=f"work-plan:{work.job.plan_id}",
            checkpoint_ref=inputs.checkpoint_ref,
            context_ref=inputs.context_ref,
            metadata=(),
            classification=DataClassification.INTERNAL,
            payload=entry.observation,
            idempotency_key=entry.delivery_id,
            occurred_at=entry.occurred_at,
            ingested_at=entry.occurred_at,
        )
        hook_session = runtime.start_session()
        plan = bridge.prepare(
            request,
            contract,
            hook_session,
            source_digest=inputs.source_digest,
            policy_digest=inputs.policy_digest,
            migration_digest=inputs.migration_digest,
        )
        authorization = authorizations.get(authorization_id)
        if (
            authorization.work_item_id != work.job.work_item_id
            or authorization.plan_id != work.job.plan_id
            or authorization.plan_digest != plan.plan_digest
            or authorization.effect_digest != plan.effect_digest
        ):
            raise PolicyViolation("Codex lifecycle pre-issued authorization exact plan drift")
        host = ExecutionHost(connection, realm_id, worker_label=work.lease.worker_label)
        claim = host.claim_effect(
            work,
            operation=LIFECYCLE_EFFECT_OPERATION,
            effect_digest=plan.effect_digest,
            authorization_digest=authorization.authorization_digest,
            resources=(ResourceRequest.parse(plan.resource, LockMode.WRITE),),
            adapter_digest=LIFECYCLE_ADAPTER_DIGEST,
            authorization_id=authorization.id,
            idempotency_key=entry.delivery_id,
        )
        session_binding_id = hook_store.start_session(
            session_ref=f"codex:{entry.session_id}:{entry.entry_digest}"
        )
        result = drain_claimed_codex_delivery(
            spool=spool,
            bridge=bridge,
            repository=repository,
            work=work,
            claim=claim,
            authorization_id=authorization.id,
            contract=contract,
            hook_session=hook_session,
            session_binding_id=session_binding_id,
            inputs=inputs,
        )
        if len(result) != 1 or result[0].canonical_ack_digest is None:
            raise PolicyViolation("Codex lifecycle handler terminal ACK uretmedi")
        return str(result[0].canonical_ack_digest)

    return handle
