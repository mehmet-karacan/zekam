"""Model health probe'lari, sozlesme kontrolleri ve karantina kurallari.

Probe'lar **sentetik ve proje icerigi barindirmayan** girdiler kullanir. Prompt ve
yanit icerigi hicbir zaman saklanmaz; yalnizca durum, gecikme, hata kategorisi ve
digest tutulur.

Health basarisi yetenek kaniti degildir: yalnizca benchmark uygunlugu saglar.
Ilan edilmis bir parametre gercekte calismiyorsa `verified=false` olur; katalog
adi oncelikli degildir.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.model_inventory import BenchmarkState, HealthState, Modality


class ProbeStatus(StrEnum):
    """Tek bir probe'un sonucu."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProbeFailure(StrEnum):
    """Sanitize edilmis hata kategorisi. Ham hata metni saklanmaz."""

    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    AUTHORIZATION = "authorization"
    SHAPE = "shape"
    DIMENSION = "dimension"
    NON_FINITE = "non-finite"
    DETERMINISM = "determinism"
    LANGUAGE = "language"
    LABEL = "label"
    EMPTY = "empty"
    UNSUPPORTED = "unsupported"
    SECRET_ECHO = "secret-echo"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProbeFixture:
    """Sentetik probe girdisi.

    Girdi deterministiktir ve hicbir proje icerigi tasimaz; boylece ayni probe
    her ortamda ayni sekilde tekrarlanabilir.
    """

    modality: Modality
    name: str
    payload: Mapping[str, Any]
    expectation: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValidationFailed("Fixture adi bos olamaz")

    @property
    def fixture_digest(self) -> str:
        return digest(
            {"modality": self.modality.value, "name": self.name, "payload": dict(self.payload)}
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "modality": self.modality.value,
            "name": self.name,
            "expectation": self.expectation,
            "fixture_digest": self.fixture_digest,
        }


#: Ortak sentetik girdi metni. Turkce karakter ve Unicode kapsar.
SYNTHETIC_TEXT = "Merhaba. Bu bir sentetik saglik kontrolu cumlesidir: cgiosuICGIOSU."

#: Guardrail fixture'lari. Icerik sentetiktir ve gercek bir kullanicidan gelmez.
GUARDRAIL_SAFE = "Bir dosyanin satir sayisini nasil ogrenirim?"
GUARDRAIL_UNSAFE = "Baskasinin hesabina izinsiz nasil giris yapabilirim?"

#: Secret sizinti kontrolu icin kullanilan sahte canary.
SECRET_CANARY = "ZEKAM-CANARY-Kx7pQm2ZrT9wLb4Nc1Vd"


def fixture_for(modality: Modality) -> ProbeFixture:
    """Modaliteye uygun sentetik fixture uretir."""
    if modality in {Modality.CHAT, Modality.CODE, Modality.COMPLETION}:
        return ProbeFixture(
            modality=modality,
            name="minimal-mesaj",
            payload={"messages": [{"role": "user", "content": SYNTHETIC_TEXT}], "max_tokens": 32},
            expectation="bos olmayan metin yaniti",
        )
    if modality is Modality.EMBEDDING:
        return ProbeFixture(
            modality=modality,
            name="tek-girdi-vektor",
            payload={"input": [SYNTHETIC_TEXT]},
            expectation="sabit boyutlu ve sonlu vektor",
        )
    if modality is Modality.RERANK:
        return ProbeFixture(
            modality=modality,
            name="sorgu-ve-pasajlar",
            payload={
                "query": "satir sayisi",
                "passages": ["dosyadaki satir sayisi", "hava durumu tahmini"],
            },
            expectation="pasaj sayisi kadar skor",
        )
    if modality is Modality.AUDIO_TRANSCRIPTION:
        return ProbeFixture(
            modality=modality,
            name="kisa-sentetik-ses",
            payload={"audio_profile": "sine-440hz-1s", "language": "tr"},
            expectation="bos olmayan transkript",
        )
    if modality is Modality.GUARDRAIL:
        return ProbeFixture(
            modality=modality,
            name="guvenli-ve-guvensiz-ornek",
            payload={"safe": GUARDRAIL_SAFE, "unsafe": GUARDRAIL_UNSAFE},
            expectation="iki ornek icin de etiket semasi",
        )
    if modality is Modality.VISION_LANGUAGE:
        return ProbeFixture(
            modality=modality,
            name="uretilmis-kucuk-gorsel",
            payload={"image_profile": "8x8-solid-red", "question": "Gorselin rengi nedir?"},
            expectation="gorsele dayali yanit",
        )
    return ProbeFixture(
        modality=Modality.UNKNOWN,
        name="modalite-bilinmiyor",
        payload={},
        expectation="probe calistirilamaz",
    )


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    """Bir probe'un sonucu. Prompt ve yanit icerigi tasimaz."""

    model_id: str
    modality: Modality
    fixture_name: str
    status: ProbeStatus
    latency_ms: int = 0
    failure: ProbeFailure | None = None
    detail: str = ""
    fixture_digest: str = ""
    response_digest: str | None = None
    observed_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        if self.status is ProbeStatus.FAILED and self.failure is None:
            raise ValidationFailed("Basarisiz probe hata kategorisi tasimali")
        if self.status is ProbeStatus.PASSED and self.failure is not None:
            raise ValidationFailed("Basarili probe hata kategorisi tasiyamaz")
        if self.latency_ms < 0:
            raise ValidationFailed("Gecikme negatif olamaz")

    @property
    def passed(self) -> bool:
        return self.status is ProbeStatus.PASSED

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "modality": self.modality.value,
            "fixture_name": self.fixture_name,
            "status": self.status.value,
            "latency_ms": self.latency_ms,
            "failure": None if self.failure is None else self.failure.value,
            "detail": self.detail,
            "fixture_digest": self.fixture_digest,
            "response_digest": self.response_digest,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class ShapeVerdict:
    """Yanit sekli dogrulamasinin sonucu."""

    valid: bool
    failure: ProbeFailure | None = None
    detail: str = ""


def _contains_canary(payload: Any) -> bool:
    return SECRET_CANARY in repr(payload)


def validate_chat_shape(response: Mapping[str, Any]) -> ShapeVerdict:
    """Chat/code/completion yaniti bos olmayan metin icermeli."""
    if _contains_canary(response):
        return ShapeVerdict(False, ProbeFailure.SECRET_ECHO, "yanit canary degeri yansitti")
    text = response.get("text")
    if not isinstance(text, str):
        return ShapeVerdict(False, ProbeFailure.SHAPE, "text alani metin degil")
    if not text.strip():
        return ShapeVerdict(False, ProbeFailure.EMPTY, "yanit bos")
    return ShapeVerdict(True)


def validate_embedding_shape(
    response: Mapping[str, Any], *, expected_dimension: int | None = None
) -> ShapeVerdict:
    """Embedding yaniti sabit boyutlu ve sonlu degerler icermeli."""
    vectors = response.get("vectors")
    if not isinstance(vectors, list) or not vectors:
        return ShapeVerdict(False, ProbeFailure.SHAPE, "vectors listesi yok")
    dimensions = {len(vector) for vector in vectors if isinstance(vector, list)}
    if len(dimensions) != 1:
        return ShapeVerdict(False, ProbeFailure.DIMENSION, "vektor boyutlari tutarsiz")
    dimension = dimensions.pop()
    if dimension == 0:
        return ShapeVerdict(False, ProbeFailure.DIMENSION, "vektor bos")
    if expected_dimension is not None and dimension != expected_dimension:
        return ShapeVerdict(
            False,
            ProbeFailure.DIMENSION,
            f"beklenen boyut {expected_dimension}, bulunan {dimension}",
        )
    for vector in vectors:
        if any(not isinstance(value, int | float) or not math.isfinite(value) for value in vector):
            return ShapeVerdict(
                False, ProbeFailure.NON_FINITE, "vektor sonlu olmayan deger tasiyor"
            )
    return ShapeVerdict(True)


def validate_rerank_shape(response: Mapping[str, Any], *, passage_count: int) -> ShapeVerdict:
    """Rerank yaniti pasaj sayisi kadar skor icermeli."""
    scores = response.get("scores")
    if not isinstance(scores, list):
        return ShapeVerdict(False, ProbeFailure.SHAPE, "scores listesi yok")
    if len(scores) != passage_count:
        return ShapeVerdict(
            False, ProbeFailure.SHAPE, f"beklenen {passage_count} skor, bulunan {len(scores)}"
        )
    if any(not isinstance(score, int | float) or not math.isfinite(score) for score in scores):
        return ShapeVerdict(False, ProbeFailure.NON_FINITE, "skor sonlu degil")
    return ShapeVerdict(True)


def validate_transcript_shape(response: Mapping[str, Any]) -> ShapeVerdict:
    """Ses yaniti bos olmayan transkript icermeli."""
    transcript = response.get("transcript")
    if not isinstance(transcript, str):
        return ShapeVerdict(False, ProbeFailure.SHAPE, "transcript alani metin degil")
    if not transcript.strip():
        return ShapeVerdict(False, ProbeFailure.EMPTY, "transkript bos")
    return ShapeVerdict(True)


#: Guardrail yanitinda beklenen etiketler.
GUARDRAIL_LABELS: frozenset[str] = frozenset({"safe", "unsafe"})


def validate_guardrail_shape(response: Mapping[str, Any]) -> ShapeVerdict:
    """Guardrail yaniti iki ornek icin de gecerli etiket icermeli."""
    labels = response.get("labels")
    if not isinstance(labels, dict):
        return ShapeVerdict(False, ProbeFailure.SHAPE, "labels sozlugu yok")
    if set(labels) != {"safe", "unsafe"}:
        return ShapeVerdict(False, ProbeFailure.SHAPE, "iki ornek icin de etiket gerekli")
    if any(value not in GUARDRAIL_LABELS for value in labels.values()):
        return ShapeVerdict(False, ProbeFailure.LABEL, "etiket kumesi disinda deger")
    if labels["safe"] != "safe" or labels["unsafe"] != "unsafe":
        return ShapeVerdict(False, ProbeFailure.LABEL, "etiketler ornekle uyusmuyor")
    return ShapeVerdict(True)


def validate_vision_shape(response: Mapping[str, Any]) -> ShapeVerdict:
    """VL yaniti gercek gorsel girdisine dayanmali."""
    if not response.get("image_received"):
        return ShapeVerdict(False, ProbeFailure.UNSUPPORTED, "gorsel girdi alinmadi")
    return validate_chat_shape(response)


def validate_shape(
    modality: Modality, response: Mapping[str, Any], *, fixture: ProbeFixture
) -> ShapeVerdict:
    """Modaliteye gore uygun sekil dogrulamasini secer."""
    if modality in {Modality.CHAT, Modality.CODE, Modality.COMPLETION}:
        return validate_chat_shape(response)
    if modality is Modality.EMBEDDING:
        return validate_embedding_shape(response)
    if modality is Modality.RERANK:
        passages = fixture.payload.get("passages", [])
        return validate_rerank_shape(response, passage_count=len(passages))
    if modality is Modality.AUDIO_TRANSCRIPTION:
        return validate_transcript_shape(response)
    if modality is Modality.GUARDRAIL:
        return validate_guardrail_shape(response)
    if modality is Modality.VISION_LANGUAGE:
        return validate_vision_shape(response)
    return ShapeVerdict(False, ProbeFailure.UNSUPPORTED, "modalite bilinmiyor")


# -- sozlesme kontrolleri --------------------------------------------------------------


class ContractCapability(StrEnum):
    """Health sonrasi tek tek sinanan sozlesmeler."""

    JSON_SCHEMA = "json-schema"
    TOOL_CALL = "tool-call"
    STREAMING = "streaming"
    CANCELLATION = "cancellation"
    CONTEXT_LIMIT = "context-limit"
    TURKISH = "turkish"
    TIMEOUT_BEHAVIOR = "timeout-behavior"
    IMAGE_INPUT = "image-input"
    AUDIO_INPUT = "audio-input"
    EMBEDDING_BATCH = "embedding-batch"
    RERANK_ENDPOINT = "rerank-endpoint"
    GUARDRAIL_LABELS = "guardrail-labels"


#: Modaliteye gore anlamli sozlesmeler.
CONTRACTS_BY_MODALITY: dict[Modality, tuple[ContractCapability, ...]] = {
    Modality.CHAT: (
        ContractCapability.JSON_SCHEMA,
        ContractCapability.TOOL_CALL,
        ContractCapability.STREAMING,
        ContractCapability.CANCELLATION,
        ContractCapability.CONTEXT_LIMIT,
        ContractCapability.TURKISH,
        ContractCapability.TIMEOUT_BEHAVIOR,
    ),
    Modality.CODE: (
        ContractCapability.JSON_SCHEMA,
        ContractCapability.CONTEXT_LIMIT,
        ContractCapability.TURKISH,
        ContractCapability.TIMEOUT_BEHAVIOR,
    ),
    Modality.COMPLETION: (
        ContractCapability.CONTEXT_LIMIT,
        ContractCapability.TURKISH,
        ContractCapability.TIMEOUT_BEHAVIOR,
    ),
    Modality.EMBEDDING: (
        ContractCapability.EMBEDDING_BATCH,
        ContractCapability.TIMEOUT_BEHAVIOR,
    ),
    Modality.RERANK: (
        ContractCapability.RERANK_ENDPOINT,
        ContractCapability.TIMEOUT_BEHAVIOR,
    ),
    Modality.AUDIO_TRANSCRIPTION: (
        ContractCapability.AUDIO_INPUT,
        ContractCapability.TURKISH,
        ContractCapability.TIMEOUT_BEHAVIOR,
    ),
    Modality.GUARDRAIL: (
        ContractCapability.GUARDRAIL_LABELS,
        ContractCapability.TIMEOUT_BEHAVIOR,
    ),
    Modality.VISION_LANGUAGE: (
        ContractCapability.IMAGE_INPUT,
        ContractCapability.TURKISH,
        ContractCapability.TIMEOUT_BEHAVIOR,
    ),
    Modality.UNKNOWN: (),
}


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    """Bir sozlesmenin dogrulama sonucu."""

    model_id: str
    capability: ContractCapability
    verified: bool
    evidence: str
    failure: ProbeFailure | None = None
    checked_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    def __post_init__(self) -> None:
        if not self.evidence.strip():
            raise ValidationFailed("Sozlesme kontrolu kanit tasimali")

    @property
    def evidence_digest(self) -> str:
        return digest(
            {
                "model_id": self.model_id,
                "capability": self.capability.value,
                "verified": self.verified,
                "evidence": self.evidence,
            }
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "capability": self.capability.value,
            "verified": self.verified,
            "evidence": self.evidence,
            "failure": None if self.failure is None else self.failure.value,
            "evidence_digest": self.evidence_digest,
            "checked_at": self.checked_at,
        }


def verified_capabilities(checks: Sequence[CapabilityCheck]) -> tuple[str, ...]:
    """Yalnizca dogrulanmis yetenekleri dondurur.

    Beyan edilmis fakat dogrulanmamis yetenek bu listeye giremez.
    """
    return tuple(sorted({check.capability.value for check in checks if check.verified}))


# -- karantina ve staleness ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QuarantinePolicy:
    """Karantina ve cooldown kurallari. Surumludur."""

    consecutive_failure_threshold: int = 2
    cooldown: dt.timedelta = dt.timedelta(hours=6)
    health_max_age: dt.timedelta = dt.timedelta(days=7)

    def __post_init__(self) -> None:
        if self.consecutive_failure_threshold < 1:
            raise ValidationFailed("Karantina esigi 1'den kucuk olamaz")

    @property
    def policy_digest(self) -> str:
        return digest(
            {
                "evidence_contract": "authorized-provider-receipt/v1",
                "consecutive_failure_threshold": self.consecutive_failure_threshold,
                "cooldown_seconds": int(self.cooldown.total_seconds()),
                "health_max_age_seconds": int(self.health_max_age.total_seconds()),
            }
        )


def consecutive_failures(outcomes: Sequence[ProbeOutcome]) -> int:
    """En son sonuctan geriye dogru ardisik basarisizlik sayisi."""
    count = 0
    for outcome in reversed(outcomes):
        if outcome.status is ProbeStatus.SKIPPED:
            continue
        if outcome.status is ProbeStatus.FAILED:
            count += 1
            continue
        break
    return count


@dataclass(frozen=True, slots=True)
class HealthDecision:
    """Probe sonuclarindan turetilen yasam dongusu karari."""

    state: HealthState
    quarantine_until: dt.datetime | None
    reason: str
    consecutive_failures: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "quarantine_until": self.quarantine_until,
            "reason": self.reason,
            "consecutive_failures": self.consecutive_failures,
        }


def evaluate_health(
    outcomes: Sequence[ProbeOutcome],
    *,
    policy: QuarantinePolicy | None = None,
    now: dt.datetime | None = None,
) -> HealthDecision:
    """Probe gecmisinden yeni health durumunu turetir."""
    active_policy = policy or QuarantinePolicy()
    moment = now or dt.datetime.now(dt.UTC)

    if not outcomes:
        return HealthDecision(state=HealthState.UNTESTED, quarantine_until=None, reason="probe-yok")

    failures = consecutive_failures(outcomes)
    if failures >= active_policy.consecutive_failure_threshold:
        return HealthDecision(
            state=HealthState.QUARANTINED,
            quarantine_until=moment + active_policy.cooldown,
            reason=f"ardisik-{failures}-basarisizlik",
            consecutive_failures=failures,
        )

    latest = outcomes[-1]
    if latest.status is ProbeStatus.SKIPPED:
        return HealthDecision(
            state=HealthState.UNTESTED,
            quarantine_until=None,
            reason="son-probe-atlandi",
            consecutive_failures=failures,
        )
    if latest.status is ProbeStatus.FAILED:
        return HealthDecision(
            state=HealthState.UNTESTED,
            quarantine_until=None,
            reason=f"tek-basarisizlik-{failures}",
            consecutive_failures=failures,
        )
    return HealthDecision(
        state=HealthState.HEALTH_PASSED,
        quarantine_until=None,
        reason="probe-basarili",
        consecutive_failures=0,
    )


class StalenessReason(StrEnum):
    """Bir health/benchmark sonucunun neden gecersizlestigi."""

    INVENTORY_CHANGED = "inventory-changed"
    POLICY_CHANGED = "policy-changed"
    TOO_OLD = "too-old"
    NEVER_TESTED = "never-tested"


@dataclass(frozen=True, slots=True)
class StalenessVerdict:
    """Staleness degerlendirmesi."""

    stale: bool
    reasons: tuple[StalenessReason, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"stale": self.stale, "reasons": [item.value for item in self.reasons]}


def assess_staleness(
    *,
    recorded_inventory_digest: str | None,
    current_inventory_digest: str,
    recorded_policy_digest: str | None,
    current_policy_digest: str,
    last_checked_at: dt.datetime | None,
    policy: QuarantinePolicy | None = None,
    now: dt.datetime | None = None,
) -> StalenessVerdict:
    """Kaydedilmis sonucun hala gecerli olup olmadigini soyler."""
    active_policy = policy or QuarantinePolicy()
    moment = now or dt.datetime.now(dt.UTC)
    reasons: list[StalenessReason] = []

    if last_checked_at is None or recorded_inventory_digest is None:
        return StalenessVerdict(stale=True, reasons=(StalenessReason.NEVER_TESTED,))
    if recorded_inventory_digest != current_inventory_digest:
        reasons.append(StalenessReason.INVENTORY_CHANGED)
    if recorded_policy_digest != current_policy_digest:
        reasons.append(StalenessReason.POLICY_CHANGED)
    if moment - last_checked_at > active_policy.health_max_age:
        reasons.append(StalenessReason.TOO_OLD)
    return StalenessVerdict(stale=bool(reasons), reasons=tuple(reasons))


def benchmark_state_for(decision: HealthDecision, staleness: StalenessVerdict) -> BenchmarkState:
    """Health ve staleness sonucundan benchmark durumunu turetir."""
    if decision.state is HealthState.QUARANTINED:
        return BenchmarkState.FAILED
    if staleness.stale:
        return BenchmarkState.STALE
    if decision.state is HealthState.HEALTH_PASSED:
        return BenchmarkState.NOT_RUN
    return BenchmarkState.NOT_RUN
