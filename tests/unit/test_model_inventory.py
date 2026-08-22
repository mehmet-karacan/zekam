"""Model envanteri yukleme, kimlik ve referans kurallari."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from zekam.application.model_registry import (
    load_inventory,
    record_from_mapping,
    summarize_snapshot,
    verify_snapshot,
)
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed
from zekam.domain.model_inventory import (
    CANONICAL_MODEL_COUNT,
    TECHNICAL_PROFILE_COUNT,
    InventorySnapshot,
    Modality,
    ModelProvenance,
    ModelRecord,
    ProviderProtocol,
    assert_no_merged_identities,
    validate_reference,
)

pytestmark = pytest.mark.unit


def _record(**overrides: object) -> ModelRecord:
    defaults: dict[str, object] = {
        "model_id": "aaaa-1111",
        "inventory_index": 1,
        "access_name": "openai/ornek",
        "backend_model": "openai/Ornek-7B",
        "provider_protocol": ProviderProtocol.OPENAI,
        "declared_category": "chat",
        "endpoint_ref": "model-endpoint:aaaa-1111",
        "credential_ref": "model-credential:aaaa-1111",
        "provenance": ModelProvenance(
            canonical_report="rapor.md", technical_profile_available=True
        ),
    }
    defaults.update(overrides)
    return ModelRecord(**defaults)  # type: ignore[arg-type]


# -- referans guvenligi ------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "https://api.example.com/v1",
        "model-endpoint:https://api.example.com",
        "model-credential:sk-abcdefgh12345678",
        "model-endpoint:10.0.0.1",
        "model-credential:AKIAZZZZQQQQWWWWEEEE",
        "model-credential:Bearer abcdefgh",
        "model-credential:" + "A" * 45,
    ],
)
def test_raw_values_are_rejected_in_references(value: str) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        validate_reference(value, kind="endpoint")


@pytest.mark.parametrize("value", ["model-endpoint:aaaa-1111", "model-credential:model_42.v2"])
def test_logical_references_are_accepted(value: str) -> None:
    assert validate_reference(value, kind="endpoint") == value


def test_record_rejects_a_raw_endpoint() -> None:
    with pytest.raises((PolicyViolation, ValidationFailed)):
        _record(endpoint_ref="https://api.example.com/v1")


def test_record_body_has_no_raw_value() -> None:
    rendered = repr(_record().body())
    assert "://" not in rendered
    assert "sk-" not in rendered


# -- kimlik ---------------------------------------------------------------------------


def test_duplicate_model_ids_are_rejected() -> None:
    with pytest.raises(ValidationFailed, match="birlestirilemez"):
        assert_no_merged_identities([_record(), _record()])


def test_same_backend_with_different_ids_is_kept_separate() -> None:
    snapshot = InventorySnapshot(
        schema="zekam-model-inventory/v1",
        inventory_date=dt.date(2026, 8, 20),
        records=(
            _record(model_id="a", inventory_index=1, access_name="a", backend_model="ortak"),
            _record(model_id="b", inventory_index=2, access_name="b", backend_model="ortak"),
        ),
    )
    assert snapshot.canonical_count == 2
    assert snapshot.duplicated_backends() == {"ortak": ("a", "b")}


def test_snapshot_rejects_duplicate_ids() -> None:
    with pytest.raises(ValidationFailed):
        InventorySnapshot(
            schema="zekam-model-inventory/v1",
            inventory_date=dt.date(2026, 8, 20),
            records=(_record(), _record()),
        )


def test_inventory_digest_is_deterministic() -> None:
    assert _record().inventory_digest == _record().inventory_digest


def test_inventory_digest_changes_with_content() -> None:
    assert _record().inventory_digest != _record(access_name="baska").inventory_digest


# -- modalite ---------------------------------------------------------------------------


def test_category_drives_capability_and_mode_drives_invocation() -> None:
    record = _record(declared_category="multimodal_generation", declared_mode="completion")
    assert record.modality is Modality.VISION_LANGUAGE
    assert record.invocation_modality is Modality.COMPLETION


def test_modality_conflict_is_visible() -> None:
    record = _record(declared_category="multimodal_generation", declared_mode="completion")
    conflict = record.modality_conflict
    assert conflict == (Modality.COMPLETION, Modality.VISION_LANGUAGE)
    assert record.as_dict()["modality_conflict"] == ["completion", "vision_language"]


def test_matching_mode_and_category_have_no_conflict() -> None:
    assert _record(declared_category="chat", declared_mode="chat").modality_conflict is None


def test_unknown_category_is_not_guessed() -> None:
    assert _record(declared_category="bilinmeyen-kategori").modality is Modality.UNKNOWN


# -- kanonik dosya -------------------------------------------------------------------------


def test_shipped_inventory_loads() -> None:
    snapshot = load_inventory()
    assert snapshot.canonical_count == CANONICAL_MODEL_COUNT
    assert snapshot.technical_profile_count == TECHNICAL_PROFILE_COUNT


def test_shipped_inventory_keeps_the_profile_gap_visible() -> None:
    snapshot = load_inventory()
    missing = snapshot.missing_technical_profile
    assert len(missing) == CANONICAL_MODEL_COUNT - TECHNICAL_PROFILE_COUNT
    assert all(record.provenance.verification_note.strip() for record in missing)


def test_shipped_inventory_has_twenty_unique_ids() -> None:
    snapshot = load_inventory()
    assert len({record.model_id for record in snapshot.records}) == CANONICAL_MODEL_COUNT


def test_shipped_inventory_carries_no_raw_endpoint() -> None:
    for record in load_inventory().records:
        validate_reference(record.endpoint_ref, kind="endpoint")
        validate_reference(record.credential_ref, kind="credential")


def test_shipped_inventory_keeps_duplicate_backend_separate() -> None:
    duplicated = load_inventory().duplicated_backends()
    assert duplicated
    for identifiers in duplicated.values():
        assert len(set(identifiers)) == len(identifiers)


def test_verify_reports_no_count_discrepancy_for_shipped_file() -> None:
    findings = verify_snapshot(load_inventory())
    kinds = {item.kind for item in findings}
    assert "canonical-count-mismatch" not in kinds
    assert "technical-profile-count-mismatch" not in kinds


def test_verify_reports_modality_conflicts() -> None:
    findings = verify_snapshot(load_inventory())
    assert any(item.kind == "modality-conflict" for item in findings)


def test_verify_detects_a_wrong_expected_count() -> None:
    findings = verify_snapshot(load_inventory(), expected_canonical=21)
    assert any(item.kind == "canonical-count-mismatch" for item in findings)


def test_summary_lists_modalities() -> None:
    summary = summarize_snapshot(load_inventory())
    assert summary["canonical_count"] == CANONICAL_MODEL_COUNT
    assert sum(summary["modalities"].values()) == CANONICAL_MODEL_COUNT


def test_missing_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        load_inventory(tmp_path / "yok.yaml")


def test_wrong_schema_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "envanter.yaml"
    target.write_text("schema: baska/v1\nmodels: []\n", encoding="utf-8")
    with pytest.raises(ValidationFailed):
        load_inventory(target)


def test_unknown_protocol_is_not_guessed() -> None:
    record = record_from_mapping(
        {
            "model_id": "x",
            "inventory_index": 1,
            "access_name": "x",
            "backend_model": "x",
            "provider_protocol": "hicbir-protokol",
            "declared_category": "chat",
            "endpoint_ref": "model-endpoint:x",
            "credential_ref": "model-credential:x",
            "source": {"canonical_report": "r.md"},
        }
    )
    assert record.provider_protocol is ProviderProtocol.UNKNOWN


def test_benchmark_eligibility_requires_health() -> None:
    assert not _record().is_benchmark_eligible()


def test_disabled_model_is_never_eligible() -> None:
    from zekam.domain.model_inventory import HealthState

    record = _record(enabled=False, health_state=HealthState.HEALTH_PASSED)
    assert not record.is_benchmark_eligible()


def test_declared_capability_is_not_verified() -> None:
    record = _record(capabilities_declared=("tool-call",))
    assert record.declares("tool-call")
    assert not record.verified("tool-call")
