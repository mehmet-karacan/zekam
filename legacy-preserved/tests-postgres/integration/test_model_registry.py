"""Model envanteri, probe, sozlesme ve rapor akisinin gercek PostgreSQL davranisi."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from zekam.application.model_health_service import (
    ModelHealthService,
    ProbeUnavailable,
    StubProviderProbe,
)
from zekam.application.model_registry import load_inventory
from zekam.application.model_report import build_report
from zekam.domain.errors import NotFound, PolicyViolation
from zekam.domain.model_health import ContractCapability, ProbeStatus, QuarantinePolicy
from zekam.domain.model_inventory import (
    CANONICAL_MODEL_COUNT,
    TECHNICAL_PROFILE_COUNT,
    BenchmarkState,
    HealthState,
    Modality,
)
from zekam.domain.realm import Realm
from zekam.infrastructure.postgres.model_health_composition import (
    compose_model_health_service,
)
from zekam.infrastructure.postgres.model_repository import (
    CapabilityCheckRepository,
    HealthReportRepository,
    ModelInventoryRepository,
    QuarantineRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
def repository(realm_session: tuple[Realm, Any]) -> ModelInventoryRepository:
    realm, connection = realm_session
    return ModelInventoryRepository(connection, realm.id)


@pytest.fixture
def imported(repository: ModelInventoryRepository) -> ModelInventoryRepository:
    for record in load_inventory().records:
        repository.upsert(record)
    return repository


def _service(
    realm_session: tuple[Realm, Any], probe: StubProviderProbe | None = None
) -> ModelHealthService:
    realm, connection = realm_session
    return compose_model_health_service(
        connection, realm, probe=probe or StubProviderProbe()
    )


# -- envanter ---------------------------------------------------------------------------


def test_import_stores_every_model(imported: ModelInventoryRepository) -> None:
    assert len(imported.list_all()) == CANONICAL_MODEL_COUNT


def test_import_is_idempotent(imported: ModelInventoryRepository) -> None:
    outcomes = [imported.upsert(record) for record in load_inventory().records]
    assert set(outcomes) == {"unchanged"}


def test_changed_record_is_updated(imported: ModelInventoryRepository) -> None:
    original = load_inventory().records[0]
    from dataclasses import replace

    modified = replace(original, access_name="openai/degisti")
    assert imported.upsert(modified) == "updated"
    assert imported.get(original.model_id).access_name == "openai/degisti"


def test_technical_profile_gap_survives_the_import(
    imported: ModelInventoryRepository,
) -> None:
    records = imported.list_all()
    with_profile = [record for record in records if record.has_technical_profile]
    assert len(with_profile) == TECHNICAL_PROFILE_COUNT
    assert len(records) - len(with_profile) == 1


def test_duplicate_backend_stays_as_two_records(
    imported: ModelInventoryRepository,
) -> None:
    records = imported.list_all()
    backends = [record.backend_model for record in records]
    assert backends.count("BAAI/bge-reranker-v2-m3") == 2


def test_raw_endpoint_is_rejected_by_database(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    _, connection = realm_session
    with (
        pytest.raises(Exception, match=r"model_endpoint_ref_format|model_refs_have_no_url"),
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "update models.model_inventory set endpoint_ref = %s",
            ("https://api.example.com/v1",),
        )


def test_missing_model_raises_not_found(repository: ModelInventoryRepository) -> None:
    with pytest.raises(NotFound):
        repository.get("olmayan-model")


def test_models_are_listed_by_modality(imported: ModelInventoryRepository) -> None:
    embeddings = imported.list_by_modality(Modality.EMBEDDING)
    assert len(embeddings) == 3
    assert all(record.modality is Modality.EMBEDDING for record in embeddings)


# -- health probe -------------------------------------------------------------------------


def test_probe_passes_for_every_known_modality(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    service = _service(realm_session)
    results = service.run_all()
    assert len(results) == CANONICAL_MODEL_COUNT
    assert all(item.outcome.status is ProbeStatus.PASSED for item in results)
    assert all(item.decision.state is HealthState.HEALTH_PASSED for item in results)


def test_probe_stores_no_prompt_or_response_content(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    _, connection = realm_session
    service = _service(realm_session)
    model_id = imported.list_all()[0].model_id
    service.run_probe(model_id)
    with connection.cursor() as cursor:
        cursor.execute(
            "select count(*) from models.health_probe where detail like %s", ("%Merhaba%",)
        )
        assert int(cursor.fetchone()[0]) == 0


def test_bad_shape_fails_the_probe(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    model_id = imported.list_by_modality(Modality.CHAT)[0].model_id
    service = _service(realm_session, StubProviderProbe(responses={model_id: {"text": ""}}))
    result = service.run_probe(model_id)
    assert result.outcome.status is ProbeStatus.FAILED
    assert result.outcome.failure is not None


def test_unavailable_probe_is_a_transport_failure(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    model_id = imported.list_all()[0].model_id
    service = _service(realm_session, StubProviderProbe(unavailable=frozenset({model_id})))
    result = service.run_probe(model_id)
    assert result.outcome.status is ProbeStatus.FAILED
    assert result.outcome.failure is not None
    assert result.outcome.failure.value == "transport"


def test_probe_history_is_append_only(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    _, connection = realm_session
    service = _service(realm_session)
    model_id = imported.list_all()[0].model_id
    service.run_probe(model_id)
    with (
        pytest.raises(Exception, match=r"append-only|permission denied"),
        connection.cursor() as cursor,
    ):
        cursor.execute("delete from models.health_probe")


# -- karantina ----------------------------------------------------------------------------


def test_two_consecutive_failures_quarantine_the_model(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    realm, connection = realm_session
    model_id = imported.list_by_modality(Modality.CHAT)[0].model_id
    service = _service(realm_session, StubProviderProbe(responses={model_id: {"text": ""}}))

    first = service.run_probe(model_id)
    assert first.decision.state is HealthState.UNTESTED

    second = service.run_probe(model_id)
    assert second.decision.state is HealthState.QUARANTINED
    assert imported.get(model_id).health_state is HealthState.QUARANTINED
    assert imported.get(model_id).benchmark_state is BenchmarkState.FAILED

    events = QuarantineRepository(connection, realm.id).history(model_id)
    assert events[-1]["action"] == "quarantined"


def test_quarantined_model_is_not_benchmark_eligible(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    model_id = imported.list_by_modality(Modality.CHAT)[0].model_id
    service = _service(realm_session, StubProviderProbe(responses={model_id: {"text": ""}}))
    service.run_probe(model_id)
    service.run_probe(model_id)
    eligible = {record.model_id for record in service.benchmark_eligible()}
    assert model_id not in eligible


def test_benchmark_gate_requires_exact_digest_and_fresh_health(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    service = _service(realm_session)
    record = imported.list_all()[0]

    with pytest.raises(PolicyViolation, match="health-passed"):
        service.require_benchmark_eligible(
            record.model_id,
            inventory_digest=record.inventory_digest,
        )

    service.run_probe(record.model_id)
    assert (
        service.require_benchmark_eligible(
            record.model_id,
            inventory_digest=record.inventory_digest,
        ).model_id
        == record.model_id
    )
    with pytest.raises(PolicyViolation, match="inventory digest"):
        service.require_benchmark_eligible(
            record.model_id,
            inventory_digest="sha256:" + "0" * 64,
        )


def test_cooldown_release_returns_the_model_to_candidates(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    model_id = imported.list_by_modality(Modality.CHAT)[0].model_id
    policy = QuarantinePolicy(cooldown=dt.timedelta(minutes=5))
    realm, connection = realm_session
    service = compose_model_health_service(
        connection,
        realm,
        probe=StubProviderProbe(responses={model_id: {"text": ""}}),
        policy=policy,
    )
    service.run_probe(model_id)
    service.run_probe(model_id)
    assert imported.get(model_id).health_state is HealthState.QUARANTINED

    later = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10)
    released = service.release_expired_quarantines(now=later)
    assert model_id in released
    assert imported.get(model_id).health_state is HealthState.UNTESTED


def test_cooldown_is_respected_before_release(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    model_id = imported.list_by_modality(Modality.CHAT)[0].model_id
    service = _service(realm_session, StubProviderProbe(responses={model_id: {"text": ""}}))
    service.run_probe(model_id)
    service.run_probe(model_id)
    assert service.release_expired_quarantines() == ()


# -- staleness ------------------------------------------------------------------------------


def test_result_is_fresh_right_after_the_probe(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    service = _service(realm_session)
    model_id = imported.list_all()[0].model_id
    service.run_probe(model_id)
    assert not service.staleness_of(model_id).stale


def test_untested_model_is_stale(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    service = _service(realm_session)
    assert service.staleness_of(imported.list_all()[0].model_id).stale


def test_policy_change_makes_the_result_stale(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    realm, connection = realm_session
    model_id = imported.list_all()[0].model_id
    compose_model_health_service(
        connection, realm, probe=StubProviderProbe()
    ).run_probe(model_id)

    changed = compose_model_health_service(
        connection,
        realm,
        probe=StubProviderProbe(),
        policy=QuarantinePolicy(consecutive_failure_threshold=5),
    )
    verdict = changed.staleness_of(model_id)
    assert verdict.stale
    assert any(reason.value == "policy-changed" for reason in verdict.reasons)


def test_inventory_change_makes_the_result_stale(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    from dataclasses import replace

    service = _service(realm_session)
    record = imported.list_all()[0]
    service.run_probe(record.model_id)
    imported.upsert(replace(record, access_name="openai/yeni-ad"))

    verdict = service.staleness_of(record.model_id)
    assert verdict.stale
    assert any(reason.value == "inventory-changed" for reason in verdict.reasons)


def test_stale_models_are_listed(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    service = _service(realm_session)
    assert len(service.stale_models()) == CANONICAL_MODEL_COUNT
    service.run_all()
    assert service.stale_models() == ()


# -- sozlesme -------------------------------------------------------------------------------


def test_capability_check_is_recorded_and_reflected(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    service = _service(realm_session)
    model_id = imported.list_by_modality(Modality.CHAT)[0].model_id
    service.record_capability(
        model_id,
        capability=ContractCapability.TURKISH,
        verified=True,
        evidence="Turkce karakterler korundu",
    )
    assert "turkish" in imported.get(model_id).capabilities_verified


def test_unverified_capability_is_not_reported_as_verified(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    service = _service(realm_session)
    model_id = imported.list_by_modality(Modality.CHAT)[0].model_id
    service.record_capability(
        model_id,
        capability=ContractCapability.TOOL_CALL,
        verified=False,
        evidence="tool cagrisi desteklenmiyor",
    )
    assert "tool-call" not in imported.get(model_id).capabilities_verified


def test_latest_check_wins(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    realm, connection = realm_session
    service = _service(realm_session)
    model_id = imported.list_by_modality(Modality.CHAT)[0].model_id
    moment = dt.datetime.now(dt.UTC)
    service.record_capability(
        model_id,
        capability=ContractCapability.JSON_SCHEMA,
        verified=True,
        evidence="ilk kontrol",
        now=moment,
    )
    service.record_capability(
        model_id,
        capability=ContractCapability.JSON_SCHEMA,
        verified=False,
        evidence="ikinci kontrol basarisiz",
        now=moment + dt.timedelta(minutes=1),
    )
    checks = CapabilityCheckRepository(connection, realm.id).latest_for_model(model_id)
    assert len(checks) == 1
    assert checks[0].verified is False


def test_contract_promotion_requires_every_expected_capability(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    service = _service(realm_session)
    model_id = imported.list_by_modality(Modality.EMBEDDING)[0].model_id
    service.run_probe(model_id)
    assert not service.promote_to_contract_passed(model_id)

    for capability in service.expected_contracts(model_id):
        service.record_capability(
            model_id, capability=capability, verified=True, evidence="dogrulandi"
        )
    assert service.promote_to_contract_passed(model_id)
    assert imported.get(model_id).health_state is HealthState.CONTRACT_PASSED


def test_capability_checks_are_append_only(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    _, connection = realm_session
    service = _service(realm_session)
    model_id = imported.list_all()[0].model_id
    service.record_capability(
        model_id, capability=ContractCapability.TIMEOUT_BEHAVIOR, verified=True, evidence="ok"
    )
    with (
        pytest.raises(Exception, match=r"append-only|permission denied"),
        connection.cursor() as cursor,
    ):
        cursor.execute("update models.capability_check set verified = false")


# -- rapor -----------------------------------------------------------------------------------


def test_report_binds_markdown_and_json_to_the_same_evidence(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    service = _service(realm_session)
    service.run_all()
    report = build_report(imported.list_all(), report_date=dt.date(2026, 8, 20), stale_model_ids=())
    assert report.evidence_digest in report.as_markdown()
    assert report.evidence_digest in report.as_json()
    assert report.markdown_digest != report.json_digest


def test_report_shows_the_profile_gap(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    report = build_report(imported.list_all(), report_date=dt.date(2026, 8, 20))
    assert report.canonical_count == CANONICAL_MODEL_COUNT
    assert report.technical_profile_count == TECHNICAL_PROFILE_COUNT
    assert report.profile_gap == 1
    assert "Görünür profil farkı: **1**" in report.as_markdown()


def test_report_lists_quarantined_models(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    model_id = imported.list_by_modality(Modality.CHAT)[0].model_id
    service = _service(realm_session, StubProviderProbe(responses={model_id: {"text": ""}}))
    service.run_probe(model_id)
    service.run_probe(model_id)
    report = build_report(imported.list_all(), report_date=dt.date(2026, 8, 20))
    assert len(report.quarantined) == 1
    assert "Karantinadaki modeller" in report.as_markdown()


def test_report_contains_no_raw_endpoint(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    report = build_report(imported.list_all(), report_date=dt.date(2026, 8, 20))
    for rendered in (report.as_markdown(), report.as_json()):
        assert "://" not in rendered
        assert "model-credential:" not in rendered


def test_report_is_stored_once_per_day(
    realm_session: tuple[Realm, Any], imported: ModelInventoryRepository
) -> None:
    realm, connection = realm_session
    repository = HealthReportRepository(connection, realm.id)
    report = build_report(imported.list_all(), report_date=dt.date(2026, 8, 20))
    first = repository.store(
        report_date=report.report_date,
        summary=report.summary(),
        evidence_digest=report.evidence_digest,
        markdown_digest=report.markdown_digest,
        json_digest=report.json_digest,
    )
    second = repository.store(
        report_date=report.report_date,
        summary=report.summary(),
        evidence_digest=report.evidence_digest,
        markdown_digest=report.markdown_digest,
        json_digest=report.json_digest,
    )
    assert first == second
    stored = repository.find(report.report_date)
    assert stored is not None
    assert stored["evidence_digest"] == report.evidence_digest


def test_probe_unavailable_error_is_a_policy_violation() -> None:
    from zekam.domain.errors import PolicyViolation

    assert issubclass(ProbeUnavailable, PolicyViolation)
