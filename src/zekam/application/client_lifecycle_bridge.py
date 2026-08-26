"""Common, fail-closed lifecycle bridge for supported client harnesses."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Protocol
from uuid import UUID

from zekam.application.hook_runtime import HookRuntime, HookSession
from zekam.domain.canonical import canonical_bytes, digest, parse_digest
from zekam.domain.clients import ClientDescriptor
from zekam.domain.errors import AuthorizationRequired, PolicyViolation, ValidationFailed
from zekam.domain.hook_runtime import HookEventType, HookRunOutcome
from zekam.domain.security import Authorization
from zekam.domain.session_continuity import (
    DataClassification,
    SessionLifecycleEvent,
    TypedMetadata,
)

_SAFE_EVENT = re.compile(r"^[a-z][a-z0-9_.:-]{0,95}$")
_FORBIDDEN_KEY = re.compile(
    r"(?:^|[_.-])(?:secret|credential|password|private[-_]?key|owner[-_]?token|"
    r"prompt|response|transcript|raw[-_]?content)(?:$|[_.-])",
    re.IGNORECASE,
)
_SENSITIVE_VALUE = re.compile(r"(?i)(?:api[_-]?key|token|secret|password|credential)\s*[:=]\s*\S+")
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|(?:^|\s)/(?:Users|home|root|etc|var)/)")
_MAX_PAYLOAD_BYTES = 16_384
_CONTINUITY_EVENTS = frozenset(
    event
    for event in HookEventType
    if event.value
    in {
        "session_start",
        "hydration_required",
        "hydration_completed",
        "pre_task",
        "post_task",
        "pre_compaction",
        "post_compaction",
        "pre_close",
        "post_close",
        "on_failure",
        "on_validation_failure",
        "on_memory_write_failure",
        "on_memory_hydration_failure",
        "on_skill_candidate",
        "on_skill_update",
        "on_state_drift",
        "unclean_exit",
    }
)


class AuthorizationStore(Protocol):
    def get(self, authorization_id: UUID) -> Authorization: ...

    def consume(
        self,
        authorization_id: UUID,
        *,
        effect_digest: str,
        consumed_by: str,
        now: dt.datetime | None = None,
    ) -> Any: ...


class DeliveryStage(Protocol):
    event_id: UUID
    outbox_id: UUID
    created: bool


class LifecycleDeliveryRepository(Protocol):
    connection: Any

    def stage_lifecycle_delivery(
        self,
        event: SessionLifecycleEvent,
        *,
        idempotency_key: str,
        plan_digest: str,
    ) -> DeliveryStage: ...

    def finalize_lifecycle_delivery(
        self,
        *,
        outbox_id: UUID,
        receipt_digest: str,
        status: str,
        completed_at: dt.datetime,
    ) -> None: ...

    def read_session_snapshot(
        self,
        *,
        project_id: UUID,
        work_item_id: UUID,
        run_id: UUID,
        session_id: str,
        client_id: str,
    ) -> Any: ...


class HookOutcomeStore(Protocol):
    def record_outcome(
        self,
        *,
        session_binding_id: UUID,
        entry: Any,
        outcome: HookRunOutcome,
        input_body: Any,
        output_body: Any | None,
    ) -> tuple[UUID, UUID]: ...


@dataclass(frozen=True, slots=True)
class LifecycleClientContract:
    """Exact installed version plus reviewed event mapping; never inferred."""

    descriptor: ClientDescriptor
    installed_version: str | None
    event_mapping: tuple[tuple[str, HookEventType], ...]
    contract_evidence_digest: str | None
    unsupported_reason: str | None
    grants_authority: bool = False

    @classmethod
    def verified(
        cls,
        *,
        descriptor: ClientDescriptor,
        installed_version: str,
        event_mapping: tuple[tuple[str, HookEventType], ...],
        contract_evidence_digest: str,
    ) -> LifecycleClientContract:
        parse_digest(contract_evidence_digest)
        if (
            not installed_version.strip()
            or descriptor.version != installed_version
            or not descriptor.supports("lifecycle-events-v2")
        ):
            raise PolicyViolation("Lifecycle contract exact kurulu surum/capability ile uyusmuyor")
        normalized = tuple(sorted(event_mapping, key=lambda item: item[0]))
        if not normalized or len({item[0] for item in normalized}) != len(normalized):
            raise ValidationFailed(
                "Lifecycle event mapping dolu ve external event bazinda tekil olmali"
            )
        if any(not _SAFE_EVENT.fullmatch(external) for external, _ in normalized):
            raise ValidationFailed("Lifecycle external event canonical formatta olmali")
        if any(internal not in _CONTINUITY_EVENTS for _, internal in normalized):
            raise ValidationFailed(
                "Lifecycle mapping yalniz versionli continuity event'i kullanabilir"
            )
        return cls(
            descriptor,
            installed_version,
            normalized,
            contract_evidence_digest,
            None,
            False,
        )

    @classmethod
    def unsupported(cls, *, descriptor: ClientDescriptor, reason: str) -> LifecycleClientContract:
        cleaned = reason.strip()
        if not cleaned or len(cleaned) > 160:
            raise ValidationFailed("Lifecycle unsupported reason bounded olmali")
        return cls(descriptor, descriptor.version, (), None, cleaned, False)

    @property
    def contract_digest(self) -> str:
        return digest(self.body())

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-lifecycle-client-contract/v1",
            "client_descriptor_digest": self.descriptor.descriptor_digest,
            "installed_version": self.installed_version,
            "event_mapping": [
                {"external": external, "internal": internal.value}
                for external, internal in self.event_mapping
            ],
            "contract_evidence_digest": self.contract_evidence_digest,
            "unsupported_reason": self.unsupported_reason,
            "grants_authority": False,
        }

    def resolve(self, external_event: str) -> HookEventType:
        if self.unsupported_reason is not None:
            raise PolicyViolation(f"Lifecycle client unsupported: {self.unsupported_reason}")
        for candidate, event_type in self.event_mapping:
            if candidate == external_event:
                return event_type
        raise PolicyViolation(f"Lifecycle event unsupported: {external_event}")


@dataclass(frozen=True, slots=True)
class LifecycleRequest:
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    run_id: UUID
    session_id: str
    client_id: str
    event_id: UUID
    external_event_type: str
    sequence: int
    previous_digest: str | None
    origin: str
    causation_id: str
    correlation_id: str
    recursion_depth: int
    max_recursion_depth: int
    source_revision: str
    work_plan_ref: str
    checkpoint_ref: str | None
    context_ref: str | None
    metadata: tuple[TypedMetadata, ...]
    classification: DataClassification
    payload: dict[str, Any]
    idempotency_key: str
    occurred_at: dt.datetime
    ingested_at: dt.datetime


@dataclass(frozen=True, slots=True)
class LifecycleAdmission:
    allowed: bool
    event_type: HookEventType | None
    reason: str
    decision_digest: str
    grants_authority: bool = False


@dataclass(frozen=True, slots=True)
class LifecycleBridgePlan:
    event: SessionLifecycleEvent
    hook_payload: dict[str, Any]
    client_contract_digest: str
    hook_generation: int
    hook_set_digest: str
    hook_ids: tuple[str, ...]
    idempotency_key: str
    resource: str
    source_digest: str
    policy_digest: str
    migration_digest: str
    effect_digest: str
    plan_digest: str
    grants_authority: bool = False

    @classmethod
    def create(
        cls,
        *,
        event: SessionLifecycleEvent,
        hook_payload: dict[str, Any],
        client_contract_digest: str,
        session: HookSession,
        hook_ids: tuple[str, ...],
        idempotency_key: str,
        source_digest: str,
        policy_digest: str,
        migration_digest: str,
    ) -> LifecycleBridgePlan:
        for value in (client_contract_digest, source_digest, policy_digest, migration_digest):
            parse_digest(value)
        resource = f"continuity:session:{event.session_id}"
        effect_digest = digest(
            {
                "effect": "database-write",
                "resource": resource,
                "event_digest": event.event_digest,
                "hook_set_digest": session.compiled_set.hook_set_digest,
            }
        )
        draft = cls(
            event,
            hook_payload,
            client_contract_digest,
            session.compiled_set.generation,
            session.compiled_set.hook_set_digest,
            hook_ids,
            idempotency_key,
            resource,
            source_digest,
            policy_digest,
            migration_digest,
            effect_digest,
            "",
            False,
        )
        return replace(draft, plan_digest=digest(draft.body()))

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-lifecycle-bridge-plan/v1",
            "event_digest": self.event.event_digest,
            "hook_payload_digest": digest(self.hook_payload),
            "client_contract_digest": self.client_contract_digest,
            "hook_generation": self.hook_generation,
            "hook_set_digest": self.hook_set_digest,
            "hook_ids": list(self.hook_ids),
            "idempotency_key": self.idempotency_key,
            "resource": self.resource,
            "source_digest": self.source_digest,
            "policy_digest": self.policy_digest,
            "migration_digest": self.migration_digest,
            "effect_digest": self.effect_digest,
            "grants_authority": False,
        }

    def assert_integrity(self) -> None:
        if self.plan_digest != digest(self.body()):
            raise PolicyViolation("Lifecycle bridge plan digest mismatch")


@dataclass(frozen=True, slots=True)
class LifecycleApplyResult:
    plan_digest: str
    event_digest: str
    event_type: HookEventType
    realm_id: UUID
    project_id: UUID
    work_item_id: UUID
    run_id: UUID
    session_id: str
    client_id: str
    event_id: UUID
    outbox_id: UUID
    delivery_created: bool
    hook_receipts: tuple[tuple[UUID, UUID], ...]
    hook_outcomes: tuple[HookRunOutcome, ...]
    status: str = "awaiting-finalization"
    terminal: bool = False
    grants_authority: bool = False

    @property
    def result_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-lifecycle-apply-result/v1",
                "plan_digest": self.plan_digest,
                "event_digest": self.event_digest,
                "event_type": self.event_type.value,
                "realm_id": str(self.realm_id),
                "project_id": str(self.project_id),
                "work_item_id": str(self.work_item_id),
                "run_id": str(self.run_id),
                "session_id": self.session_id,
                "client_id": self.client_id,
                "event_id": str(self.event_id),
                "outbox_id": str(self.outbox_id),
                "delivery_created": self.delivery_created,
                "hook_receipts": [list(map(str, item)) for item in self.hook_receipts],
                "hook_outcome_digests": [
                    digest(_hook_outcome_body(item)) for item in self.hook_outcomes
                ],
                "status": self.status,
                "terminal": False,
                "grants_authority": False,
            }
        )


@dataclass(frozen=True, slots=True)
class LifecycleFinalizeResult:
    outbox_id: UUID
    receipt_digest: str
    status: str
    completed_at: dt.datetime
    terminal: bool = True
    grants_authority: bool = False


@dataclass(frozen=True, slots=True)
class ClientLifecycleBridge:
    runtime: HookRuntime
    repository: LifecycleDeliveryRepository
    authorizations: AuthorizationStore
    hook_outcomes: HookOutcomeStore

    def check(
        self, request: LifecycleRequest, contract: LifecycleClientContract
    ) -> LifecycleAdmission:
        reason = "allowed"
        event_type: HookEventType | None = None
        if request.client_id != contract.descriptor.client_id:
            reason = "client-identity-mismatch"
        elif contract.unsupported_reason is not None:
            reason = f"client-unsupported:{contract.unsupported_reason}"
        elif request.max_recursion_depth < 0 or request.max_recursion_depth > 16:
            reason = "recursion-policy-invalid"
        elif request.origin == "zekam-internal" and (
            request.recursion_depth > request.max_recursion_depth
        ):
            reason = "recursion-depth-quarantine"
        elif request.origin not in {
            "zekam-internal",
            f"client:{request.client_id}",
        }:
            reason = "origin-client-binding-invalid"
        elif request.origin != "zekam-internal" and request.recursion_depth != 0:
            reason = "external-origin-recursion-invalid"
        elif request.classification in {
            DataClassification.SECRET,
            DataClassification.RAW_TRANSCRIPT,
        }:
            reason = "classification-not-content-safe"
        else:
            try:
                _assert_content_safe(request.payload)
                event_type = contract.resolve(request.external_event_type)
            except (PolicyViolation, ValidationFailed) as exc:
                reason = str(exc)
        allowed = event_type is not None and reason == "allowed"
        decision = {
            "schema": "zekam-lifecycle-admission/v1",
            "allowed": allowed,
            "event_type": None if event_type is None else event_type.value,
            "reason": reason,
            "client_contract_digest": contract.contract_digest,
            "payload_digest": digest(request.payload),
            "origin": request.origin,
            "causation_id": request.causation_id,
            "correlation_id": request.correlation_id,
            "recursion_depth": request.recursion_depth,
            "grants_authority": False,
        }
        return LifecycleAdmission(allowed, event_type, reason, digest(decision), False)

    def prepare(
        self,
        request: LifecycleRequest,
        contract: LifecycleClientContract,
        session: HookSession,
        *,
        source_digest: str,
        policy_digest: str,
        migration_digest: str,
    ) -> LifecycleBridgePlan:
        """Read-only deterministic validation and exact hook preview."""

        admission = self.check(request, contract)
        if not admission.allowed or admission.event_type is None:
            raise PolicyViolation(f"Lifecycle admission rejected: {admission.reason}")
        event = SessionLifecycleEvent(
            request.realm_id,
            request.project_id,
            request.work_item_id,
            request.run_id,
            request.session_id,
            request.client_id,
            request.event_id,
            admission.event_type.value,
            request.sequence,
            request.previous_digest,
            request.origin,
            request.causation_id,
            request.correlation_id,
            request.recursion_depth,
            request.source_revision,
            request.work_plan_ref,
            request.checkpoint_ref,
            request.context_ref,
            digest(request.payload),
            request.metadata,
            request.classification,
            request.occurred_at,
            request.ingested_at,
        )
        hook_payload = {"lifecycle": event.body(), "data": request.payload}
        _assert_content_safe(hook_payload)
        previews = self.runtime.preview(session, admission.event_type, hook_payload)
        effective = tuple(item for item in previews if item.will_execute)
        if len(effective) != 1:
            raise PolicyViolation(
                f"Required lifecycle event effective handler count must be 1; got {len(effective)}"
            )
        return LifecycleBridgePlan.create(
            event=event,
            hook_payload=hook_payload,
            client_contract_digest=contract.contract_digest,
            session=session,
            hook_ids=tuple(item.hook_id for item in effective),
            idempotency_key=_idempotency_key(request.idempotency_key),
            source_digest=source_digest,
            policy_digest=policy_digest,
            migration_digest=migration_digest,
        )

    def apply(
        self,
        plan: LifecycleBridgePlan,
        session: HookSession,
        *,
        session_binding_id: UUID,
        authorization_id: UUID,
        current_source_digest: str,
        current_policy_digest: str,
        current_migration_digest: str,
        now: dt.datetime | None = None,
    ) -> LifecycleApplyResult:
        """Consume exact authority, stage event/outbox, then persist hook receipts."""

        moment = now or dt.datetime.now(dt.UTC)
        plan.assert_integrity()
        if (
            session.compiled_set.generation != plan.hook_generation
            or session.compiled_set.hook_set_digest != plan.hook_set_digest
            or current_source_digest != plan.source_digest
            or current_policy_digest != plan.policy_digest
            or current_migration_digest != plan.migration_digest
        ):
            raise PolicyViolation("Lifecycle apply binding drift; replan required")
        event_type = HookEventType(plan.event.event_type)
        previews = self.runtime.preview(session, event_type, plan.hook_payload)
        if tuple(item.hook_id for item in previews if item.will_execute) != plan.hook_ids:
            raise PolicyViolation("Lifecycle effective handler drift; replan required")

        with self.repository.connection.transaction():
            authorization = self.authorizations.get(authorization_id)
            reason = authorization.rejection_reason(moment)
            if (
                reason is not None
                or authorization.realm_id != plan.event.realm_id
                or authorization.plan_digest != plan.plan_digest
                or authorization.effect_digest != plan.effect_digest
                or not authorization.scope.covers_effect("database-write")
                or not authorization.scope.covers_resource(plan.resource)
            ):
                raise AuthorizationRequired(
                    f"Lifecycle exact authorization binding yok: {reason or 'scope-mismatch'}"
                )
            consumed = self.authorizations.consume(
                authorization_id,
                effect_digest=plan.effect_digest,
                consumed_by="client-lifecycle-bridge/v1",
                now=moment,
            )
            if not bool(getattr(consumed, "consumed", False)):
                consume_reason = getattr(consumed, "reason", "unknown")
                raise AuthorizationRequired(
                    f"Lifecycle authorization tuketilemedi: {consume_reason}"
                )
            stage = self.repository.stage_lifecycle_delivery(
                plan.event,
                idempotency_key=plan.idempotency_key,
                plan_digest=plan.plan_digest,
            )

        records = self.runtime.run_with_records(session, event_type, plan.hook_payload)
        receipt_refs: list[tuple[UUID, UUID]] = []
        for record in records:
            if record.output_body is not None:
                _assert_content_safe(record.output_body)
            receipt_refs.append(
                self.hook_outcomes.record_outcome(
                    session_binding_id=session_binding_id,
                    entry=record.entry,
                    outcome=record.outcome,
                    input_body=plan.hook_payload,
                    output_body=record.output_body,
                )
            )
        return LifecycleApplyResult(
            plan_digest=plan.plan_digest,
            event_digest=plan.event.event_digest,
            event_type=event_type,
            realm_id=plan.event.realm_id,
            project_id=plan.event.project_id,
            work_item_id=plan.event.work_item_id,
            run_id=plan.event.run_id,
            session_id=plan.event.session_id,
            client_id=plan.event.client_id,
            event_id=stage.event_id,
            outbox_id=stage.outbox_id,
            delivery_created=stage.created,
            hook_receipts=tuple(receipt_refs),
            hook_outcomes=tuple(record.outcome for record in records),
        )

    def finalize(
        self,
        applied: LifecycleApplyResult,
        *,
        receipt_digest: str,
        status: str,
        completed_at: dt.datetime | None = None,
    ) -> LifecycleFinalizeResult:
        """Bind a durable terminal receipt; never infer success from process start."""

        parse_digest(receipt_digest)
        moment = completed_at or dt.datetime.now(dt.UTC)
        if moment.tzinfo is None:
            raise ValidationFailed("Lifecycle finalize zamani timezone-aware olmali")
        if status not in {"completed", "failed", "recovery-required"}:
            raise ValidationFailed("Lifecycle finalize status gecersiz")
        if status == "completed" and (
            not applied.hook_receipts
            or any(item.status != "completed" for item in applied.hook_outcomes)
        ):
            raise PolicyViolation("Lifecycle completed terminal hook receipt zinciri ister")
        if status == "completed" and applied.event_type in {
            HookEventType.PRE_COMPACTION,
            HookEventType.POST_COMPACTION,
            HookEventType.PRE_CLOSE,
            HookEventType.POST_CLOSE,
        }:
            snapshot = self.repository.read_session_snapshot(
                project_id=applied.project_id,
                work_item_id=applied.work_item_id,
                run_id=applied.run_id,
                session_id=applied.session_id,
                client_id=applied.client_id,
            )
            canonical_digest = (
                snapshot.compaction_receipt_digest
                if applied.event_type
                in {HookEventType.PRE_COMPACTION, HookEventType.POST_COMPACTION}
                else snapshot.close_receipt_digest
            )
            if canonical_digest != receipt_digest:
                raise PolicyViolation("Lifecycle close/compaction finalize canonical receipt ister")
        with self.repository.connection.transaction():
            self.repository.finalize_lifecycle_delivery(
                outbox_id=applied.outbox_id,
                receipt_digest=receipt_digest,
                status=status,
                completed_at=moment,
            )
        return LifecycleFinalizeResult(applied.outbox_id, receipt_digest, status, moment)


def _idempotency_key(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 160 or _FORBIDDEN_KEY.search(cleaned):
        raise ValidationFailed("Lifecycle idempotency key gecersiz")
    return cleaned


def _hook_outcome_body(outcome: HookRunOutcome) -> dict[str, Any]:
    return {
        "hook_id": outcome.hook_id,
        "hook_revision": outcome.hook_revision,
        "kind": None if outcome.kind is None else outcome.kind.value,
        "status": outcome.status,
        "input_digest": outcome.input_digest,
        "output_digest": outcome.output_digest,
        "proposal_digest": outcome.proposal_digest,
        "warning": outcome.warning,
        "requires_governed_effect": outcome.requires_governed_effect,
        "effect_performed": False,
        "grants_authority": False,
    }


def _assert_content_safe(value: Any) -> None:
    def visit(item: Any) -> None:
        if item is None or isinstance(item, (bool, int, float, dt.date, UUID, Enum)):
            return
        if isinstance(item, str):
            if len(item) > 2048 or _SENSITIVE_VALUE.search(item) or _ABSOLUTE_PATH.search(item):
                raise ValidationFailed("Lifecycle telemetry text bounded olmali")
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                declared_absent = (
                    key
                    in {
                        "contains_prompt",
                        "contains_response",
                        "contains_transcript",
                    }
                    and child is False
                )
                if not isinstance(key, str) or (_FORBIDDEN_KEY.search(key) and not declared_absent):
                    raise PolicyViolation("Lifecycle telemetry raw veya hassas alan tasiyamaz")
                visit(child)
            return
        raise ValidationFailed("Lifecycle telemetry JSON-compatible olmali")

    visit(value)
    encoded = canonical_bytes(value)
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise ValidationFailed("Lifecycle telemetry payload bounded olmali")
