from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest

from zekam.application.chaos_campaign import (
    CompositeFaultInjector,
    FaultExecutionEvidence,
    GovernedRuntimeFaultProbe,
    compose_chaos_campaign_handler,
    persist_chaos_campaign,
    run_chaos_campaign,
)
from zekam.domain.canonical import digest
from zekam.domain.chaos_campaign import (
    ChaosAuditEvent,
    ChaosCampaignPlan,
    ChaosCampaignPolicy,
    ChaosCampaignStatus,
    ChaosObservation,
    ChaosOperatorRecord,
    ChaosScenario,
    ChaosVerifierVerdict,
    FaultInjectionAuthorization,
    FaultInjectionReceipt,
    FaultPoint,
    RuntimeSafetySnapshot,
    default_chaos_scenarios,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.scheduler import REQUIRED_JOB_INTERVALS, REQUIRED_JOBS
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

NOW = dt.datetime(2026, 8, 25, 12, 0, tzinfo=dt.UTC)


def _plan(*, repetitions: int = 1) -> ChaosCampaignPlan:
    scenarios = default_chaos_scenarios()
    return ChaosCampaignPlan(
        campaign_id="chaos-2026-08-25",
        source_revision="abc123",
        suite_digest=digest([item.as_dict() for item in scenarios]),
        policy=ChaosCampaignPolicy(repetitions=repetitions),
        scenarios=scenarios,
    )


def _evidence(scenario: ChaosScenario, repetition: int) -> FaultExecutionEvidence:
    seed = (str(scenario.fault_point), repetition)
    before_head = digest((*seed, "audit-before"))
    before = RuntimeSafetySnapshot(
        realm_id="chaos-realm",
        authority_bindings=("work:owner-1",),
        irreversible_effect_occurrences=(),
        non_terminal_state_refs=(f"attempt:{scenario.fault_point}:running",),
        accessed_realm_ids=("chaos-realm",),
        audit_head_digest=before_head,
    )
    event = ChaosAuditEvent(
        realm_id="chaos-realm",
        sequence=1,
        event_type=f"fault:{scenario.fault_point}",
        record_ref=f"fault-receipt:{scenario.fault_point}:{repetition}",
        previous_digest=before_head,
    )
    state_ref = f"incident:{scenario.fault_point}:recovery-required"
    after = RuntimeSafetySnapshot(
        realm_id="chaos-realm",
        authority_bindings=("work:owner-1",),
        irreversible_effect_occurrences=(),
        non_terminal_state_refs=(state_ref,),
        accessed_realm_ids=("chaos-realm",),
        audit_head_digest=event.event_digest,
    )
    authorization = FaultInjectionAuthorization(
        realm_id="chaos-realm",
        scenario_digest=scenario.scenario_digest,
        target=scenario.target,
        actor_identity=f"probe/{scenario.fault_point}",
        valid_from=NOW - dt.timedelta(minutes=1),
        valid_until=NOW + dt.timedelta(minutes=1),
        repetition=repetition,
        authorization_record_digest=digest((*seed, "authorization-record")),
    )
    receipt = FaultInjectionReceipt(
        scenario_digest=scenario.scenario_digest,
        repetition=repetition,
        injector_identity=f"probe/{scenario.fault_point}",
        authorization_digest=authorization.authorization_digest,
        before_snapshot_digest=before.snapshot_digest,
        after_snapshot_digest=after.snapshot_digest,
        started_at=NOW,
        completed_at=NOW + dt.timedelta(milliseconds=1),
    )
    operator = ChaosOperatorRecord(
        realm_id="chaos-realm",
        scenario_digest=scenario.scenario_digest,
        state_ref=state_ref,
        next_safe_action=scenario.expected_next_action,
        audit_head_digest=event.event_digest,
    )
    return FaultExecutionEvidence(authorization, receipt, before, after, (event,), operator)


class ExecutableProbe:
    def __init__(self, fault_point: FaultPoint) -> None:
        self.fault_point = fault_point
        self.injector_identity = f"probe/{fault_point}"
        self.calls = 0

    def execute(self, scenario: ChaosScenario, *, repetition: int) -> FaultExecutionEvidence:
        assert scenario.fault_point is self.fault_point
        self.calls += 1
        return _evidence(scenario, repetition)


class IndependentVerifier:
    verifier_identity = "independent-chaos-verifier/v1"

    def verify(self, evidence: FaultExecutionEvidence) -> ChaosVerifierVerdict:
        evidence_digest = digest(evidence.evidence_body())
        body = {
            "verifier_identity": self.verifier_identity,
            "evidence_bundle_digest": evidence_digest,
            "accepted": True,
        }
        return ChaosVerifierVerdict(self.verifier_identity, evidence_digest, True, digest(body))


def _injector() -> tuple[CompositeFaultInjector, dict[FaultPoint, ExecutableProbe]]:
    probes = {fault: ExecutableProbe(fault) for fault in FaultPoint}
    return CompositeFaultInjector(probes, IndependentVerifier()), probes


def test_default_matrix_rapordaki_tum_fault_noktalarini_kapsar() -> None:
    scenarios = default_chaos_scenarios()
    assert tuple(item.fault_point for item in scenarios) == tuple(FaultPoint)
    assert len(scenarios) == 15


def test_kampanya_her_executable_probeu_cagirip_guvenlik_kapilarini_turetir() -> None:
    injector, probes = _injector()
    result = run_chaos_campaign(_plan(), injector, completed_at=NOW)
    assert result.status is ChaosCampaignStatus.PASSED
    assert all(probe.calls == 1 for probe in probes.values())
    assert all(item.passed for item in result.observations)
    assert len({item.receipt.receipt_digest for item in result.observations}) == len(FaultPoint)


def test_eksik_ve_semantigi_degistirilmis_matrix_reddedilir() -> None:
    plan = _plan()
    with pytest.raises(ValidationFailed):
        replace(plan, scenarios=plan.scenarios[:-1])
    forged = replace(plan.scenarios[0], target="not-real-target")
    scenarios = (forged, *plan.scenarios[1:])
    with pytest.raises(ValidationFailed):
        ChaosCampaignPlan(
            plan.campaign_id,
            plan.source_revision,
            digest([item.as_dict() for item in scenarios]),
            plan.policy,
            scenarios,
        )


def test_self_attested_verifier_reddedilir() -> None:
    scenario = default_chaos_scenarios()[0]
    evidence = _evidence(scenario, 1)
    evidence_digest = digest(evidence.evidence_body())
    body = {
        "verifier_identity": evidence.receipt.injector_identity,
        "evidence_bundle_digest": evidence_digest,
        "accepted": True,
    }
    verdict = ChaosVerifierVerdict(
        evidence.receipt.injector_identity, evidence_digest, True, digest(body)
    )
    with pytest.raises(Exception, match="kendi kanitini verify"):
        ChaosObservation(
            evidence.authorization,
            evidence.receipt,
            evidence.before,
            evidence.after,
            evidence.audit_events,
            evidence.operator_record,
            verdict,
        )


def test_audit_operator_ve_realm_tamperi_reddedilir() -> None:
    scenario = default_chaos_scenarios()[0]
    evidence = _evidence(scenario, 1)
    injector, _ = _injector()
    observation = injector.inject(scenario, repetition=1)
    with pytest.raises(ValidationFailed):
        replace(observation, audit_events=(replace(evidence.audit_events[0], sequence=2),))
    with pytest.raises(ValidationFailed):
        replace(
            observation,
            operator_record=replace(evidence.operator_record, state_ref="made-up"),
        )
    with pytest.raises(ValidationFailed):
        replace(
            observation,
            after=replace(evidence.after, accessed_realm_ids=("another-realm",)),
        )


def test_yeni_irreversible_effect_semantik_olarak_failed_olur() -> None:
    scenario = default_chaos_scenarios()[0]
    evidence = _evidence(scenario, 1)
    changed_after = replace(
        evidence.after, irreversible_effect_occurrences=("unexpected-effect:1",)
    )
    changed_receipt = replace(evidence.receipt, after_snapshot_digest=changed_after.snapshot_digest)
    changed = replace(evidence, receipt=changed_receipt, after=changed_after)
    verdict = IndependentVerifier().verify(changed)
    observation = ChaosObservation(
        changed.authorization,
        changed.receipt,
        changed.before,
        changed.after,
        changed.audit_events,
        changed.operator_record,
        verdict,
    )
    assert not observation.passed


def test_authorization_exact_repetition_ve_targeta_baglidir() -> None:
    injector, _ = _injector()
    result = run_chaos_campaign(_plan(), injector, completed_at=NOW)
    object.__setattr__(result.observations[0].authorization, "target", "wrong-target")
    with pytest.raises(PolicyViolation, match="authorization kapsami"):
        result.validate()
    fresh = _evidence(default_chaos_scenarios()[0], 1)
    changed = replace(fresh, receipt=replace(fresh.receipt, repetition=2))
    verdict = IndependentVerifier().verify(changed)
    with pytest.raises(PolicyViolation, match="authorization kapsami"):
        ChaosObservation(
            changed.authorization,
            changed.receipt,
            changed.before,
            changed.after,
            changed.audit_events,
            changed.operator_record,
            verdict,
        )


def test_bad_authorization_fault_effectinden_once_reddedilir() -> None:
    scenario = default_chaos_scenarios()[0]
    evidence = _evidence(scenario, 1)

    class BadAuthorizationProvider:
        def issue(self, scenario, *, repetition, actor_identity):  # type: ignore[no-untyped-def]
            return replace(evidence.authorization, target="wrong-target")

        def verify_current(self, authorization):  # type: ignore[no-untyped-def]
            return True

    class CountingRuntime:
        fault_point = scenario.fault_point

        def __init__(self) -> None:
            self.inject_calls = 0

        def capture_safety_snapshot(self):  # type: ignore[no-untyped-def]
            return evidence.before

        def inject_fault(self, scenario, *, repetition, authorization):  # type: ignore[no-untyped-def]
            self.inject_calls += 1

        def audit_events_since(self, previous_digest):  # type: ignore[no-untyped-def]
            return evidence.audit_events

        def operator_record(self, scenario):  # type: ignore[no-untyped-def]
            return evidence.operator_record

    runtime = CountingRuntime()
    probe = GovernedRuntimeFaultProbe(
        scenario.fault_point,
        evidence.receipt.injector_identity,
        runtime,
        BadAuthorizationProvider(),
    )
    with pytest.raises(PolicyViolation, match="effect oncesi"):
        probe.execute(scenario, repetition=1)
    assert runtime.inject_calls == 0


def test_sonuc_casa_read_after_write_ile_kalici_yazilir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    injector, _ = _injector()
    result = run_chaos_campaign(_plan(), injector, completed_at=NOW)
    store = LocalContentAddressedStore(tmp_path).ensure()
    stored = persist_chaos_campaign(result, store)
    assert stored.result_digest == result.result_digest
    assert stored.status == "passed"
    assert not stored.grants_authority
    assert store.get(stored.object_digest) == result.to_bytes()


def test_nested_observation_tamperi_persistence_oncesi_reddedilir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    injector, _ = _injector()
    result = run_chaos_campaign(_plan(), injector, completed_at=NOW)
    object.__setattr__(result.observations[0].operator_record, "next_safe_action", "forged")
    store = LocalContentAddressedStore(tmp_path).ensure()
    with pytest.raises(ValidationFailed):
        persist_chaos_campaign(result, store)
    assert not tuple((tmp_path / "sha256").rglob("*.bin"))


def test_campaign_schedulerda_haftalik_zorunlu_bakim_isidir() -> None:
    assert "chaos-campaign" in REQUIRED_JOBS
    assert REQUIRED_JOB_INTERVALS["chaos-campaign"] == "7d"


def test_explicit_scheduler_handler_matrixi_calistirip_kaniti_yazar(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = LocalContentAddressedStore(tmp_path).ensure()
    injector, probes = _injector()
    handler = compose_chaos_campaign_handler(_plan(), injector, store)
    detail = handler(NOW)
    assert detail.startswith("chaos campaign passed; evidence=sha256:")
    assert all(probe.calls == 1 for probe in probes.values())
    assert len(tuple((tmp_path / "sha256").rglob("*.bin"))) == 1
