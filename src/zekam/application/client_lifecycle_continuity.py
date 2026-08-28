"""ClaimedWork-bound PostgreSQL admission for one immutable lifecycle delivery."""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from zekam.application.client_lifecycle_bridge import (
    ClientLifecycleBridge,
    LifecycleBridgePlan,
)
from zekam.application.client_lifecycle_spool import (
    CONTINUITY_BINDING_SCHEMA,
    CONTINUITY_PREFLIGHT_SCHEMA,
    CanonicalLifecycleReceipt,
    LifecycleSpoolEntry,
)
from zekam.application.execution import ExecutionHost
from zekam.application.hook_runtime import HookSession
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.clients import ClientKind
from zekam.domain.errors import PolicyViolation
from zekam.domain.runtime import AttemptOutcome, EffectClaim, ReceiptStatus
from zekam.infrastructure.postgres.client_lifecycle_repository import (
    ActiveLifecycleExecution,
    ClientLifecycleRepository,
)
from zekam.infrastructure.postgres.runtime_repository import ClaimedWork

LIFECYCLE_EFFECT_OPERATION = "client-lifecycle-drain"
LIFECYCLE_ADAPTER_DIGEST = digest(
    {"adapter": "claimedwork-codex-lifecycle", "version": 1}
)


@dataclass(frozen=True, slots=True)
class ClaimedLifecycleDelivery:
    """Process-local owner token plus immutable canonical identities for one effect."""

    work: ClaimedWork
    claim: EffectClaim
    authorization_id: UUID
    plan: LifecycleBridgePlan
    hook_session: HookSession
    session_binding_id: UUID
    client_instance_id: str
    work_plan_digest: str


@dataclass(frozen=True, slots=True)
class PostgresLifecycleContinuityAdmission:
    """Fail-closed three-phase adapter used by ``drain_to_postgres``.

    The effect claim is deliberately supplied by the governed worker.  This
    adapter never claims a job, invents an owner token, or retries an uncertain
    claim.  A new effect is admitted only while that exact claim is receiptless;
    a completed replay is reconstructed from immutable terminal rows.
    """

    connection: Any
    realm_id: UUID
    bridge: ClientLifecycleBridge
    repository: ClientLifecycleRepository
    delivery: ClaimedLifecycleDelivery

    def preflight(
        self,
        entry: LifecycleSpoolEntry,
        canonical_event: Mapping[str, Any],
        *,
        client_instance_id: str,
    ) -> Mapping[str, Any]:
        self._assert_uow_identity()
        self._assert_input(entry, canonical_event, client_instance_id=client_instance_id)
        receipt = ExecutionHost(self.connection, self.realm_id).ledger.receipt_for_claim(
            self.delivery.claim.id
        )
        if receipt is None:
            execution = self._current_execution(entry, now=dt.datetime.now(dt.UTC))
            self._assert_plan_current(execution)
        elif receipt.status is ReceiptStatus.COMPLETED:
            # A crash after the atomic commit is a lookup replay, never a retry.
            self._terminal_receipt(entry, canonical_event)
        else:
            raise PolicyViolation("Lifecycle failed terminal claim sessiz retry edilemez")
        body = {
            "schema": CONTINUITY_PREFLIGHT_SCHEMA,
            "entry_digest": entry.entry_digest,
            "canonical_event_digest": str(canonical_event["event_digest"]),
            "client_instance_id": client_instance_id,
            "realm_id": str(self.realm_id),
            "project_id": str(self.delivery.plan.event.project_id),
            "work_item_id": str(self.delivery.plan.event.work_item_id),
            "run_id": str(self.delivery.plan.event.run_id),
            "authorization_id": str(self.delivery.authorization_id),
            "job_id": str(self.delivery.work.job.id),
            "claim_id": str(self.delivery.claim.id),
            "plan_digest": self.delivery.plan.plan_digest,
            "effect_digest": self.delivery.plan.effect_digest,
            "allowed": True,
            "mutation_performed": False,
            "grants_authority": False,
        }
        return body | {"preflight_digest": digest(body)}

    def apply(
        self,
        entry: LifecycleSpoolEntry,
        canonical_event: Mapping[str, Any],
        *,
        preflight: Mapping[str, Any],
        client_instance_id: str,
        now: dt.datetime,
    ) -> CanonicalLifecycleReceipt:
        self._assert_uow_identity()
        self._assert_input(entry, canonical_event, client_instance_id=client_instance_id)
        self._assert_preflight(preflight)
        host = ExecutionHost(
            self.connection,
            self.realm_id,
            worker_label=self.delivery.work.lease.worker_label,
        )
        existing = host.ledger.receipt_for_claim(self.delivery.claim.id)
        if existing is not None:
            if existing.status is not ReceiptStatus.COMPLETED:
                raise PolicyViolation("Lifecycle terminal failed claim yeniden uygulanamaz")
            return self._terminal_receipt(entry, canonical_event)

        with self._recover_on_failure(host), self.connection.transaction():
            execution = self._current_execution(entry, now=now)
            self._assert_plan_current(execution)
            canonical_ack = self.repository.ingest(
                canonical_event,
                client_instance_id=client_instance_id,
                client_kind=ClientKind.CODEX,
                now=now,
            )
            applied = self.bridge.apply(
                self.delivery.plan,
                self.delivery.hook_session,
                session_binding_id=self.delivery.session_binding_id,
                authorization_id=self.delivery.authorization_id,
                current_source_digest=execution.source_digest,
                current_policy_digest=execution.policy_digest,
                current_migration_digest=execution.migration_digest,
                now=now,
            )
            # Hook execution may consume time.  Re-read lease/fence/lock/run,
            # now with the exact consumed authorization, before terminal writes.
            post_hook_execution = self._current_execution(
                entry,
                now=dt.datetime.now(dt.UTC),
                allow_consumed=True,
            )
            self._assert_plan_current(post_hook_execution)
            hook_output = self.repository.lookup_hook_terminal_output(
                session_binding_id=self.delivery.session_binding_id,
                event_type=self.delivery.plan.event.event_type,
                input_digest=digest(self.delivery.plan.hook_payload),
            )
            terminal_at = dt.datetime.now(dt.UTC)
            if entry.internal_event_type == "pre_compaction" and not hook_output.compiler_enqueue:
                raise PolicyViolation("Pre-compaction durable compiler enqueue uretmedi")
            self.bridge.repository.finalize_lifecycle_delivery(
                outbox_id=applied.outbox_id,
                receipt_digest=hook_output.output_digest,
                status="completed",
                completed_at=terminal_at,
            )
            result_digest = digest(
                {
                    "schema": "zekam-client-lifecycle-effect-result/v1",
                    "entry_digest": entry.entry_digest,
                    "canonical_ack_digest": canonical_ack.canonical_digest,
                    "bridge_result_digest": _bridge_result_formula(
                        plan_digest=applied.plan_digest,
                        event_digest=applied.event_digest,
                        event_id=applied.event_id,
                        outbox_id=applied.outbox_id,
                        hook_receipt_id=hook_output.receipt_id,
                        hook_output_digest=hook_output.output_digest,
                    ),
                    "terminal_hook_receipt_digest": hook_output.output_digest,
                    "compiler_enqueue": hook_output.compiler_enqueue,
                    "grants_authority": False,
                }
            )
            effect_receipt = host.record_success(
                self.delivery.claim,
                result_digest=result_digest,
                adapter_evidence_digest=digest(
                    {
                        "adapter": "claimedwork-codex-lifecycle/v1",
                        "entry_digest": entry.entry_digest,
                        "plan_digest": self.delivery.plan.plan_digest,
                        "terminal_hook_receipt_digest": hook_output.output_digest,
                    }
                ),
                now=terminal_at,
            )
            step_id = self.delivery.work.job.step_id
            if step_id is None:
                raise PolicyViolation("Lifecycle claimed job exact step_id tasimali")
            self.repository.store_job_checkpoint(
                execution=execution,
                job_id=self.delivery.work.job.id,
                step_id=step_id,
                result_digest=result_digest,
                now=terminal_at,
            )
            if not host.finish(
                self.delivery.work,
                outcome=AttemptOutcome.SUCCEEDED,
                result_digest=result_digest,
                now=terminal_at,
            ):
                raise PolicyViolation("Lifecycle claimed job terminal finish reddedildi")
            self.repository.record_governed_admission(
                lifecycle_event_id=canonical_ack.event_id,
                entry_digest=entry.entry_digest,
                continuity_event_id=applied.event_id,
                delivery_outbox_id=applied.outbox_id,
                hook_receipt_id=hook_output.receipt_id,
                job_id=self.delivery.work.job.id,
                attempt_id=self.delivery.work.attempt_id,
                envelope_id=execution.envelope_id,
                authorization_id=self.delivery.authorization_id,
                claim_id=self.delivery.claim.id,
                effect_receipt_id=effect_receipt.id,
                work_plan_digest=self.delivery.work_plan_digest,
                effect_plan_digest=self.delivery.plan.plan_digest,
                effect_plan_body=self.delivery.plan.body(),
                effect_digest=self.delivery.plan.effect_digest,
                source_digest=self.delivery.plan.source_digest,
                policy_digest=self.delivery.plan.policy_digest,
                migration_digest=self.delivery.plan.migration_digest,
                envelope_digest=execution.envelope_digest,
                terminal_hook_receipt_digest=hook_output.output_digest,
                result_formula_digest=result_digest,
                now=terminal_at,
            )
        return self._terminal_receipt(entry, canonical_event)

    @contextmanager
    def _recover_on_failure(self, host: ExecutionHost):
        """Turn a receiptless, possibly-started claim into explicit recovery state."""

        try:
            yield
        except Exception as exc:
            if host.ledger.receipt_for_claim(self.delivery.claim.id) is None:
                finished = host.finish(
                    self.delivery.work,
                    outcome=AttemptOutcome.RECOVERY_REQUIRED,
                    result_digest=digest(
                        {
                            "schema": "zekam-client-lifecycle-recovery-required/v1",
                            "claim_id": str(self.delivery.claim.id),
                            "effect_digest": self.delivery.claim.effect_digest,
                            "reason": "atomic-apply-did-not-produce-terminal-receipt",
                        }
                    ),
                )
                if not finished:
                    raise PolicyViolation(
                        "Lifecycle recovery-required finish owner/fence tarafindan reddedildi"
                    ) from exc
            raise

    def lookup(
        self,
        entry: LifecycleSpoolEntry,
        canonical_event: Mapping[str, Any],
        *,
        preflight: Mapping[str, Any],
        client_instance_id: str,
    ) -> CanonicalLifecycleReceipt:
        self._assert_uow_identity()
        self._assert_input(entry, canonical_event, client_instance_id=client_instance_id)
        self._assert_preflight(preflight)
        return self._terminal_receipt(entry, canonical_event)

    def _current_execution(
        self,
        entry: LifecycleSpoolEntry,
        *,
        now: dt.datetime,
        allow_consumed: bool = False,
    ) -> ActiveLifecycleExecution:
        work = self.delivery.work
        return self.repository.current_execution(
            job_id=work.job.id,
            attempt_id=work.attempt_id,
            lease_id=work.lease.id,
            owner_digest=work.lease.owner_digest,
            fencing_token=work.lease.fencing_token,
            claim_id=self.delivery.claim.id,
            authorization_id=self.delivery.authorization_id,
            effect_plan_digest=self.delivery.plan.plan_digest,
            work_plan_digest=self.delivery.work_plan_digest,
            effect_digest=self.delivery.plan.effect_digest,
            operation=LIFECYCLE_EFFECT_OPERATION,
            adapter_digest=LIFECYCLE_ADAPTER_DIGEST,
            claim_digest=self.delivery.claim.claim_digest,
            authorization_digest=self.delivery.claim.authorization_digest,
            source_digest=self.delivery.plan.source_digest,
            policy_digest=self.delivery.plan.policy_digest,
            migration_digest=self.delivery.plan.migration_digest,
            resource=self.delivery.plan.resource,
            session_id=entry.session_id,
            now=now,
            allow_consumed=allow_consumed,
        )

    def _assert_plan_current(self, execution: ActiveLifecycleExecution) -> None:
        plan = self.delivery.plan
        work = self.delivery.work
        claim = self.delivery.claim
        if (
            execution.project_id != plan.event.project_id
            or execution.work_item_id != plan.event.work_item_id
            or execution.run_id != plan.event.run_id
            or execution.plan_id != work.job.plan_id
            or execution.work_plan_digest != self.delivery.work_plan_digest
            or work.job.project_id != execution.project_id
            or work.job.work_item_id != execution.work_item_id
            or work.job.run_id != execution.run_id
            or claim.job_id != work.job.id
            or claim.attempt_id != work.attempt_id
            or claim.fencing_token != work.lease.fencing_token
            or claim.operation != LIFECYCLE_EFFECT_OPERATION
            or claim.adapter_digest != LIFECYCLE_ADAPTER_DIGEST
            or claim.effect_digest != plan.effect_digest
            or execution.source_digest != plan.source_digest
            or execution.policy_digest != plan.policy_digest
            or execution.migration_digest != plan.migration_digest
        ):
            raise PolicyViolation("Lifecycle plan/ClaimedWork/current execution drift")

    def _terminal_receipt(
        self,
        entry: LifecycleSpoolEntry,
        canonical_event: Mapping[str, Any],
    ) -> CanonicalLifecycleReceipt:
        canonical_ack = self.repository.lookup(str(canonical_event["event_digest"]))
        terminal = self.repository.lookup_terminal_delivery(
            idempotency_key=self.delivery.plan.idempotency_key,
            effect_plan_digest=self.delivery.plan.plan_digest,
            work_plan_digest=self.delivery.work_plan_digest,
            session_binding_id=self.delivery.session_binding_id,
            event_type=self.delivery.plan.event.event_type,
            hook_input_digest=digest(self.delivery.plan.hook_payload),
            job_id=self.delivery.work.job.id,
            attempt_id=self.delivery.work.attempt_id,
            claim_id=self.delivery.claim.id,
            authorization_id=self.delivery.authorization_id,
            effect_digest=self.delivery.plan.effect_digest,
            operation=LIFECYCLE_EFFECT_OPERATION,
            adapter_digest=LIFECYCLE_ADAPTER_DIGEST,
            authorization_digest=self.delivery.claim.authorization_digest,
            fencing_token=self.delivery.work.lease.fencing_token,
            resource=self.delivery.plan.resource,
        )
        effect_receipt = ExecutionHost(
            self.connection, self.realm_id
        ).ledger.receipt_for_claim(self.delivery.claim.id)
        if effect_receipt is None or effect_receipt.status is not ReceiptStatus.COMPLETED:
            raise PolicyViolation("Lifecycle exact completed effect receipt bulunamadi")
        if (
            terminal.effect_receipt_id != effect_receipt.id
            or terminal.effect_result_digest != effect_receipt.result_digest
            or terminal.continuity_event_digest != self.delivery.plan.event.event_digest
        ):
            raise PolicyViolation("Lifecycle terminal lookup effect receipt drift")
        parse_digest(terminal.checkpoint_digest)
        expected_adapter_evidence = digest(
            {
                "adapter": "claimedwork-codex-lifecycle/v1",
                "entry_digest": entry.entry_digest,
                "plan_digest": self.delivery.plan.plan_digest,
                "terminal_hook_receipt_digest": terminal.terminal_receipt_digest,
            }
        )
        if terminal.adapter_evidence_digest != expected_adapter_evidence:
            raise PolicyViolation("Lifecycle terminal adapter evidence drift")
        effect_receipt_digest = digest(effect_receipt.as_dict())
        body = {
            "schema": CONTINUITY_BINDING_SCHEMA,
            "entry_digest": entry.entry_digest,
            "canonical_event_digest": str(canonical_event["event_digest"]),
            "realm_id": str(self.realm_id),
            "project_id": str(self.delivery.plan.event.project_id),
            "work_item_id": str(self.delivery.plan.event.work_item_id),
            "run_id": str(self.delivery.plan.event.run_id),
            "authorization_id": str(self.delivery.authorization_id),
            "job_id": str(self.delivery.work.job.id),
            "claim_id": str(self.delivery.claim.id),
            "plan_digest": self.delivery.plan.plan_digest,
            "effect_digest": self.delivery.plan.effect_digest,
            "effect_receipt_id": str(effect_receipt.id),
            "effect_receipt_digest": effect_receipt_digest,
            "continuity_event_id": str(terminal.continuity_event_id),
            "continuity_event_digest": terminal.continuity_event_digest,
            "delivery_outbox_id": str(terminal.delivery_outbox_id),
            "terminal_receipt_digest": terminal.terminal_receipt_digest,
            "event_type": entry.internal_event_type,
            "session_id": entry.session_id,
            "client_id": entry.client_id,
            "compiler_enqueue": terminal.compiler_enqueue,
            "status": "completed",
            "grants_authority": False,
        }
        generic = CanonicalLifecycleReceipt.verified(
            entry, canonical_event, canonical_ack, canonical_ack
        )
        return generic.bind_continuity(entry, body | {"binding_digest": digest(body)})

    def _assert_input(
        self,
        entry: LifecycleSpoolEntry,
        canonical_event: Mapping[str, Any],
        *,
        client_instance_id: str,
    ) -> None:
        plan_event = self.delivery.plan.event
        event_digest = str(canonical_event.get("event_digest", ""))
        parse_digest(event_digest)
        if (
            client_instance_id != self.delivery.client_instance_id
            or entry.client_id != plan_event.client_id
            or entry.session_id != plan_event.session_id
            or entry.sequence != plan_event.sequence
            or entry.internal_event_type != plan_event.event_type
            or digest(entry.observation) != plan_event.payload_digest
        ):
            raise PolicyViolation("Lifecycle spool/bridge delivery binding drift")

    def _assert_preflight(self, preflight: Mapping[str, Any]) -> None:
        expected = {
            "realm_id": str(self.realm_id),
            "project_id": str(self.delivery.plan.event.project_id),
            "work_item_id": str(self.delivery.plan.event.work_item_id),
            "run_id": str(self.delivery.plan.event.run_id),
            "authorization_id": str(self.delivery.authorization_id),
            "job_id": str(self.delivery.work.job.id),
            "claim_id": str(self.delivery.claim.id),
            "plan_digest": self.delivery.plan.plan_digest,
            "effect_digest": self.delivery.plan.effect_digest,
        }
        if any(preflight.get(key) != value for key, value in expected.items()):
            raise PolicyViolation("Lifecycle preflight exact delivery binding drift")

    def _assert_uow_identity(self) -> None:
        participants = (
            self.repository,
            self.bridge.repository,
            self.bridge.authorizations,
            self.bridge.hook_outcomes,
        )
        if any(getattr(item, "connection", None) is not self.connection for item in participants):
            raise PolicyViolation("Lifecycle UoW tek exact PostgreSQL connection ister")
        if any(getattr(item, "realm_id", None) != self.realm_id for item in participants):
            raise PolicyViolation("Lifecycle UoW participant realm identity drift")


def _bridge_result_formula(
    *,
    plan_digest: str,
    event_digest: str,
    event_id: UUID,
    outbox_id: UUID,
    hook_receipt_id: UUID,
    hook_output_digest: str,
) -> str:
    return digest(
        {
            "schema": "zekam-client-lifecycle-bridge-result/v1",
            "plan_digest": plan_digest,
            "event_digest": event_digest,
            "event_id": str(event_id),
            "outbox_id": str(outbox_id),
            "hook_receipt_id": str(hook_receipt_id),
            "hook_output_digest": hook_output_digest,
            "grants_authority": False,
        }
    )
