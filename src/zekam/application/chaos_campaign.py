"""Execution and persistence boundaries for the scheduled chaos campaign."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from zekam.application.transcript_corpus_import import ContentAddressedStore
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.chaos_campaign import (
    ChaosAuditEvent,
    ChaosCampaignPlan,
    ChaosCampaignResult,
    ChaosObservation,
    ChaosOperatorRecord,
    ChaosScenario,
    ChaosVerifierVerdict,
    FaultInjectionAuthorization,
    FaultInjectionReceipt,
    FaultPoint,
    RuntimeSafetySnapshot,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed


class FaultInjector(Protocol):
    def inject(self, scenario: ChaosScenario, *, repetition: int) -> ChaosObservation: ...


@dataclass(frozen=True, slots=True)
class FaultExecutionEvidence:
    authorization: FaultInjectionAuthorization
    receipt: FaultInjectionReceipt
    before: RuntimeSafetySnapshot
    after: RuntimeSafetySnapshot
    audit_events: tuple[ChaosAuditEvent, ...]
    operator_record: ChaosOperatorRecord

    def evidence_body(self) -> dict[str, object]:
        return {
            "authorization": self.authorization.as_dict(),
            "receipt": self.receipt.as_dict(),
            "before": self.before.as_dict(),
            "after": self.after.as_dict(),
            "audit_events": [item.as_dict() for item in self.audit_events],
            "operator_record": self.operator_record.as_dict(),
        }


class RuntimeFaultProbe(Protocol):
    """A concrete adapter that injects one exact fault into a governed test realm."""

    injector_identity: str

    def execute(self, scenario: ChaosScenario, *, repetition: int) -> FaultExecutionEvidence: ...


class ChaosEvidenceVerifier(Protocol):
    @property
    def verifier_identity(self) -> str: ...

    def verify(self, evidence: FaultExecutionEvidence) -> ChaosVerifierVerdict: ...


class FaultAuthorizationProvider(Protocol):
    def issue(
        self, scenario: ChaosScenario, *, repetition: int, actor_identity: str
    ) -> FaultInjectionAuthorization: ...

    def verify_current(self, authorization: FaultInjectionAuthorization) -> bool: ...


class FaultRuntimeAdapter(Protocol):
    """Runtime-specific, effectful adapter used only in a governed chaos realm."""

    fault_point: FaultPoint

    def capture_safety_snapshot(self) -> RuntimeSafetySnapshot: ...

    def inject_fault(
        self,
        scenario: ChaosScenario,
        *,
        repetition: int,
        authorization: FaultInjectionAuthorization,
    ) -> None: ...

    def audit_events_since(self, previous_digest: str) -> tuple[ChaosAuditEvent, ...]: ...

    def operator_record(self, scenario: ChaosScenario) -> ChaosOperatorRecord: ...


@dataclass(frozen=True, slots=True)
class GovernedRuntimeFaultProbe:
    """Concrete probe orchestration around a real runtime adapter."""

    fault_point: FaultPoint
    injector_identity: str
    runtime: FaultRuntimeAdapter
    authorizations: FaultAuthorizationProvider

    def __post_init__(self) -> None:
        if not self.injector_identity.strip() or self.runtime.fault_point is not self.fault_point:
            raise ValidationFailed("governed fault probe identity/adapter eslesmiyor")

    def execute(self, scenario: ChaosScenario, *, repetition: int) -> FaultExecutionEvidence:
        if scenario.fault_point is not self.fault_point:
            raise ValidationFailed("fault probe baska scenarioyu calistiramaz")
        authorization = self.authorizations.issue(
            scenario, repetition=repetition, actor_identity=self.injector_identity
        )
        authorization.__post_init__()
        started_at = dt.datetime.now(dt.UTC)
        before = self.runtime.capture_safety_snapshot()
        if (
            authorization.realm_id != before.realm_id
            or authorization.scenario_digest != scenario.scenario_digest
            or authorization.target != scenario.target
            or authorization.actor_identity != self.injector_identity
            or authorization.repetition != repetition
            or not authorization.valid_from <= started_at <= authorization.valid_until
            or not self.authorizations.verify_current(authorization)
        ):
            raise PolicyViolation("fault authorization effect oncesi exact scope/current degil")
        self.runtime.inject_fault(scenario, repetition=repetition, authorization=authorization)
        after = self.runtime.capture_safety_snapshot()
        completed_at = dt.datetime.now(dt.UTC)
        receipt = FaultInjectionReceipt(
            scenario_digest=scenario.scenario_digest,
            repetition=repetition,
            injector_identity=self.injector_identity,
            authorization_digest=authorization.authorization_digest,
            before_snapshot_digest=before.snapshot_digest,
            after_snapshot_digest=after.snapshot_digest,
            started_at=started_at,
            completed_at=completed_at,
        )
        return FaultExecutionEvidence(
            authorization=authorization,
            receipt=receipt,
            before=before,
            after=after,
            audit_events=self.runtime.audit_events_since(before.audit_head_digest),
            operator_record=self.runtime.operator_record(scenario),
        )


@dataclass(frozen=True, slots=True)
class CompositeFaultInjector:
    """Production harness: every fault requires an explicit executable probe adapter."""

    probes: Mapping[FaultPoint, RuntimeFaultProbe]
    verifier: ChaosEvidenceVerifier

    def __post_init__(self) -> None:
        if tuple(self.probes) != tuple(FaultPoint):
            raise ValidationFailed("production chaos harness exact probe matrixi ister")

    def inject(self, scenario: ChaosScenario, *, repetition: int) -> ChaosObservation:
        probe = self.probes[scenario.fault_point]
        evidence = probe.execute(scenario, repetition=repetition)
        if evidence.receipt.injector_identity != probe.injector_identity:
            raise ValidationFailed("fault probe execution identity receipt ile uyusmuyor")
        verdict = self.verifier.verify(evidence)
        observation = ChaosObservation(
            authorization=evidence.authorization,
            receipt=evidence.receipt,
            before=evidence.before,
            after=evidence.after,
            audit_events=evidence.audit_events,
            operator_record=evidence.operator_record,
            verifier_verdict=verdict,
        )
        observation.validate()
        return observation


def run_chaos_campaign(
    plan: ChaosCampaignPlan,
    injector: FaultInjector,
    *,
    completed_at: dt.datetime,
) -> ChaosCampaignResult:
    plan.__post_init__()
    observations: list[ChaosObservation] = []
    for scenario in plan.scenarios:
        for repetition in range(1, plan.policy.repetitions + 1):
            observation = injector.inject(scenario, repetition=repetition)
            observation.__post_init__()
            if observation.scenario_digest != scenario.scenario_digest:
                raise ValidationFailed("fault injector baska scenario icin kanit dondurdu")
            observations.append(observation)
    return ChaosCampaignResult(plan, tuple(observations), completed_at)


@dataclass(frozen=True, slots=True)
class StoredChaosCampaign:
    result_digest: str
    object_digest: str
    status: str
    grants_authority: bool = False


def persist_chaos_campaign(
    result: ChaosCampaignResult, store: ContentAddressedStore
) -> StoredChaosCampaign:
    result.validate()
    payload = result.to_bytes()
    info = store.put(
        payload,
        media_type="application/vnd.zekam.chaos-campaign-result+json",
        metadata={"result_digest": result.result_digest, "status": str(result.status)},
    )
    if (
        info.digest != digest_of_bytes(payload)
        or not store.exists(info.digest)
        or store.get(info.digest) != payload
    ):
        raise ValidationFailed("chaos campaign CAS dogrulamasi basarisiz")
    return StoredChaosCampaign(result.result_digest, info.digest, str(result.status))


def compose_chaos_campaign_handler(
    plan: ChaosCampaignPlan,
    injector: FaultInjector,
    store: ContentAddressedStore,
) -> Callable[[dt.datetime], str]:
    """Build an explicit scheduler handler; no production no-op fallback exists."""

    def handler(now: dt.datetime) -> str:
        result = run_chaos_campaign(plan, injector, completed_at=now)
        stored = persist_chaos_campaign(result, store)
        if str(result.status) != "passed":
            raise PolicyViolation(
                f"chaos campaign safety gate failed; evidence={stored.object_digest}"
            )
        return f"chaos campaign passed; evidence={stored.object_digest}"

    return handler
