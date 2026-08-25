"""Deterministic chaos/fault campaign contracts and safety verdicts."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import canonical_bytes, digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class FaultPoint(StrEnum):
    WORKER_PROCESS_KILL = "worker-process-kill"
    POSTGRES_CONNECTION_DROP = "postgres-connection-drop"
    TRANSACTION_CONFLICT = "transaction-serialization-or-deadlock"
    OBJECT_STORE_FAILURE = "object-store-write-or-read-failure"
    PROVIDER_FAILURE = "provider-timeout-429-500-or-malformed-json"
    PARTIAL_DISK_WRITE = "partial-local-disk-write"
    OUTBOX_DISK_FULL = "outbox-disk-full"
    CLOCK_SKEW = "clock-skew"
    LATE_RESPONSE_AFTER_LEASE = "late-response-after-expired-lease"
    ROUTE_INVENTORY_RACE = "route-inventory-update-race"
    CONTEXT_SUPERSESSION = "context-source-supersession"
    SANDBOX_DRIFT = "sandbox-deletion-or-dirty-state-drift"
    DUPLICATE_LIFECYCLE = "duplicate-lifecycle-producer"
    VERIFIER_UNAVAILABLE = "verifier-unavailable"
    CLIENT_CRASH_DURING_COMPACTION = "client-crash-during-compaction"


class ChaosCampaignStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ChaosCampaignPolicy:
    schedule_interval: str = "7d"
    repetitions: int = 1
    policy_version: str = "1"

    def __post_init__(self) -> None:
        if self.schedule_interval != "7d":
            raise PolicyViolation("kanonik chaos campaign haftalik kosmali")
        if not 1 <= self.repetitions <= 5:
            raise ValidationFailed("chaos repetition 1..5 araliginda olmali")
        if not self.policy_version.strip():
            raise ValidationFailed("chaos policy version ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-chaos-campaign-policy/v1",
            "schedule_interval": self.schedule_interval,
            "repetitions": self.repetitions,
            "policy_version": self.policy_version,
        }

    @property
    def policy_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class ChaosScenario:
    fault_point: FaultPoint
    target: str
    injection_phase: str
    expected_next_action: str

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (self.target, self.injection_phase, self.expected_next_action)
        ):
            raise ValidationFailed("chaos scenario target, phase ve next action ister")

    def as_dict(self) -> dict[str, str]:
        return {
            "fault_point": str(self.fault_point),
            "target": self.target,
            "injection_phase": self.injection_phase,
            "expected_next_action": self.expected_next_action,
        }

    @property
    def scenario_digest(self) -> str:
        return digest(self.as_dict())


_DEFAULT_SCENARIOS: tuple[tuple[FaultPoint, str, str, str], ...] = (
    (FaultPoint.WORKER_PROCESS_KILL, "worker", "after-claim", "expire lease and reclaim"),
    (
        FaultPoint.POSTGRES_CONNECTION_DROP,
        "postgres",
        "transaction-open",
        "reconnect and reconcile",
    ),
    (FaultPoint.TRANSACTION_CONFLICT, "postgres", "commit", "bounded transaction retry"),
    (
        FaultPoint.OBJECT_STORE_FAILURE,
        "object-store",
        "write-or-read",
        "verify digest and retry safely",
    ),
    (FaultPoint.PROVIDER_FAILURE, "model-gateway", "provider-call", "reconcile provider request"),
    (FaultPoint.PARTIAL_DISK_WRITE, "local-cas", "atomic-write", "discard partial and retry"),
    (FaultPoint.OUTBOX_DISK_FULL, "lifecycle-outbox", "append", "retain pending delivery"),
    (FaultPoint.CLOCK_SKEW, "lease-clock", "lease-validation", "use canonical database time"),
    (
        FaultPoint.LATE_RESPONSE_AFTER_LEASE,
        "effect-ledger",
        "response-arrival",
        "quarantine late response",
    ),
    (
        FaultPoint.ROUTE_INVENTORY_RACE,
        "model-routing",
        "route-bind",
        "revalidate inventory snapshot",
    ),
    (
        FaultPoint.CONTEXT_SUPERSESSION,
        "context-compiler",
        "request-materialize",
        "recompile current context",
    ),
    (FaultPoint.SANDBOX_DRIFT, "sandbox", "before-effect", "recreate verified sandbox"),
    (FaultPoint.DUPLICATE_LIFECYCLE, "client-lifecycle", "event-delivery", "dedupe occurrence key"),
    (FaultPoint.VERIFIER_UNAVAILABLE, "verifier", "verification", "keep result pending"),
    (
        FaultPoint.CLIENT_CRASH_DURING_COMPACTION,
        "client-session",
        "compaction",
        "resume from checkpoint",
    ),
)


def default_chaos_scenarios() -> tuple[ChaosScenario, ...]:
    return tuple(ChaosScenario(*values) for values in _DEFAULT_SCENARIOS)


@dataclass(frozen=True, slots=True)
class ChaosCampaignPlan:
    campaign_id: str
    source_revision: str
    suite_digest: str
    policy: ChaosCampaignPolicy
    scenarios: tuple[ChaosScenario, ...]

    def __post_init__(self) -> None:
        if not self.campaign_id.strip() or not self.source_revision.strip():
            raise ValidationFailed("chaos campaign identity ve source revision ister")
        parse_digest(self.suite_digest)
        self.policy.__post_init__()
        for scenario in self.scenarios:
            scenario.__post_init__()
        scenario_faults = tuple(item.fault_point for item in self.scenarios)
        if scenario_faults != tuple(FaultPoint):
            raise ValidationFailed("chaos matrix tum fault noktalarini canonical sirada ister")
        if len({item.scenario_digest for item in self.scenarios}) != len(self.scenarios):
            raise ValidationFailed("chaos scenario digestleri tekil olmali")
        if self.scenarios != default_chaos_scenarios():
            raise ValidationFailed("chaos matrix versioned canonical scenario semantiginden sapti")
        if self.suite_digest != digest([item.as_dict() for item in self.scenarios]):
            raise ValidationFailed("chaos suite digest scenario matrixini baglamali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-chaos-campaign-plan/v1",
            "campaign_id": self.campaign_id,
            "source_revision": self.source_revision,
            "suite_digest": self.suite_digest,
            "policy": self.policy.as_dict(),
            "scenarios": [item.as_dict() for item in self.scenarios],
        }

    @property
    def plan_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class RuntimeSafetySnapshot:
    realm_id: str
    authority_bindings: tuple[str, ...]
    irreversible_effect_occurrences: tuple[str, ...]
    non_terminal_state_refs: tuple[str, ...]
    accessed_realm_ids: tuple[str, ...]
    audit_head_digest: str

    def __post_init__(self) -> None:
        if not self.realm_id.strip():
            raise ValidationFailed("runtime snapshot realm ister")
        parse_digest(self.audit_head_digest)
        for values, label, allow_duplicates in (
            (self.authority_bindings, "authority", False),
            (self.irreversible_effect_occurrences, "effect occurrence", True),
            (self.non_terminal_state_refs, "non-terminal state", False),
            (self.accessed_realm_ids, "accessed realm", False),
        ):
            if any(not item.strip() for item in values) or values != tuple(sorted(values)):
                raise ValidationFailed(f"runtime snapshot {label} degerleri sirali ve dolu olmali")
            if not allow_duplicates and len(values) != len(set(values)):
                raise ValidationFailed(f"runtime snapshot {label} degerleri tekil olmali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "realm_id": self.realm_id,
            "authority_bindings": list(self.authority_bindings),
            "irreversible_effect_occurrences": list(self.irreversible_effect_occurrences),
            "non_terminal_state_refs": list(self.non_terminal_state_refs),
            "accessed_realm_ids": list(self.accessed_realm_ids),
            "audit_head_digest": self.audit_head_digest,
        }

    @property
    def snapshot_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class ChaosAuditEvent:
    realm_id: str
    sequence: int
    event_type: str
    record_ref: str
    previous_digest: str

    def __post_init__(self) -> None:
        if self.sequence < 1 or not all(
            value.strip() for value in (self.realm_id, self.event_type, self.record_ref)
        ):
            raise ValidationFailed("chaos audit event identity ve sequence ister")
        parse_digest(self.previous_digest)

    def as_dict(self) -> dict[str, Any]:
        return {
            "realm_id": self.realm_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "record_ref": self.record_ref,
            "previous_digest": self.previous_digest,
        }

    @property
    def event_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class FaultInjectionAuthorization:
    realm_id: str
    scenario_digest: str
    target: str
    actor_identity: str
    valid_from: dt.datetime
    valid_until: dt.datetime
    repetition: int
    authorization_record_digest: str

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.realm_id, self.target, self.actor_identity)):
            raise ValidationFailed("fault authorization realm, target ve actor ister")
        parse_digest(self.scenario_digest)
        parse_digest(self.authorization_record_digest)
        if self.valid_from.tzinfo is None or self.valid_until.tzinfo is None:
            raise ValidationFailed("fault authorization timezone-aware zaman ister")
        if self.valid_until <= self.valid_from or self.repetition < 1:
            raise ValidationFailed("fault authorization sure/repetition kapsami gecersiz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "realm_id": self.realm_id,
            "scenario_digest": self.scenario_digest,
            "target": self.target,
            "actor_identity": self.actor_identity,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "repetition": self.repetition,
            "authorization_record_digest": self.authorization_record_digest,
        }

    @property
    def authorization_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class FaultInjectionReceipt:
    scenario_digest: str
    repetition: int
    injector_identity: str
    authorization_digest: str
    before_snapshot_digest: str
    after_snapshot_digest: str
    started_at: dt.datetime
    completed_at: dt.datetime

    def __post_init__(self) -> None:
        for value in (
            self.scenario_digest,
            self.authorization_digest,
            self.before_snapshot_digest,
            self.after_snapshot_digest,
        ):
            parse_digest(value)
        if self.repetition < 1 or not self.injector_identity.strip():
            raise ValidationFailed("fault receipt repetition ve injector identity ister")
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValidationFailed("fault receipt timezone-aware zaman ister")
        if self.completed_at < self.started_at:
            raise ValidationFailed("fault receipt completion baslangictan once olamaz")

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario_digest": self.scenario_digest,
            "repetition": self.repetition,
            "injector_identity": self.injector_identity,
            "authorization_digest": self.authorization_digest,
            "before_snapshot_digest": self.before_snapshot_digest,
            "after_snapshot_digest": self.after_snapshot_digest,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
        }

    @property
    def receipt_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class ChaosOperatorRecord:
    realm_id: str
    scenario_digest: str
    state_ref: str
    next_safe_action: str
    audit_head_digest: str

    def __post_init__(self) -> None:
        if not all(
            value.strip() for value in (self.realm_id, self.state_ref, self.next_safe_action)
        ):
            raise ValidationFailed("chaos operator record realm/state/action ister")
        parse_digest(self.scenario_digest)
        parse_digest(self.audit_head_digest)

    def as_dict(self) -> dict[str, str]:
        return {
            "realm_id": self.realm_id,
            "scenario_digest": self.scenario_digest,
            "state_ref": self.state_ref,
            "next_safe_action": self.next_safe_action,
            "audit_head_digest": self.audit_head_digest,
        }

    @property
    def record_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class ChaosVerifierVerdict:
    verifier_identity: str
    evidence_bundle_digest: str
    accepted: bool
    verdict_receipt_digest: str

    def __post_init__(self) -> None:
        if not self.verifier_identity.strip():
            raise ValidationFailed("chaos verifier identity ister")
        parse_digest(self.evidence_bundle_digest)
        parse_digest(self.verdict_receipt_digest)
        expected = digest(
            {
                "verifier_identity": self.verifier_identity,
                "evidence_bundle_digest": self.evidence_bundle_digest,
                "accepted": self.accepted,
            }
        )
        if self.verdict_receipt_digest != expected:
            raise ValidationFailed("chaos verifier receipt verdict semantigini baglamiyor")

    def as_dict(self) -> dict[str, Any]:
        return {
            "verifier_identity": self.verifier_identity,
            "evidence_bundle_digest": self.evidence_bundle_digest,
            "accepted": self.accepted,
            "verdict_receipt_digest": self.verdict_receipt_digest,
        }


@dataclass(frozen=True, slots=True)
class ChaosObservation:
    authorization: FaultInjectionAuthorization
    receipt: FaultInjectionReceipt
    before: RuntimeSafetySnapshot
    after: RuntimeSafetySnapshot
    audit_events: tuple[ChaosAuditEvent, ...]
    operator_record: ChaosOperatorRecord
    verifier_verdict: ChaosVerifierVerdict

    def __post_init__(self) -> None:
        self.validate()

    @property
    def scenario_digest(self) -> str:
        return self.receipt.scenario_digest

    @property
    def repetition(self) -> int:
        return self.receipt.repetition

    def evidence_body(self) -> dict[str, Any]:
        return {
            "authorization": self.authorization.as_dict(),
            "receipt": self.receipt.as_dict(),
            "before": self.before.as_dict(),
            "after": self.after.as_dict(),
            "audit_events": [item.as_dict() for item in self.audit_events],
            "operator_record": self.operator_record.as_dict(),
        }

    @property
    def evidence_bundle_digest(self) -> str:
        return digest(self.evidence_body())

    def validate(self) -> None:
        self.authorization.__post_init__()
        self.receipt.__post_init__()
        self.before.__post_init__()
        self.after.__post_init__()
        self.operator_record.__post_init__()
        self.verifier_verdict.__post_init__()
        if (
            self.receipt.authorization_digest != self.authorization.authorization_digest
            or self.authorization.scenario_digest != self.scenario_digest
            or self.authorization.realm_id != self.before.realm_id
            or self.receipt.injector_identity != self.authorization.actor_identity
            or self.receipt.repetition != self.authorization.repetition
            or not (
                self.authorization.valid_from
                <= self.receipt.started_at
                <= self.receipt.completed_at
                <= self.authorization.valid_until
            )
        ):
            raise PolicyViolation("fault receipt exact authorization kapsami disinda")
        if self.receipt.before_snapshot_digest != self.before.snapshot_digest:
            raise ValidationFailed("fault receipt before snapshot digest uyusmuyor")
        if self.receipt.after_snapshot_digest != self.after.snapshot_digest:
            raise ValidationFailed("fault receipt after snapshot digest uyusmuyor")
        if self.before.realm_id != self.after.realm_id:
            raise ValidationFailed("fault snapshot realm degistiremez")
        previous = self.before.audit_head_digest
        for sequence, event in enumerate(self.audit_events, start=1):
            event.__post_init__()
            if (
                event.realm_id != self.before.realm_id
                or event.sequence != sequence
                or event.previous_digest != previous
            ):
                raise ValidationFailed("chaos audit zinciri kopuk veya realm disi")
            previous = event.event_digest
        if not self.audit_events or self.after.audit_head_digest != previous:
            raise ValidationFailed("chaos audit head injected event zincirine bagli degil")
        if (
            self.operator_record.realm_id != self.after.realm_id
            or self.operator_record.scenario_digest != self.scenario_digest
            or self.operator_record.audit_head_digest != self.after.audit_head_digest
            or self.operator_record.state_ref not in self.after.non_terminal_state_refs
        ):
            raise ValidationFailed(
                "operator record canonical post-state/audit zincirine bagli degil"
            )
        if self.verifier_verdict.verifier_identity == self.receipt.injector_identity:
            raise PolicyViolation("fault injector kendi kanitini verify edemez")
        if self.verifier_verdict.evidence_bundle_digest != self.evidence_bundle_digest:
            raise ValidationFailed("chaos verifier verdict evidence bundle ile uyusmuyor")

    @property
    def passed(self) -> bool:
        return (
            self.verifier_verdict.accepted
            and self.before.authority_bindings == self.after.authority_bindings
            and self.before.irreversible_effect_occurrences
            == self.after.irreversible_effect_occurrences
            and bool(self.after.non_terminal_state_refs)
            and self.after.accessed_realm_ids == (self.after.realm_id,)
        )

    @property
    def safe_next_action(self) -> str:
        return self.operator_record.next_safe_action

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.evidence_body(),
            "verifier_verdict": self.verifier_verdict.as_dict(),
            "evidence_bundle_digest": self.evidence_bundle_digest,
            "passed": self.passed,
        }

    @property
    def observation_digest(self) -> str:
        return digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class ChaosCampaignResult:
    plan: ChaosCampaignPlan
    observations: tuple[ChaosObservation, ...]
    completed_at: dt.datetime

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        self.plan.__post_init__()
        if self.completed_at.tzinfo is None:
            raise ValidationFailed("chaos result timezone-aware completion ister")
        expected = tuple(
            (scenario.scenario_digest, repetition)
            for scenario in self.plan.scenarios
            for repetition in range(1, self.plan.policy.repetitions + 1)
        )
        actual = tuple((item.scenario_digest, item.repetition) for item in self.observations)
        if actual != expected:
            raise ValidationFailed("chaos result eksiksiz ve canonical matrix ister")
        receipt_digests = tuple(item.receipt.receipt_digest for item in self.observations)
        verdict_digests = tuple(
            item.verifier_verdict.verdict_receipt_digest for item in self.observations
        )
        authorization_records = tuple(
            item.authorization.authorization_record_digest for item in self.observations
        )
        audit_heads = tuple(item.after.audit_head_digest for item in self.observations)
        operator_records = tuple(item.operator_record.record_digest for item in self.observations)
        if len(set(receipt_digests)) != len(receipt_digests):
            raise ValidationFailed(
                "chaos fault receiptleri scenario/repetition bazinda tekil olmali"
            )
        if len(set(verdict_digests)) != len(verdict_digests):
            raise ValidationFailed("chaos verifier receiptleri observation bazinda tekil olmali")
        if len(set(authorization_records)) != len(authorization_records):
            raise ValidationFailed("chaos authorization kayitlari scenario bazinda tekil olmali")
        if len(set(audit_heads)) != len(audit_heads):
            raise ValidationFailed("chaos audit headleri scenario bazinda tekil olmali")
        if len(set(operator_records)) != len(operator_records):
            raise ValidationFailed("chaos operator kayitlari scenario bazinda tekil olmali")
        for scenario, observation in zip(
            (
                scenario
                for scenario in self.plan.scenarios
                for _ in range(self.plan.policy.repetitions)
            ),
            self.observations,
            strict=True,
        ):
            observation.__post_init__()
            if (
                observation.safe_next_action != scenario.expected_next_action
                or observation.authorization.target != scenario.target
            ):
                raise ValidationFailed("chaos authorization/action scenario ile uyusmuyor")

    @property
    def status(self) -> ChaosCampaignStatus:
        return (
            ChaosCampaignStatus.PASSED
            if all(item.passed for item in self.observations)
            else ChaosCampaignStatus.FAILED
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-chaos-campaign-result/v1",
            "plan": self.plan.as_dict(),
            "plan_digest": self.plan.plan_digest,
            "completed_at": self.completed_at.isoformat(),
            "status": str(self.status),
            "observations": [item.as_dict() for item in self.observations],
        }

    @property
    def result_digest(self) -> str:
        return digest(self.as_dict())

    def to_bytes(self) -> bytes:
        return canonical_bytes(self.as_dict())
