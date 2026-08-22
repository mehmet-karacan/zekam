"""Model envanteri alan modeli.

Kurallar (`modeller/MODEL_ENVANTER_MUTABAKAT.md`):

- Her Model ID bagimsiz bir yonetim nesnesidir. Ayni backend/model adi farkli
  Model ID veya protokolle geliyorsa **birlestirilmez**.
- Aktif envanter kaydi ham endpoint adresi veya credential degeri tasimaz;
  yalnizca `endpoint_ref` ve `credential_ref` mantiksal referanslarini tasir.
- Health basarisi yetenek kaniti degildir; yalnizca benchmark uygunlugu saglar.
- Bilinmeyen alan tahmin edilmez; `None` kalir ve gorunur provenance tasir.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

#: Envanterde beklenen kanonik kayit sayisi.
CANONICAL_MODEL_COUNT = 20

#: Teknik profili bulunan kayit sayisi. Fark gorunur provenance olarak korunur.
TECHNICAL_PROFILE_COUNT = 19

#: `model-endpoint:<kimlik>` veya `model-credential:<kimlik>` bicimi.
_REFERENCE_PATTERN = re.compile(r"^model-(endpoint|credential):[A-Za-z0-9._-]{1,128}$")

#: Referans alaninda gorunmesi kesinlikle yasak desenler.
_FORBIDDEN_IN_REFERENCE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("url", re.compile(r"://")),
    ("host-port", re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\b")),
    ("api-key-prefix", re.compile(r"(?i)\b(sk|pk|api)[-_][A-Za-z0-9]{8,}")),
    ("aws-key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("long-opaque-token", re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")),
)


class ProviderProtocol(StrEnum):
    """Saglayici konusma protokolu."""

    OPENAI = "openai"
    HOSTED_VLLM = "hosted_vllm"
    ANTHROPIC = "anthropic"
    CUSTOM_OPENAI = "custom_openai"
    UNKNOWN = "unknown"


class Modality(StrEnum):
    """Modelin calisma bicimi. Health probe'u bu belirler."""

    CHAT = "chat"
    CODE = "code"
    COMPLETION = "completion"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    AUDIO_TRANSCRIPTION = "audio_transcription"
    GUARDRAIL = "guardrail"
    VISION_LANGUAGE = "vision_language"
    UNKNOWN = "unknown"


#: Beyan edilen kategoriden turetilen modalite. Eslesmeyen kategori `unknown` kalir.
CATEGORY_TO_MODALITY: dict[str, Modality] = {
    "chat": Modality.CHAT,
    "chat_generation": Modality.CHAT,
    "code_chat": Modality.CHAT,
    "reasoning_generation": Modality.CHAT,
    "text_generation": Modality.CHAT,
    "code_generation": Modality.CODE,
    "completion": Modality.COMPLETION,
    "embedding": Modality.EMBEDDING,
    "rerank": Modality.RERANK,
    "audio_transcription": Modality.AUDIO_TRANSCRIPTION,
    "guardrail": Modality.GUARDRAIL,
    "multimodal_generation": Modality.VISION_LANGUAGE,
}


class HealthState(StrEnum):
    """Model yasam dongusu.

    `inventory state model assignment degildir`: bu durum yalnizca modelin
    hangi kapilardan gectigini soyler, hangi ise atanacagini soylemez.
    """

    UNTESTED = "untested"
    HEALTH_PASSED = "health-passed"
    CONTRACT_PASSED = "contract-passed"
    BENCHMARK_ELIGIBLE = "benchmark-eligible"
    PROJECT_QUALIFIED = "project-qualified"
    ACTIVE_CANDIDATE = "active-candidate"
    QUARANTINED = "quarantined"
    COOLDOWN = "cooldown"


#: Ilerleme sirasi; her adim bir onceki adimin kanitini ister.
HEALTH_PROGRESSION: tuple[HealthState, ...] = (
    HealthState.UNTESTED,
    HealthState.HEALTH_PASSED,
    HealthState.CONTRACT_PASSED,
    HealthState.BENCHMARK_ELIGIBLE,
    HealthState.PROJECT_QUALIFIED,
    HealthState.ACTIVE_CANDIDATE,
)


class BenchmarkState(StrEnum):
    """Benchmark durumu."""

    NOT_RUN = "not-run"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    STALE = "stale"


def validate_reference(value: str, *, kind: str) -> str:
    """Endpoint/credential referansinin ham deger tasimadigini dogrular."""
    candidate = value.strip()
    if not candidate:
        raise ValidationFailed(f"{kind} referansi bos olamaz")
    for label, pattern in _FORBIDDEN_IN_REFERENCE:
        if pattern.search(candidate):
            raise PolicyViolation(
                f"{kind} referansi ham deger tasiyamaz ({label}); mantiksal referans kullanin"
            )
    if not _REFERENCE_PATTERN.match(candidate):
        raise ValidationFailed(f"{kind} referansi `model-endpoint:<kimlik>` bicimine uymali")
    return candidate


@dataclass(frozen=True, slots=True)
class CostEvidence:
    """Maliyet kaniti. Tahmin degil, gozlem tasir."""

    ui_input_usd_per_million: float | None = None
    ui_output_usd_per_million: float | None = None
    raw_input_cost_per_token: float | None = None
    raw_output_cost_per_token: float | None = None

    @property
    def has_evidence(self) -> bool:
        return any(
            value is not None
            for value in (
                self.ui_input_usd_per_million,
                self.ui_output_usd_per_million,
                self.raw_input_cost_per_token,
                self.raw_output_cost_per_token,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ui_input_usd_per_million_token_equivalent": self.ui_input_usd_per_million,
            "ui_output_usd_per_million_token_equivalent": self.ui_output_usd_per_million,
            "raw_input_cost_per_token": self.raw_input_cost_per_token,
            "raw_output_cost_per_token": self.raw_output_cost_per_token,
        }

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any] | None) -> CostEvidence:
        source = document or {}
        return cls(
            ui_input_usd_per_million=source.get("ui_input_usd_per_million_token_equivalent"),
            ui_output_usd_per_million=source.get("ui_output_usd_per_million_token_equivalent"),
            raw_input_cost_per_token=source.get("raw_input_cost_per_token"),
            raw_output_cost_per_token=source.get("raw_output_cost_per_token"),
        )


@dataclass(frozen=True, slots=True)
class ModelProvenance:
    """Kaydin nereden geldigi ve neyin dogrulanmadigi."""

    canonical_report: str
    technical_report: str | None = None
    technical_profile_available: bool = False
    verification_note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "canonical_report": self.canonical_report,
            "technical_report": self.technical_report,
            "technical_profile_available": self.technical_profile_available,
            "verification_note": self.verification_note,
        }


@dataclass(frozen=True, slots=True)
class ModelRecord:
    """Tek bir Model ID kaydi.

    Bu nesne ham endpoint veya credential tasimaz; tasima girisimi kurulusta
    reddedilir.
    """

    model_id: str
    inventory_index: int
    access_name: str
    backend_model: str
    provider_protocol: ProviderProtocol
    declared_category: str
    endpoint_ref: str
    credential_ref: str
    provenance: ModelProvenance
    declared_mode: str | None = None
    endpoint_scope: str | None = None
    declared_parameter_profile: str | None = None
    reasoning_effort: str | None = None
    enabled: bool = True
    status: str = "candidate"
    health_state: HealthState = HealthState.UNTESTED
    benchmark_state: BenchmarkState = BenchmarkState.NOT_RUN
    quarantine_until: dt.datetime | None = None
    capabilities_declared: tuple[str, ...] = ()
    capabilities_verified: tuple[str, ...] = ()
    cost: CostEvidence = field(default_factory=CostEvidence)

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValidationFailed("Model ID bos olamaz")
        if self.inventory_index < 1:
            raise ValidationFailed("Envanter sirasi 1'den kucuk olamaz")
        if not self.access_name.strip():
            raise ValidationFailed("Erisim adi bos olamaz")
        validate_reference(self.endpoint_ref, kind="endpoint")
        validate_reference(self.credential_ref, kind="credential")

    @property
    def mode_modality(self) -> Modality | None:
        """Beyan edilen calisma modundan turetilen modalite."""
        if not self.declared_mode:
            return None
        return CATEGORY_TO_MODALITY.get(self.declared_mode)

    @property
    def category_modality(self) -> Modality:
        """Beyan edilen kategoriden turetilen modalite."""
        return CATEGORY_TO_MODALITY.get(self.declared_category, Modality.UNKNOWN)

    @property
    def modality(self) -> Modality:
        """Capability kategorisi.

        ``declared_mode`` invocation seklidir; modelin neyi yapabildigini
        ``declared_category`` belirler. Catismalar yine gorunur kalir.
        """
        category = self.category_modality
        return category if category is not Modality.UNKNOWN else self.mode_modality or category

    @property
    def invocation_modality(self) -> Modality:
        """Provider cagrisi icin kullanilacak transport modu."""

        return self.mode_modality or self.category_modality

    @property
    def modality_conflict(self) -> tuple[Modality, Modality] | None:
        """Mod ve kategori farkli modalite soyluyorsa ciftini dondurur."""
        from_mode = self.mode_modality
        if from_mode is None or from_mode is self.category_modality:
            return None
        return (from_mode, self.category_modality)

    @property
    def has_technical_profile(self) -> bool:
        return self.provenance.technical_profile_available

    def is_quarantined(self, *, now: dt.datetime | None = None) -> bool:
        moment = now or dt.datetime.now(dt.UTC)
        if self.health_state is HealthState.QUARANTINED:
            return True
        return self.quarantine_until is not None and self.quarantine_until > moment

    def is_benchmark_eligible(self, *, now: dt.datetime | None = None) -> bool:
        """Health basarisi tek basina yetenek kaniti degildir; yalnizca uygunluk saglar."""
        if not self.enabled or self.is_quarantined(now=now):
            return False
        return self.health_state in {
            HealthState.HEALTH_PASSED,
            HealthState.CONTRACT_PASSED,
            HealthState.BENCHMARK_ELIGIBLE,
            HealthState.PROJECT_QUALIFIED,
            HealthState.ACTIVE_CANDIDATE,
        }

    def declares(self, capability: str) -> bool:
        return capability in self.capabilities_declared

    def verified(self, capability: str) -> bool:
        """Yalnizca dogrulanmis yetenek gecerlidir; katalog adi oncelikli degildir."""
        return capability in self.capabilities_verified

    def body(self) -> dict[str, Any]:
        """Digest hesaplanan govde. Ham deger icermez."""
        return {
            "model_id": self.model_id,
            "inventory_index": self.inventory_index,
            "access_name": self.access_name,
            "backend_model": self.backend_model,
            "provider_protocol": self.provider_protocol.value,
            "declared_mode": self.declared_mode,
            "declared_category": self.declared_category,
            "endpoint_ref": self.endpoint_ref,
            "credential_ref": self.credential_ref,
            "endpoint_scope": self.endpoint_scope,
            "declared_parameter_profile": self.declared_parameter_profile,
            "reasoning_effort": self.reasoning_effort,
            "enabled": self.enabled,
            "status": self.status,
            "capabilities_declared": sorted(self.capabilities_declared),
            "cost": self.cost.as_dict(),
            "provenance": self.provenance.as_dict(),
        }

    @property
    def inventory_digest(self) -> str:
        return digest(self.body())

    def as_dict(self) -> dict[str, Any]:
        conflict = self.modality_conflict
        return self.body() | {
            "modality": self.modality.value,
            "invocation_modality": self.invocation_modality.value,
            "modality_conflict": (
                None if conflict is None else [conflict[0].value, conflict[1].value]
            ),
            "health_state": self.health_state.value,
            "benchmark_state": self.benchmark_state.value,
            "capabilities_verified": sorted(self.capabilities_verified),
            "quarantine_until": self.quarantine_until,
            "inventory_digest": self.inventory_digest,
        }


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    """Yuklenmis envanterin tamami."""

    schema: str
    inventory_date: dt.date
    records: tuple[ModelRecord, ...]

    def __post_init__(self) -> None:
        identifiers = [record.model_id for record in self.records]
        if len(identifiers) != len(set(identifiers)):
            raise ValidationFailed("Model ID'ler tekil olmali")

    @property
    def canonical_count(self) -> int:
        return len(self.records)

    @property
    def technical_profile_count(self) -> int:
        return sum(1 for record in self.records if record.has_technical_profile)

    @property
    def missing_technical_profile(self) -> tuple[ModelRecord, ...]:
        """Teknik profili olmayan kayitlar. Fark gizlenmez, gorunur kalir."""
        return tuple(record for record in self.records if not record.has_technical_profile)

    def by_id(self, model_id: str) -> ModelRecord | None:
        return next((record for record in self.records if record.model_id == model_id), None)

    def duplicated_backends(self) -> dict[str, tuple[str, ...]]:
        """Ayni backend adini paylasan farkli Model ID'ler.

        Bunlar **birlestirilmez**; ayri yonetim nesnesi olarak kalirlar.
        """
        grouped: dict[str, list[str]] = {}
        for record in self.records:
            grouped.setdefault(record.backend_model, []).append(record.model_id)
        return {backend: tuple(sorted(ids)) for backend, ids in grouped.items() if len(ids) > 1}

    def by_modality(self) -> dict[Modality, tuple[ModelRecord, ...]]:
        grouped: dict[Modality, list[ModelRecord]] = {}
        for record in self.records:
            grouped.setdefault(record.modality, []).append(record)
        return {
            modality: tuple(sorted(items, key=lambda item: item.inventory_index))
            for modality, items in sorted(grouped.items(), key=lambda pair: pair[0].value)
        }

    @property
    def snapshot_digest(self) -> str:
        return digest([record.body() for record in self.records])

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "inventory_date": self.inventory_date,
            "canonical_count": self.canonical_count,
            "technical_profile_count": self.technical_profile_count,
            "missing_technical_profile": [
                record.model_id for record in self.missing_technical_profile
            ],
            "duplicated_backends": {
                backend: list(ids) for backend, ids in self.duplicated_backends().items()
            },
            "snapshot_digest": self.snapshot_digest,
        }


@dataclass(frozen=True, slots=True)
class InventoryDiscrepancy:
    """Beklenen ve gozlemlenen envanter arasindaki fark."""

    kind: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "detail": self.detail}


def check_inventory(
    snapshot: InventorySnapshot,
    *,
    expected_canonical: int = CANONICAL_MODEL_COUNT,
    expected_technical: int = TECHNICAL_PROFILE_COUNT,
) -> tuple[InventoryDiscrepancy, ...]:
    """Envanterin beyan edilen sayilara uydugunu dogrular."""
    findings: list[InventoryDiscrepancy] = []
    if snapshot.canonical_count != expected_canonical:
        findings.append(
            InventoryDiscrepancy(
                kind="canonical-count-mismatch",
                detail=f"beklenen {expected_canonical}, bulunan {snapshot.canonical_count}",
            )
        )
    if snapshot.technical_profile_count != expected_technical:
        findings.append(
            InventoryDiscrepancy(
                kind="technical-profile-count-mismatch",
                detail=(
                    f"beklenen {expected_technical}, bulunan {snapshot.technical_profile_count}"
                ),
            )
        )
    for record in snapshot.missing_technical_profile:
        if not record.provenance.verification_note.strip():
            findings.append(
                InventoryDiscrepancy(
                    kind="missing-verification-note",
                    detail=f"{record.model_id} teknik profili yok fakat aciklama tasimiyor",
                )
            )
    for record in snapshot.records:
        conflict = record.modality_conflict
        if conflict is not None:
            findings.append(
                InventoryDiscrepancy(
                    kind="modality-conflict",
                    detail=(
                        f"{record.access_name}: calisma modu {conflict[0].value},"
                        f" kategori {conflict[1].value}; probe modu esas alir"
                    ),
                )
            )
    return tuple(findings)


def assert_no_merged_identities(records: Sequence[ModelRecord]) -> None:
    """Ayni Model ID'nin iki kez gorunmesini reddeder."""
    seen: set[str] = set()
    for record in records:
        if record.model_id in seen:
            raise ValidationFailed(
                f"Model ID birlestirilemez veya tekrarlanamaz: {record.model_id}"
            )
        seen.add(record.model_id)
