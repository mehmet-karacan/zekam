"""Model envanterini yukleme, dogrulama ve iceri aktarma.

Yukleme sirasinda:

- 20 kanonik kayit ve 19 teknik profil beklentisi dogrulanir; fark gizlenmez.
- Ayni backend adini paylasan farkli Model ID'ler **birlestirilmez**.
- Ham endpoint veya credential degeri tasiyan kayit reddedilir.
- Bilinmeyen alan tahmin edilmez; `None` kalir.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.domain.model_inventory import (
    CANONICAL_MODEL_COUNT,
    TECHNICAL_PROFILE_COUNT,
    BenchmarkState,
    CostEvidence,
    HealthState,
    InventoryDiscrepancy,
    InventorySnapshot,
    ModelProvenance,
    ModelRecord,
    ProviderProtocol,
    assert_no_merged_identities,
    check_inventory,
)

INVENTORY_SCHEMA = "zekam-model-inventory/v1"


def default_inventory_file() -> Path:
    """Core dagitimindaki kanonik envanter dosyasi."""
    from zekam.application.config import core_root

    return core_root() / "modeller" / "KANONIK_MODEL_ENVANTERI.yaml"


def _protocol(raw: str | None) -> ProviderProtocol:
    if raw is None:
        return ProviderProtocol.UNKNOWN
    try:
        return ProviderProtocol(raw)
    except ValueError:
        # Bilinmeyen protokol tahmin edilmez; acikca `unknown` kalir.
        return ProviderProtocol.UNKNOWN


def record_from_mapping(document: Mapping[str, Any]) -> ModelRecord:
    """Tek bir envanter girdisini kayda cevirir."""
    source = document.get("source") or {}
    return ModelRecord(
        model_id=str(document["model_id"]),
        inventory_index=int(document["inventory_index"]),
        access_name=str(document["access_name"]),
        backend_model=str(document["backend_model"]),
        provider_protocol=_protocol(document.get("provider_protocol")),
        declared_mode=document.get("declared_mode"),
        declared_category=str(document.get("declared_category") or "unknown"),
        endpoint_ref=str(document["endpoint_ref"]),
        credential_ref=str(document["credential_ref"]),
        endpoint_scope=document.get("endpoint_scope"),
        declared_parameter_profile=document.get("declared_parameter_profile"),
        reasoning_effort=document.get("reasoning_effort"),
        enabled=bool(document.get("enabled", True)),
        status=str(document.get("status") or "candidate"),
        health_state=HealthState(document.get("health_state") or "untested"),
        benchmark_state=BenchmarkState(document.get("benchmark_state") or "not-run"),
        quarantine_until=document.get("quarantine_until"),
        capabilities_declared=tuple(document.get("capabilities_declared") or ()),
        capabilities_verified=tuple(document.get("capabilities_verified") or ()),
        cost=CostEvidence.from_mapping(document.get("cost_evidence")),
        provenance=ModelProvenance(
            canonical_report=str(source.get("canonical_report") or "bilinmiyor"),
            technical_report=source.get("technical_report"),
            technical_profile_available=bool(source.get("technical_profile_available", False)),
            verification_note=str(source.get("verification_note") or ""),
        ),
    )


def load_inventory(path: Path | None = None) -> InventorySnapshot:
    """Kanonik envanter dosyasini okur ve dogrular."""
    target = path or default_inventory_file()
    if not target.is_file():
        raise ConfigurationError("Model envanteri dosyasi bulunamadi")
    try:
        document = yaml.safe_load(target.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # pragma: no cover - hata metni sanitize edilir
        raise ConfigurationError("Model envanteri okunamadi") from exc

    if not isinstance(document, dict):
        raise ValidationFailed("Model envanteri sozluk olmali")
    if document.get("schema") != INVENTORY_SCHEMA:
        raise ValidationFailed(f"Desteklenmeyen envanter semasi: {document.get('schema')!r}")
    entries = document.get("models")
    if not isinstance(entries, list) or not entries:
        raise ValidationFailed("Envanter en az bir model icermeli")

    records = tuple(record_from_mapping(entry) for entry in entries)
    assert_no_merged_identities(records)

    inventory_date = document.get("inventory_date")
    if isinstance(inventory_date, str):
        inventory_date = dt.date.fromisoformat(inventory_date)
    if not isinstance(inventory_date, dt.date):
        raise ValidationFailed("Envanter tarihi gecersiz")

    return InventorySnapshot(
        schema=str(document["schema"]), inventory_date=inventory_date, records=records
    )


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Envanter iceri aktarmanin sonucu."""

    inserted: int
    updated: int
    unchanged: int
    discrepancies: tuple[InventoryDiscrepancy, ...]
    snapshot_digest: str

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged

    @property
    def is_clean(self) -> bool:
        return not self.discrepancies

    def as_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "total": self.total,
            "discrepancies": [item.as_dict() for item in self.discrepancies],
            "snapshot_digest": self.snapshot_digest,
            "is_clean": self.is_clean,
        }


def verify_snapshot(
    snapshot: InventorySnapshot,
    *,
    expected_canonical: int = CANONICAL_MODEL_COUNT,
    expected_technical: int = TECHNICAL_PROFILE_COUNT,
) -> tuple[InventoryDiscrepancy, ...]:
    """Envanterin beyan edilen sayilarla uyumunu raporlar."""
    return check_inventory(
        snapshot, expected_canonical=expected_canonical, expected_technical=expected_technical
    )


def summarize_snapshot(snapshot: InventorySnapshot) -> dict[str, Any]:
    """Insan okunur ozet uretir."""
    modalities = {
        modality.value: len(records) for modality, records in snapshot.by_modality().items()
    }
    return {
        "canonical_count": snapshot.canonical_count,
        "technical_profile_count": snapshot.technical_profile_count,
        "missing_technical_profile": [
            record.model_id for record in snapshot.missing_technical_profile
        ],
        "duplicated_backends": {
            backend: list(ids) for backend, ids in snapshot.duplicated_backends().items()
        },
        "modalities": modalities,
        "snapshot_digest": snapshot.snapshot_digest,
    }


def assert_no_raw_values(records: Sequence[ModelRecord]) -> None:
    """Kayitlarin ham endpoint veya credential tasimadigini yeniden dogrular.

    `ModelRecord` kurulusta zaten dogrular; bu fonksiyon iceri aktarma oncesi
    ikinci savunma katmanidir.
    """
    from zekam.domain.model_inventory import validate_reference

    for record in records:
        validate_reference(record.endpoint_ref, kind="endpoint")
        validate_reference(record.credential_ref, kind="credential")
