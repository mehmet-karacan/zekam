"""Health probe calistirma, sozlesme dogrulama ve karantina yonetimi.

Probe calistirmak bir **provider call**'dur: exact authorization ve Secret Broker
gerektirir. Bu servis cagriyi kendisi yapmaz; enjekte edilen bir `ProviderProbe`
uzerinden yapar. Boylece:

- test ve gelistirme sentetik bir probe ile calisir,
- gercek cagri her zaman governance kapilarindan gecer,
- prompt ve yanit icerigi hicbir zaman kaydedilmez.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.model_health import (
    CONTRACTS_BY_MODALITY,
    CapabilityCheck,
    ContractCapability,
    HealthDecision,
    ProbeFailure,
    ProbeFixture,
    ProbeOutcome,
    ProbeStatus,
    QuarantinePolicy,
    StalenessVerdict,
    assess_staleness,
    benchmark_state_for,
    evaluate_health,
    fixture_for,
    validate_shape,
    verified_capabilities,
)
from zekam.domain.model_inventory import BenchmarkState, HealthState, Modality, ModelRecord
from zekam.domain.realm import Realm


class ModelInventoryStore(Protocol):
    def get(self, model_id: str) -> ModelRecord: ...

    def list_all(self) -> tuple[ModelRecord, ...]: ...

    def set_health(
        self,
        model_id: str,
        *,
        state: HealthState,
        quarantine_until: dt.datetime | None,
        benchmark_state: BenchmarkState,
        policy_digest: str,
        inventory_digest: str,
        verified_capabilities: Sequence[str] | None = None,
        now: dt.datetime | None = None,
    ) -> None: ...

    def health_metadata(
        self, model_id: str
    ) -> tuple[dt.datetime | None, str | None, str | None]: ...


class HealthProbeStore(Protocol):
    def record(
        self, outcome: ProbeOutcome, *, policy_digest: str, inventory_digest: str
    ) -> UUID: ...

    def history(self, model_id: str, *, limit: int = 20) -> tuple[ProbeOutcome, ...]: ...


class CapabilityCheckStore(Protocol):
    def record(self, check: CapabilityCheck) -> UUID: ...

    def latest_for_model(self, model_id: str) -> tuple[CapabilityCheck, ...]: ...


class QuarantineStore(Protocol):
    def record(
        self,
        *,
        model_id: str,
        action: str,
        reason: str,
        consecutive_failures: int,
        cooldown_until: dt.datetime | None,
        policy_digest: str,
        now: dt.datetime | None = None,
    ) -> UUID: ...


class ProviderProbe(Protocol):
    """Sentetik probe'u gercekten calistiran adapter.

    Uygulama, yanit icerigini **saklamaz**; yalnizca sekil dogrulamasi icin
    kullanir ve ardindan digest'ini tutar.
    """

    def run(self, record: ModelRecord, fixture: ProbeFixture) -> Mapping[str, Any]:
        """Probe'u calistirir ve ham yaniti dondurur."""
        ...


class ProbeUnavailable(PolicyViolation):
    """Probe calistirilamiyor: yetki, ag veya adapter yok."""

    code = "probe-unavailable"


@dataclass(frozen=True, slots=True)
class AuthorizationRequiredProviderProbe:
    """Production varsayilani: exact provider authority yoksa probe calistirmaz.

    Bu adapterin amaci sentetik test adapterinin CLI production yoluna yanlislikla
    sizmasini engellemektir. Gercek probe adapteri AuthorizedProviderClient,
    claim ve terminal receipt zinciri tarafindan enjekte edilmelidir.
    """

    def run(self, record: ModelRecord, fixture: ProbeFixture) -> Mapping[str, Any]:
        del record, fixture
        raise ProbeUnavailable("Exact authorized provider health probe adapteri gerekli")


@dataclass(frozen=True, slots=True)
class StubProviderProbe:
    """Gelistirme ve test icin deterministik probe.

    Gercek bir saglayiciya baglanmaz. Hangi modelin nasil yanit verecegi
    onceden verilir; bu sayede karantina ve staleness kurallari ag olmadan
    dogrulanabilir.
    """

    responses: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    unavailable: frozenset[str] = frozenset()

    def run(self, record: ModelRecord, fixture: ProbeFixture) -> Mapping[str, Any]:
        if record.model_id in self.unavailable:
            raise ProbeUnavailable(f"Probe calistirilamadi: {record.model_id}")
        prepared = self.responses.get(record.model_id)
        if prepared is not None:
            return prepared
        return default_response(fixture)


def default_response(fixture: ProbeFixture) -> dict[str, Any]:
    """Fixture'a uygun, sekli gecerli sentetik yanit."""
    modality = fixture.modality
    if modality in {Modality.CHAT, Modality.CODE, Modality.COMPLETION}:
        return {"text": "Merhaba, saglik kontrolu yaniti."}
    if modality is Modality.EMBEDDING:
        return {"vectors": [[0.1, 0.2, 0.3, 0.4]]}
    if modality is Modality.RERANK:
        passages = fixture.payload.get("passages", [])
        return {"scores": [0.9 - index * 0.1 for index in range(len(passages))]}
    if modality is Modality.AUDIO_TRANSCRIPTION:
        return {"transcript": "sentetik ses transkripti"}
    if modality is Modality.GUARDRAIL:
        return {"labels": {"safe": "safe", "unsafe": "unsafe"}}
    if modality is Modality.VISION_LANGUAGE:
        return {"image_received": True, "text": "Gorsel kirmizi."}
    return {}


@dataclass(frozen=True, slots=True)
class HealthRunResult:
    """Tek bir model icin health calistirma sonucu."""

    record: ModelRecord
    outcome: ProbeOutcome
    decision: HealthDecision
    staleness: StalenessVerdict

    @property
    def quarantined(self) -> bool:
        return self.decision.state is HealthState.QUARANTINED

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.record.model_id,
            "access_name": self.record.access_name,
            "modality": self.record.modality.value,
            "outcome": self.outcome.as_dict(),
            "decision": self.decision.as_dict(),
            "staleness": self.staleness.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ModelHealthService:
    """Probe calistirma ve yasam dongusu yonetimi."""

    inventory: ModelInventoryStore
    probes: HealthProbeStore
    capabilities: CapabilityCheckStore
    quarantine: QuarantineStore
    probe: ProviderProbe
    policy: QuarantinePolicy = field(default_factory=QuarantinePolicy)

    # -- probe -------------------------------------------------------------------

    def run_probe(self, model_id: str, *, now: dt.datetime | None = None) -> HealthRunResult:
        """Modaliteye uygun sentetik probe'u calistirir ve sonucu kaydeder."""
        moment = now or dt.datetime.now(dt.UTC)
        record = self.inventory.get(model_id)
        fixture = fixture_for(record.modality)

        if record.modality is Modality.UNKNOWN:
            outcome = ProbeOutcome(
                model_id=model_id,
                modality=record.modality,
                fixture_name=fixture.name,
                status=ProbeStatus.SKIPPED,
                detail="modalite bilinmiyor; tahmin edilmez",
                fixture_digest=fixture.fixture_digest,
                observed_at=moment,
            )
            return self._persist(record, outcome, moment)

        started = time.monotonic()
        try:
            response = self.probe.run(record, fixture)
        except ProbeUnavailable as exc:
            outcome = ProbeOutcome(
                model_id=model_id,
                modality=record.modality,
                fixture_name=fixture.name,
                status=ProbeStatus.FAILED,
                failure=ProbeFailure.TRANSPORT,
                detail=str(exc),
                latency_ms=int((time.monotonic() - started) * 1000),
                fixture_digest=fixture.fixture_digest,
                observed_at=moment,
            )
            return self._persist(record, outcome, moment)
        except Exception as exc:
            outcome = ProbeOutcome(
                model_id=model_id,
                modality=record.modality,
                fixture_name=fixture.name,
                status=ProbeStatus.FAILED,
                failure=ProbeFailure.UNKNOWN,
                detail=type(exc).__name__,
                latency_ms=int((time.monotonic() - started) * 1000),
                fixture_digest=fixture.fixture_digest,
                observed_at=moment,
            )
            return self._persist(record, outcome, moment)

        latency_ms = int((time.monotonic() - started) * 1000)
        verdict = validate_shape(record.modality, response, fixture=fixture)
        outcome = ProbeOutcome(
            model_id=model_id,
            modality=record.modality,
            fixture_name=fixture.name,
            status=ProbeStatus.PASSED if verdict.valid else ProbeStatus.FAILED,
            failure=verdict.failure,
            detail=verdict.detail,
            latency_ms=latency_ms,
            fixture_digest=fixture.fixture_digest,
            # Yanit icerigi degil, yalnizca digest'i tutulur.
            response_digest=digest({"shape": sorted(response)}),
            observed_at=moment,
        )
        return self._persist(record, outcome, moment)

    def _persist(
        self, record: ModelRecord, outcome: ProbeOutcome, moment: dt.datetime
    ) -> HealthRunResult:
        self.probes.record(
            outcome,
            policy_digest=self.policy.policy_digest,
            inventory_digest=record.inventory_digest,
        )
        history = self.probes.history(record.model_id)
        decision = evaluate_health(history, policy=self.policy, now=moment)
        staleness = assess_staleness(
            recorded_inventory_digest=record.inventory_digest,
            current_inventory_digest=record.inventory_digest,
            recorded_policy_digest=self.policy.policy_digest,
            current_policy_digest=self.policy.policy_digest,
            last_checked_at=moment,
            policy=self.policy,
            now=moment,
        )
        self.inventory.set_health(
            record.model_id,
            state=decision.state,
            quarantine_until=decision.quarantine_until,
            benchmark_state=benchmark_state_for(decision, staleness),
            policy_digest=self.policy.policy_digest,
            inventory_digest=record.inventory_digest,
            now=moment,
        )
        if decision.state is HealthState.QUARANTINED:
            self.quarantine.record(
                model_id=record.model_id,
                action="quarantined",
                reason=decision.reason,
                consecutive_failures=decision.consecutive_failures,
                cooldown_until=decision.quarantine_until,
                policy_digest=self.policy.policy_digest,
                now=moment,
            )
        return HealthRunResult(
            record=self.inventory.get(record.model_id),
            outcome=outcome,
            decision=decision,
            staleness=staleness,
        )

    def run_all(self, *, now: dt.datetime | None = None) -> tuple[HealthRunResult, ...]:
        """Butun kayitli modeller icin probe calistirir."""
        return tuple(
            self.run_probe(record.model_id, now=now) for record in self.inventory.list_all()
        )

    # -- sozlesme ------------------------------------------------------------------

    def record_capability(
        self,
        model_id: str,
        *,
        capability: ContractCapability,
        verified: bool,
        evidence: str,
        failure: ProbeFailure | None = None,
        now: dt.datetime | None = None,
    ) -> CapabilityCheck:
        """Tek bir sozlesme kontrolunu kaydeder."""
        check = CapabilityCheck(
            model_id=model_id,
            capability=capability,
            verified=verified,
            evidence=evidence,
            failure=failure,
            checked_at=now or dt.datetime.now(dt.UTC),
        )
        self.capabilities.record(check)
        self._refresh_verified(model_id, now=now)
        return check

    def _refresh_verified(self, model_id: str, *, now: dt.datetime | None = None) -> None:
        record = self.inventory.get(model_id)
        checks = self.capabilities.latest_for_model(model_id)
        self.inventory.set_health(
            model_id,
            state=record.health_state,
            quarantine_until=record.quarantine_until,
            benchmark_state=record.benchmark_state,
            policy_digest=self.policy.policy_digest,
            inventory_digest=record.inventory_digest,
            verified_capabilities=verified_capabilities(checks),
            now=now,
        )

    def expected_contracts(self, model_id: str) -> tuple[ContractCapability, ...]:
        """Modalitesine gore sinanmasi gereken sozlesmeler."""
        return CONTRACTS_BY_MODALITY[self.inventory.get(model_id).modality]

    def promote_to_contract_passed(self, model_id: str, *, now: dt.datetime | None = None) -> bool:
        """Butun beklenen sozlesmeler dogrulandiysa durumu ilerletir."""
        record = self.inventory.get(model_id)
        if record.health_state is not HealthState.HEALTH_PASSED:
            return False
        expected = set(self.expected_contracts(model_id))
        verified = {
            check.capability
            for check in self.capabilities.latest_for_model(model_id)
            if check.verified
        }
        if not expected or not expected <= verified:
            return False
        self.inventory.set_health(
            model_id,
            state=HealthState.CONTRACT_PASSED,
            quarantine_until=None,
            benchmark_state=record.benchmark_state,
            policy_digest=self.policy.policy_digest,
            inventory_digest=record.inventory_digest,
            now=now,
        )
        return True

    # -- karantina ve staleness ----------------------------------------------------------

    def release_expired_quarantines(self, *, now: dt.datetime | None = None) -> tuple[str, ...]:
        """Cooldown suresi dolmus modelleri aday havuzuna geri alir."""
        moment = now or dt.datetime.now(dt.UTC)
        released: list[str] = []
        for record in self.inventory.list_all():
            if record.health_state is not HealthState.QUARANTINED:
                continue
            if record.quarantine_until is None or record.quarantine_until > moment:
                continue
            self.inventory.set_health(
                record.model_id,
                state=HealthState.UNTESTED,
                quarantine_until=None,
                benchmark_state=record.benchmark_state,
                policy_digest=self.policy.policy_digest,
                inventory_digest=record.inventory_digest,
                now=moment,
            )
            self.quarantine.record(
                model_id=record.model_id,
                action="released",
                reason="cooldown-doldu",
                consecutive_failures=0,
                cooldown_until=None,
                policy_digest=self.policy.policy_digest,
                now=moment,
            )
            released.append(record.model_id)
        return tuple(released)

    def staleness_of(self, model_id: str, *, now: dt.datetime | None = None) -> StalenessVerdict:
        """Kaydedilmis health sonucunun hala gecerli olup olmadigini soyler."""
        record = self.inventory.get(model_id)
        last_at, last_policy, last_inventory = self.inventory.health_metadata(model_id)
        return assess_staleness(
            recorded_inventory_digest=last_inventory,
            current_inventory_digest=record.inventory_digest,
            recorded_policy_digest=last_policy,
            current_policy_digest=self.policy.policy_digest,
            last_checked_at=last_at,
            policy=self.policy,
            now=now,
        )

    def stale_models(self, *, now: dt.datetime | None = None) -> tuple[str, ...]:
        """Yeniden test edilmesi gereken modeller."""
        return tuple(
            record.model_id
            for record in self.inventory.list_all()
            if self.staleness_of(record.model_id, now=now).stale
        )

    def benchmark_eligible(self, *, now: dt.datetime | None = None) -> tuple[ModelRecord, ...]:
        """Benchmark'a girebilecek modeller.

        Health basarisi yetenek kaniti degildir; yalnizca uygunluk saglar.
        """
        moment = now or dt.datetime.now(dt.UTC)
        return tuple(
            record
            for record in self.inventory.list_all()
            if record.is_benchmark_eligible(now=moment)
            and not self.staleness_of(record.model_id, now=moment).stale
        )

    def require_benchmark_eligible(
        self,
        model_id: str,
        *,
        inventory_digest: str,
        now: dt.datetime | None = None,
    ) -> ModelRecord:
        """Benchmark effect'inden once exact, fresh canonical health kaniti ister."""

        record = self.inventory.get(model_id)
        if record.inventory_digest != inventory_digest:
            raise PolicyViolation("Benchmark inventory digest canonical model ile eslesmiyor")
        eligible = {item.model_id for item in self.benchmark_eligible(now=now)}
        if record.model_id not in eligible:
            raise PolicyViolation("Model fresh health-passed olmadigi icin benchmark disi")
        return record


def contract_coverage(modality: Modality, checks: Sequence[CapabilityCheck]) -> dict[str, bool]:
    """Beklenen sozlesmelerin hangilerinin dogrulandigini gosterir."""
    verified = {check.capability for check in checks if check.verified}
    return {
        capability.value: capability in verified for capability in CONTRACTS_BY_MODALITY[modality]
    }


def realm_identity(realm: Realm) -> UUID:  # pragma: no cover - yardimci
    return realm.id
