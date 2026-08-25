"""Explicit command-driver composition for a real, isolated chaos environment.

Nothing is enabled by default.  An operator-provided JSON config selects an exact
driver argv and a non-source artifact root.  Commands are executed without a
shell; the driver must implement the typed JSON protocol below.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from zekam.application.chaos_campaign import (
    ChaosEvidenceVerifier,
    CompositeFaultInjector,
    FaultExecutionEvidence,
    GovernedRuntimeFaultProbe,
    RuntimeFaultProbe,
    compose_chaos_campaign_handler,
)
from zekam.application.transcript_corpus_import import ContentAddressedStore
from zekam.application.worker import ScheduledHandler
from zekam.domain.canonical import canonical_bytes, digest
from zekam.domain.chaos_campaign import (
    ChaosAuditEvent,
    ChaosCampaignPlan,
    ChaosCampaignPolicy,
    ChaosOperatorRecord,
    ChaosScenario,
    ChaosVerifierVerdict,
    FaultInjectionAuthorization,
    FaultPoint,
    RuntimeSafetySnapshot,
    default_chaos_scenarios,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

DRIVER_TIMEOUT_SECONDS = 120


def _moment(value: object) -> dt.datetime:
    if not isinstance(value, str):
        raise ValidationFailed("chaos driver zaman alani string olmali")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationFailed("chaos driver zaman alani gecersiz") from exc
    if parsed.tzinfo is None:
        raise ValidationFailed("chaos driver zamani timezone-aware olmali")
    return parsed


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationFailed(f"chaos driver {label} string listesi olmali")
    return tuple(value)


@dataclass(frozen=True, slots=True)
class ChaosCommandDriver:
    argv: tuple[str, ...]
    timeout_seconds: int = DRIVER_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.argv or any(not item.strip() for item in self.argv):
            raise ValidationFailed("chaos driver exact argv ister")
        if not 1 <= self.timeout_seconds <= 600:
            raise ValidationFailed("chaos driver timeout 1..600 olmali")

    def call(self, operation: str, body: Mapping[str, object]) -> Mapping[str, Any]:
        if operation not in {
            "authorize",
            "authorize-verify",
            "snapshot",
            "inject",
            "audit",
            "operator",
            "verify",
        }:
            raise PolicyViolation("chaos driver operation allowlist disinda")
        completed = subprocess.run(
            (*self.argv, operation),
            input=canonical_bytes(body),
            capture_output=True,
            check=False,
            shell=False,
            timeout=self.timeout_seconds,
        )
        if completed.returncode != 0:
            raise ValidationFailed(f"chaos driver {operation} basarisiz")
        try:
            document = json.loads(completed.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("chaos driver canonical JSON dondurmedi") from exc
        if not isinstance(document, Mapping):
            raise ValidationFailed("chaos driver JSON object dondurmeli")
        return document


@dataclass(frozen=True, slots=True)
class CommandAuthorizationProvider:
    driver: ChaosCommandDriver
    realm_id: str

    def issue(
        self, scenario: ChaosScenario, *, repetition: int, actor_identity: str
    ) -> FaultInjectionAuthorization:
        body = self.driver.call(
            "authorize",
            {
                "realm_id": self.realm_id,
                "scenario": scenario.as_dict(),
                "repetition": repetition,
                "actor_identity": actor_identity,
            },
        )
        return FaultInjectionAuthorization(
            realm_id=str(body.get("realm_id", "")),
            scenario_digest=str(body.get("scenario_digest", "")),
            target=str(body.get("target", "")),
            actor_identity=str(body.get("actor_identity", "")),
            valid_from=_moment(body.get("valid_from")),
            valid_until=_moment(body.get("valid_until")),
            repetition=int(body.get("repetition", 0)),
            authorization_record_digest=str(body.get("authorization_record_digest", "")),
        )

    def verify_current(self, authorization: FaultInjectionAuthorization) -> bool:
        body = self.driver.call("authorize-verify", {"authorization": authorization.as_dict()})
        return body == {
            "current": True,
            "authorization_digest": authorization.authorization_digest,
        }


@dataclass(slots=True)
class CommandFaultRuntimeAdapter:
    driver: ChaosCommandDriver
    fault_point: FaultPoint
    realm_id: str
    _scenario: ChaosScenario | None = None

    def capture_safety_snapshot(self) -> RuntimeSafetySnapshot:
        body = self.driver.call(
            "snapshot", {"realm_id": self.realm_id, "fault_point": str(self.fault_point)}
        )
        return RuntimeSafetySnapshot(
            realm_id=str(body.get("realm_id", "")),
            authority_bindings=_strings(body.get("authority_bindings"), "authority_bindings"),
            irreversible_effect_occurrences=_strings(
                body.get("irreversible_effect_occurrences"), "effect occurrences"
            ),
            non_terminal_state_refs=_strings(
                body.get("non_terminal_state_refs"), "non-terminal refs"
            ),
            accessed_realm_ids=_strings(body.get("accessed_realm_ids"), "accessed realms"),
            audit_head_digest=str(body.get("audit_head_digest", "")),
        )

    def inject_fault(
        self,
        scenario: ChaosScenario,
        *,
        repetition: int,
        authorization: FaultInjectionAuthorization,
    ) -> None:
        self._scenario = scenario
        body = self.driver.call(
            "inject",
            {
                "scenario": scenario.as_dict(),
                "repetition": repetition,
                "authorization": authorization.as_dict(),
            },
        )
        if body != {"status": "injected", "scenario_digest": scenario.scenario_digest}:
            raise ValidationFailed("chaos driver exact injection receipt ack dondurmedi")

    def audit_events_since(self, previous_digest: str) -> tuple[ChaosAuditEvent, ...]:
        body = self.driver.call(
            "audit",
            {
                "realm_id": self.realm_id,
                "fault_point": str(self.fault_point),
                "previous_digest": previous_digest,
            },
        )
        rows = body.get("events")
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise ValidationFailed("chaos driver audit events listesi ister")
        return tuple(
            ChaosAuditEvent(
                realm_id=str(row.get("realm_id", "")),
                sequence=int(row.get("sequence", 0)),
                event_type=str(row.get("event_type", "")),
                record_ref=str(row.get("record_ref", "")),
                previous_digest=str(row.get("previous_digest", "")),
            )
            for row in rows
        )

    def operator_record(self, scenario: ChaosScenario) -> ChaosOperatorRecord:
        body = self.driver.call(
            "operator", {"realm_id": self.realm_id, "scenario": scenario.as_dict()}
        )
        return ChaosOperatorRecord(
            realm_id=str(body.get("realm_id", "")),
            scenario_digest=str(body.get("scenario_digest", "")),
            state_ref=str(body.get("state_ref", "")),
            next_safe_action=str(body.get("next_safe_action", "")),
            audit_head_digest=str(body.get("audit_head_digest", "")),
        )


@dataclass(frozen=True, slots=True)
class CommandEvidenceVerifier:
    driver: ChaosCommandDriver
    verifier_identity: str

    def verify(self, evidence: FaultExecutionEvidence) -> ChaosVerifierVerdict:
        evidence_digest = digest(evidence.evidence_body())
        body = self.driver.call(
            "verify",
            {"evidence": evidence.evidence_body(), "evidence_bundle_digest": evidence_digest},
        )
        accepted = body.get("accepted") is True
        verdict_body = {
            "verifier_identity": self.verifier_identity,
            "evidence_bundle_digest": evidence_digest,
            "accepted": accepted,
        }
        if body.get("verdict_receipt_digest") != digest(verdict_body):
            raise ValidationFailed("chaos driver verifier receipt uyusmuyor")
        return ChaosVerifierVerdict(
            self.verifier_identity, evidence_digest, accepted, digest(verdict_body)
        )


def canonical_zekam_source_root() -> Path:
    """Resolve from installed module location, never from the caller's cwd."""

    module = Path(__file__).resolve()
    for candidate in module.parents:
        if (candidate / "PROJE_MANIFESTI.yaml").is_file():
            return candidate
    return module.parents[1]


def compose_command_chaos_handler(
    config_file: Path, *, source_root: Path | None = None
) -> ScheduledHandler:
    """Load an explicit production driver; never writes into the source repository."""

    try:
        raw = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed("chaos driver config okunamadi") from exc
    if not isinstance(raw, Mapping):
        raise ValidationFailed("chaos driver config JSON object olmali")
    argv = _strings(raw.get("driver_argv"), "driver_argv")
    verifier_argv = _strings(raw.get("verifier_argv"), "verifier_argv")
    if verifier_argv == argv:
        raise PolicyViolation("chaos injector ve verifier exact argv bagimsiz olmali")
    artifact_root = Path(str(raw.get("artifact_root", ""))).resolve()
    resolved_source = (source_root or canonical_zekam_source_root()).resolve()
    if (
        not artifact_root.is_absolute()
        or artifact_root == resolved_source
        or resolved_source in artifact_root.parents
    ):
        raise PolicyViolation("chaos artifact root source repository icinde olamaz")
    realm_id = str(raw.get("realm_id", ""))
    source_revision = str(raw.get("source_revision", ""))
    verifier_identity = str(raw.get("verifier_identity", ""))
    driver = ChaosCommandDriver(argv, int(raw.get("timeout_seconds", DRIVER_TIMEOUT_SECONDS)))
    verifier_driver = ChaosCommandDriver(
        verifier_argv, int(raw.get("timeout_seconds", DRIVER_TIMEOUT_SECONDS))
    )
    authorizations = CommandAuthorizationProvider(driver, realm_id)
    probes = {
        fault: GovernedRuntimeFaultProbe(
            fault,
            f"command-probe/{fault}",
            CommandFaultRuntimeAdapter(driver, fault, realm_id),
            authorizations,
        )
        for fault in FaultPoint
    }
    scenarios = default_chaos_scenarios()
    plan = ChaosCampaignPlan(
        campaign_id=str(raw.get("campaign_id", "")),
        source_revision=source_revision,
        suite_digest=digest([item.as_dict() for item in scenarios]),
        policy=ChaosCampaignPolicy(),
        scenarios=scenarios,
    )
    injector = CompositeFaultInjector(
        cast(Mapping[FaultPoint, RuntimeFaultProbe], probes),
        cast(ChaosEvidenceVerifier, CommandEvidenceVerifier(verifier_driver, verifier_identity)),
    )
    store = LocalContentAddressedStore(artifact_root).ensure()
    return compose_chaos_campaign_handler(plan, injector, cast(ContentAddressedStore, store))
