"""Model benchmark, route karari, kota ve bounded deliberation sozlesmeleri.

Bu modul provider adapter'i icermez. Fixture ve trial kayitlari ham prompt/yanit
yerine surum, metrik ve digest tasir; route karari ise authority uretmeyen,
yeniden hesaplanabilir bir kanit kaydidir.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import statistics
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any

from zekam.domain.canonical import digest, digest_of_bytes, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

MINIMUM_REPETITIONS = 5
MAX_DELIBERATION_ROUNDS = 2
MAX_DELIBERATION_SECONDS = 600
QUOTA_MAX_AGE = dt.timedelta(minutes=15)


def benchmark_effect_digest(plan_digest: str, fixture_digest: str, repetition: int) -> str:
    parse_digest(plan_digest)
    parse_digest(fixture_digest)
    if repetition < 1:
        raise ValidationFailed("Benchmark repetition pozitif olmali")
    return digest_of_bytes(f"{plan_digest}:{fixture_digest}:{repetition}".encode())


def benchmark_verifier_effect_digest(
    plan_digest: str,
    fixture_digest: str,
    repetition: int,
    verifier_model_id: str,
    tested_response_digest: str,
) -> str:
    parse_digest(plan_digest)
    parse_digest(fixture_digest)
    parse_digest(tested_response_digest)
    if repetition < 1 or not verifier_model_id.strip():
        raise ValidationFailed("Verifier effect kimligi gecersiz")
    return digest_of_bytes(
        (
            f"{plan_digest}:{fixture_digest}:{repetition}:verifier:"
            f"{verifier_model_id}:{tested_response_digest}"
        ).encode()
    )


class SuiteKind(StrEnum):
    GENERAL = "general"
    PROJECT = "project"


class BenchmarkTaskFamily(StrEnum):
    SQL_PLSQL = "sql-plsql"
    CODE_REPAIR = "code-repair"
    CODE_REVIEW = "code-review"
    ARCHITECTURE = "architecture"
    RAG_RETRIEVAL = "rag-retrieval"
    TOOL_USE = "tool-use"
    AGENTIC_WORKFLOW = "agentic-workflow"
    LONG_CONTEXT = "long-context"
    DOCUMENT_ANALYSIS = "document-analysis"
    STRUCTURED_OUTPUT = "structured-output"
    SAFETY_POLICY = "safety-policy"
    EMBEDDING_RETRIEVAL = "embedding-retrieval"
    RERANKING = "reranking"
    CREATIVE_TOURNAMENT = "creative-tournament"


BENCHMARK_TASK_FAMILIES: tuple[BenchmarkTaskFamily, ...] = tuple(BenchmarkTaskFamily)
REQUIRED_SCORE_DIMENSIONS: tuple[str, ...] = (
    "correctness",
    "evidence-citation",
    "structured-format",
    "safety",
    "reliability",
    "latency",
    "token-efficiency",
    "tool-correctness",
    "recovery",
    "human-correction",
)


class ExecutionEligibility(StrEnum):
    LOCAL_ONLY = "local-only"
    REMOTE_ALLOWED = "remote-allowed"


class TrialStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    UNSAFE = "unsafe"
    TIMEOUT = "timeout"


class RuntimeOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CORRECTED = "corrected"


class QuotaTrust(StrEnum):
    TRUSTED = "trusted"
    UNKNOWN = "unknown"


class QuotaPool(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    LOCAL = "local"


class CandidateGate(StrEnum):
    ENABLED = "enabled"
    HEALTH = "health-current-passed"
    SUPPORT = "workload-client-modality-support"
    PROJECT_BENCHMARK = "project-benchmark-current-passed"
    SECURITY = "data-locality-security"
    REQUIREMENTS = "context-tool-structured-output"
    VERIFIER_EXCLUSION = "independent-verifier-exclusion"
    BUDGET = "latency-money-token-budget"
    QUOTA = "quota-pool"


HARD_GATE_ORDER: tuple[CandidateGate, ...] = tuple(CandidateGate)


@dataclass(frozen=True, slots=True)
class BenchmarkFixture:
    """Secret-free benchmark case metadata.

    Fixture payload'i ayri immutable artifact'tir; kayit yalnizca content ve
    expected-schema digest'lerini tasir. Boylece registry log/prompt sizintisi
    yaratmaz.
    """

    case_id: str
    version: int
    workload: str
    modality: str
    fixture_source: str
    execution_eligibility: ExecutionEligibility
    content_digest: str
    expected_schema_digest: str
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if any(type(value) is not str for value in (self.case_id, self.workload, self.modality)):
            raise ValidationFailed("Fixture kimligi, workload ve modality metin olmali")
        if not self.case_id.strip() or not self.workload.strip() or not self.modality.strip():
            raise ValidationFailed("Fixture kimligi, workload ve modality bos olamaz")
        if type(self.version) is not int or self.version < 1:
            raise ValidationFailed("Fixture surumu pozitif olmali")
        if type(self.execution_eligibility) is not ExecutionEligibility:
            raise ValidationFailed("Fixture execution eligibility exact enum olmali")
        if type(self.tags) is not tuple or any(type(item) is not str for item in self.tags):
            raise ValidationFailed("Fixture tags metin tuple olmali")
        parse_digest(self.content_digest)
        parse_digest(self.expected_schema_digest)
        source = self.fixture_source.strip()
        source_path = PurePosixPath(source)
        windows_absolute = re.match(r"^[A-Za-z]:[\\/]", source) is not None
        if (
            not source
            or source_path.is_absolute()
            or windows_absolute
            or "\\" in source
            or ".." in source_path.parts
            or source_path.as_posix() != source
        ):
            raise PolicyViolation("Fixture source logical relative POSIX path olmali")
        metadata = (self.case_id, self.workload, self.modality, source, *self.tags)
        forbidden = ("://", "credential", "bearer ", "sk-", "api_key")
        secret_pattern = re.compile(
            r"(?:\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|[A-Za-z0-9+/]{40,}={0,2})"
        )
        if any(token in value.lower() for value in metadata for token in forbidden) or any(
            secret_pattern.search(value) for value in metadata
        ):
            raise PolicyViolation("Fixture metadata endpoint, secret veya credential tasiyamaz")

    @property
    def fixture_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "version": self.version,
            "workload": self.workload,
            "modality": self.modality,
            "fixture_source": self.fixture_source,
            "execution_eligibility": self.execution_eligibility.value,
            "content_digest": self.content_digest,
            "expected_schema_digest": self.expected_schema_digest,
            "tags": sorted(self.tags),
        }


@dataclass(frozen=True, slots=True)
class FixtureRegistry:
    schema_version: int
    fixtures: tuple[BenchmarkFixture, ...]

    def __post_init__(self) -> None:
        if self.schema_version < 1:
            raise ValidationFailed("Registry schema surumu pozitif olmali")
        identities = [(item.case_id, item.version) for item in self.fixtures]
        if len(identities) != len(set(identities)):
            raise ValidationFailed("Fixture case_id ve version birlikte tekil olmali")
        latest: dict[str, int] = {}
        for item in self.fixtures:
            previous = latest.get(item.case_id, 0)
            if item.version <= previous:
                raise ValidationFailed("Fixture surumleri kayit sirasinda artmali")
            latest[item.case_id] = item.version

    @property
    def registry_digest(self) -> str:
        return digest(
            {
                "schema_version": self.schema_version,
                "fixtures": [item.as_dict() for item in self.fixtures],
            }
        )

    def eligible(self, *, remote: bool) -> tuple[BenchmarkFixture, ...]:
        return tuple(
            item
            for item in self.fixtures
            if not remote or item.execution_eligibility is ExecutionEligibility.REMOTE_ALLOWED
        )


@dataclass(frozen=True, slots=True)
class BenchmarkSuite:
    suite_id: str
    version: int
    kind: SuiteKind
    fixture_digests: tuple[str, ...]
    capability_profile_digest: str | None = None
    project_id: str | None = None

    def __post_init__(self) -> None:
        if (
            type(self.suite_id) is not str
            or not self.suite_id.strip()
            or type(self.version) is not int
            or self.version < 1
            or type(self.kind) is not SuiteKind
            or type(self.fixture_digests) is not tuple
            or not self.fixture_digests
        ):
            raise ValidationFailed("Suite kimligi, surumu ve fixture'lari zorunludur")
        for value in self.fixture_digests:
            parse_digest(value)
        if self.kind is SuiteKind.PROJECT:
            if not self.project_id or not self.capability_profile_digest:
                raise ValidationFailed("Project suite proje ve capability digest ister")
            parse_digest(self.capability_profile_digest)
        elif self.project_id is not None or self.capability_profile_digest is not None:
            raise ValidationFailed("General suite project binding tasiyamaz")

    @property
    def suite_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "suite_id": self.suite_id,
            "version": self.version,
            "kind": self.kind.value,
            "fixture_digests": list(self.fixture_digests),
            "project_id": self.project_id,
            "capability_profile_digest": self.capability_profile_digest,
        }


def build_project_suite(
    *, project_id: str, capability_profile_digest: str, registry: FixtureRegistry, version: int = 1
) -> BenchmarkSuite:
    """Capability profile'a gore workload case'lerini baglayan micro suite."""
    parse_digest(capability_profile_digest)
    selected = tuple(
        item.fixture_digest
        for item in registry.fixtures
        if "project" in item.tags or item.workload in {"code", "analysis", "verification"}
    )
    if not selected:
        raise ValidationFailed("Capability profile icin uygun project fixture bulunamadi")
    return BenchmarkSuite(
        suite_id=f"project:{project_id}",
        version=version,
        kind=SuiteKind.PROJECT,
        fixture_digests=selected,
        project_id=project_id,
        capability_profile_digest=capability_profile_digest,
    )


@dataclass(frozen=True, slots=True)
class BenchmarkPlan:
    model_id: str
    suite_digest: str
    inventory_digest: str
    policy_digest: str
    fixture_registry_digest: str
    repetitions: int = MINIMUM_REPETITIONS
    remote_execution: bool = False

    def __post_init__(self) -> None:
        if type(self.model_id) is not str or not self.model_id.strip():
            raise ValidationFailed("Benchmark model kimligi bos olamaz")
        for value in (
            self.suite_digest,
            self.inventory_digest,
            self.policy_digest,
            self.fixture_registry_digest,
        ):
            parse_digest(value)
        if type(self.repetitions) is not int or self.repetitions < MINIMUM_REPETITIONS:
            raise PolicyViolation("Benchmark en az bes repetition ister")
        if type(self.remote_execution) is not bool:
            raise ValidationFailed("Benchmark remote execution bool olmali")

    @property
    def plan_digest(self) -> str:
        return digest(
            {
                "model_id": self.model_id,
                "suite_digest": self.suite_digest,
                "inventory_digest": self.inventory_digest,
                "policy_digest": self.policy_digest,
                "fixture_registry_digest": self.fixture_registry_digest,
                "repetitions": self.repetitions,
                "remote_execution": self.remote_execution,
            }
        )


@dataclass(frozen=True, slots=True)
class TrialResult:
    fixture_digest: str
    repetition: int
    status: TrialStatus
    parse_ok: bool
    format_ok: bool
    evidence_ok: bool
    verifier_approved: bool
    quality: float
    reliability: float
    latency_ms: int
    input_tokens: int
    output_tokens: int
    retry_count: int
    human_corrections: int
    estimated_cost: float
    actual_cost: float | None
    response_digest: str
    evidence_digest: str
    failure_category: str | None = None
    tool_correctness: float = 0.0
    recovery: float = 0.0

    def __post_init__(self) -> None:
        if type(self.status) is not TrialStatus:
            raise ValidationFailed("Trial status exact enum olmali")
        if type(self.repetition) is not int or self.repetition < 1:
            raise ValidationFailed("Repetition pozitif olmali")
        parse_digest(self.fixture_digest)
        if any(
            type(value) is not bool
            for value in (
                self.parse_ok,
                self.format_ok,
                self.evidence_ok,
                self.verifier_approved,
            )
        ):
            raise ValidationFailed("Trial karar alanlari bool olmali")
        if type(self.quality) is not float or type(self.reliability) is not float:
            raise ValidationFailed("Quality ve reliability exact float olmali")
        if not 0 <= self.quality <= 1 or not 0 <= self.reliability <= 1:
            raise ValidationFailed("Quality ve reliability 0..1 araliginda olmali")
        if any(
            type(value) is not float or not math.isfinite(value) or not 0 <= value <= 1
            for value in (self.tool_correctness, self.recovery)
        ):
            raise ValidationFailed("Tool correctness ve recovery 0..1 exact float olmali")
        numeric = (
            self.latency_ms,
            self.input_tokens,
            self.output_tokens,
            self.retry_count,
            self.human_corrections,
        )
        if any(type(value) is not int or value < 0 for value in numeric):
            raise ValidationFailed("Trial sayac alanlari negatif olmayan integer olmali")
        if type(self.estimated_cost) is not float or not math.isfinite(self.estimated_cost):
            raise ValidationFailed("Estimated cost exact finite float olmali")
        if self.actual_cost is not None and (
            type(self.actual_cost) is not float or not math.isfinite(self.actual_cost)
        ):
            raise ValidationFailed("Actual cost exact finite float veya None olmali")
        if self.estimated_cost < 0 or (self.actual_cost is not None and self.actual_cost < 0):
            raise ValidationFailed("Trial cost metrikleri negatif olamaz")
        parse_digest(self.response_digest)
        parse_digest(self.evidence_digest)
        if self.failure_category is not None and type(self.failure_category) is not str:
            raise ValidationFailed("Failure category metin olmali")
        if self.status is TrialStatus.PASSED and self.failure_category is not None:
            raise ValidationFailed("Basarili trial failure category tasiyamaz")
        if self.status is not TrialStatus.PASSED and not self.failure_category:
            raise ValidationFailed("Basarisiz trial failure category ister")

    @property
    def cost(self) -> float:
        return self.actual_cost if self.actual_cost is not None else self.estimated_cost

    @property
    def valid(self) -> bool:
        return (
            self.status is TrialStatus.PASSED
            and self.parse_ok
            and self.format_ok
            and self.evidence_ok
            and self.verifier_approved
        )

    @property
    def unsafe(self) -> bool:
        return self.status is TrialStatus.UNSAFE or (
            self.status is TrialStatus.PASSED and not self.valid
        )


@dataclass(frozen=True, slots=True)
class VerifierVerdict:
    tested_model_id: str
    verifier_model_id: str
    execution_identity: str
    tested_response_digest: str
    approved: bool
    evidence_digest: str

    def __post_init__(self) -> None:
        if type(self.approved) is not bool:
            raise ValidationFailed("Verifier approved exact bool olmali")
        if not self.tested_model_id.strip() or not self.verifier_model_id.strip():
            raise ValidationFailed("Tested ve verifier model kimligi zorunludur")
        if self.tested_model_id == self.verifier_model_id:
            raise PolicyViolation("Model kendi sonucunu onaylayamaz")
        if not self.execution_identity.strip():
            raise ValidationFailed("Verifier execution identity zorunludur")
        parse_digest(self.tested_response_digest)
        parse_digest(self.evidence_digest)


@dataclass(frozen=True, slots=True)
class MetricAggregate:
    mean: float
    median: float
    p95: float
    variance: float

    def as_dict(self) -> dict[str, float]:
        return {
            "mean": self.mean,
            "median": self.median,
            "p95": self.p95,
            "variance": self.variance,
        }


def _aggregate(values: list[float]) -> MetricAggregate:
    if not values:
        raise ValidationFailed("Bos trial kumesi aggregate edilemez")
    ordered = sorted(values)
    rank = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return MetricAggregate(
        mean=statistics.fmean(ordered),
        median=statistics.median(ordered),
        p95=ordered[rank],
        variance=statistics.pvariance(ordered),
    )


@dataclass(frozen=True, slots=True)
class BenchmarkAggregate:
    approved: bool
    unsafe: bool
    verifier_model_id: str
    verifier_execution_identity: str
    verifier_provenance_digest: str
    tested_model_id: str
    quality: MetricAggregate
    reliability: MetricAggregate
    latency_ms: MetricAggregate
    cost: MetricAggregate
    token_count: MetricAggregate
    correctness: MetricAggregate
    evidence_citation: MetricAggregate
    structured_format: MetricAggregate
    safety: MetricAggregate
    token_efficiency: MetricAggregate
    tool_correctness: MetricAggregate
    recovery: MetricAggregate
    human_correction: MetricAggregate
    pass_rate: float
    confidence_interval_low: float
    confidence_interval_high: float
    evidence_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "unsafe": self.unsafe,
            "tested_model_id": self.tested_model_id,
            "verifier_model_id": self.verifier_model_id,
            "verifier_execution_identity": self.verifier_execution_identity,
            "verifier_provenance_digest": self.verifier_provenance_digest,
            "quality": self.quality.as_dict(),
            "reliability": self.reliability.as_dict(),
            "latency_ms": self.latency_ms.as_dict(),
            "cost": self.cost.as_dict(),
            "token_count": self.token_count.as_dict(),
            "correctness": self.correctness.as_dict(),
            "evidence_citation": self.evidence_citation.as_dict(),
            "structured_format": self.structured_format.as_dict(),
            "safety": self.safety.as_dict(),
            "token_efficiency": self.token_efficiency.as_dict(),
            "tool_correctness": self.tool_correctness.as_dict(),
            "recovery": self.recovery.as_dict(),
            "human_correction": self.human_correction.as_dict(),
            "pass_rate": self.pass_rate,
            "confidence_interval": {
                "low": self.confidence_interval_low,
                "high": self.confidence_interval_high,
            },
            "evidence_digest": self.evidence_digest,
        }


@dataclass(frozen=True, slots=True)
class VerifierIdentity:
    model_id: str
    execution_identity: str
    provenance_digest: str

    def __post_init__(self) -> None:
        if not self.model_id.strip() or not self.execution_identity.strip():
            raise ValidationFailed("Verifier model ve execution identity ister")
        parse_digest(self.provenance_digest)


def aggregate_trials(
    trials: tuple[TrialResult, ...], *, tested_model_id: str, verifier: VerifierIdentity
) -> BenchmarkAggregate:
    if len(trials) < MINIMUM_REPETITIONS:
        raise PolicyViolation("Aggregate icin en az bes trial gerekir")
    if len({(trial.fixture_digest, trial.repetition) for trial in trials}) != len(trials):
        raise ValidationFailed("Fixture/repetition cifti tekil olmali")
    valid_trials = tuple(trial for trial in trials if trial.valid)
    if len(valid_trials) < MINIMUM_REPETITIONS:
        raise PolicyViolation(
            "Aggregate en az bes parse/format/evidence/verifier-valid trial ister"
        )
    if verifier.model_id == tested_model_id:
        raise PolicyViolation("Test edilen model kendi independent verifier'i olamaz")
    unsafe = any(trial.unsafe for trial in trials)
    approved = not unsafe and len(valid_trials) == len(trials)
    pass_rate = len(valid_trials) / len(trials)
    z = 1.959963984540054
    denominator = 1 + (z * z / len(trials))
    center = (pass_rate + (z * z / (2 * len(trials)))) / denominator
    margin = (
        z
        * math.sqrt(
            (pass_rate * (1 - pass_rate) / len(trials)) + (z * z / (4 * len(trials) * len(trials)))
        )
        / denominator
    )
    token_totals = [trial.input_tokens + trial.output_tokens for trial in trials]
    token_floor = max(1, min(token_totals))
    evidence_digest = digest(
        {
            "tested_model_id": tested_model_id,
            "verifier_model_id": verifier.model_id,
            "verifier_execution_identity": verifier.execution_identity,
            "verifier_provenance_digest": verifier.provenance_digest,
            "trials": [
                trial.evidence_digest
                for trial in sorted(
                    trials,
                    key=lambda row: (
                        row.fixture_digest,
                        row.repetition,
                        row.response_digest,
                        row.evidence_digest,
                    ),
                )
            ],
        }
    )
    return BenchmarkAggregate(
        approved=approved,
        unsafe=unsafe,
        verifier_model_id=verifier.model_id,
        verifier_execution_identity=verifier.execution_identity,
        verifier_provenance_digest=verifier.provenance_digest,
        tested_model_id=tested_model_id,
        quality=_aggregate([trial.quality for trial in trials]),
        reliability=_aggregate([trial.reliability for trial in trials]),
        latency_ms=_aggregate([float(trial.latency_ms) for trial in trials]),
        cost=_aggregate([trial.cost for trial in trials]),
        token_count=_aggregate(
            [float(trial.input_tokens + trial.output_tokens) for trial in trials]
        ),
        correctness=_aggregate([trial.quality for trial in trials]),
        evidence_citation=_aggregate([1.0 if trial.evidence_ok else 0.0 for trial in trials]),
        structured_format=_aggregate(
            [1.0 if trial.parse_ok and trial.format_ok else 0.0 for trial in trials]
        ),
        safety=_aggregate([0.0 if trial.unsafe else 1.0 for trial in trials]),
        token_efficiency=_aggregate(
            [token_floor / max(1, token_total) for token_total in token_totals]
        ),
        tool_correctness=_aggregate([trial.tool_correctness for trial in trials]),
        recovery=_aggregate([trial.recovery for trial in trials]),
        human_correction=_aggregate([float(trial.human_corrections) for trial in trials]),
        pass_rate=pass_rate,
        confidence_interval_low=max(0.0, center - margin),
        confidence_interval_high=min(1.0, center + margin),
        evidence_digest=evidence_digest,
    )


@dataclass(frozen=True, slots=True)
class QuotaObservation:
    pool: QuotaPool
    trust: QuotaTrust
    remaining_ratio: float | None
    source_digest: str | None
    observed_at: dt.datetime

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValidationFailed("Quota observation timezone ister")
        if self.trust is QuotaTrust.UNKNOWN:
            if self.remaining_ratio is not None or self.source_digest is not None:
                raise PolicyViolation("Bilinmeyen kota tahmin veya source digest tasiyamaz")
        else:
            if self.remaining_ratio is None or not 0 <= self.remaining_ratio <= 1:
                raise ValidationFailed("Guvenilir kota orani 0..1 araliginda olmali")
            if self.source_digest is None:
                raise ValidationFailed("Guvenilir kota source digest ister")
            parse_digest(self.source_digest)


def _quota_body(row: QuotaObservation) -> dict[str, Any]:
    return {
        "pool": row.pool.value,
        "trust": row.trust.value,
        "remaining_ratio": row.remaining_ratio,
        "source_digest": row.source_digest,
        "observed_at": row.observed_at,
    }


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    """Kanonik Work state olmayan, route puanina geri beslenen runtime kaniti."""

    model_id: str
    workload: str
    outcome: RuntimeOutcome
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cost: float
    human_corrections: int
    evidence_digest: str
    observed_at: dt.datetime
    authority_granted: bool = False

    def __post_init__(self) -> None:
        if not self.model_id.strip() or not self.workload.strip():
            raise ValidationFailed("Runtime observation model ve workload ister")
        if min(self.latency_ms, self.input_tokens, self.output_tokens, self.human_corrections) < 0:
            raise ValidationFailed("Runtime observation metrikleri negatif olamaz")
        if self.cost < 0:
            raise ValidationFailed("Runtime observation cost negatif olamaz")
        if self.observed_at.tzinfo is None:
            raise ValidationFailed("Runtime observation timezone ister")
        parse_digest(self.evidence_digest)
        if self.authority_granted:
            raise PolicyViolation("Runtime observation authority uretemez")


def quota_pool_order(
    observations: tuple[QuotaObservation, ...], *, now: dt.datetime | None = None
) -> tuple[QuotaPool, ...]:
    """Codex .40 ve Claude .30 kuralini trusted observation ile uygular."""
    moment = now or dt.datetime.now(dt.UTC)
    latest: dict[QuotaPool, QuotaObservation] = {}
    for row in observations:
        current = latest.get(row.pool)
        row_key = (row.observed_at, digest(_quota_body(row)))
        current_key = (
            None
            if current is None
            else (
                current.observed_at,
                digest(_quota_body(current)),
            )
        )
        if current_key is None or row_key > current_key:
            latest[row.pool] = row

    def current_trusted(pool: QuotaPool) -> QuotaObservation | None:
        row = latest.get(pool)
        if row is None or row.trust is QuotaTrust.UNKNOWN:
            return None
        if row.observed_at > moment or moment - row.observed_at > QUOTA_MAX_AGE:
            return None
        return row

    codex = latest.get(QuotaPool.CODEX)
    if codex is None or current_trusted(QuotaPool.CODEX) is None:
        return (QuotaPool.CODEX,)
    if codex.remaining_ratio is not None and codex.remaining_ratio >= 0.40:
        return (QuotaPool.CODEX,)
    claude = latest.get(QuotaPool.CLAUDE)
    if claude is None or current_trusted(QuotaPool.CLAUDE) is None:
        return (QuotaPool.CLAUDE,)
    if claude.remaining_ratio is not None and claude.remaining_ratio >= 0.30:
        return (QuotaPool.CLAUDE,)
    return (QuotaPool.LOCAL,)


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    model_id: str
    quota_pool: QuotaPool
    evidence_digests: tuple[str, ...]
    gates: dict[CandidateGate, bool]
    quality: float
    reliability: float
    project_specialization: float
    observed_success: float
    latency_efficiency: float
    token_efficiency: float
    cost_efficiency: float
    correction_efficiency: float

    def __post_init__(self) -> None:
        if set(self.gates) != set(HARD_GATE_ORDER):
            raise ValidationFailed("Aday butun hard gate sonuclarini tasimali")
        for value in self.evidence_digests:
            parse_digest(value)
        scores = (
            self.quality,
            self.reliability,
            self.project_specialization,
            self.observed_success,
            self.latency_efficiency,
            self.token_efficiency,
            self.cost_efficiency,
            self.correction_efficiency,
        )
        if any(not 0 <= value <= 1 for value in scores):
            raise ValidationFailed("Aday skor bilesenleri 0..1 araliginda olmali")

    @property
    def score(self) -> float:
        weights = (0.24, 0.18, 0.14, 0.12, 0.09, 0.08, 0.08, 0.07)
        values = (
            self.quality,
            self.reliability,
            self.project_specialization,
            self.observed_success,
            self.latency_efficiency,
            self.token_efficiency,
            self.cost_efficiency,
            self.correction_efficiency,
        )
        return sum(weight * value for weight, value in zip(weights, values, strict=True))

    @property
    def rejected_reasons(self) -> tuple[str, ...]:
        return tuple(gate.value for gate in HARD_GATE_ORDER if not self.gates[gate])


@dataclass(frozen=True, slots=True)
class DecisionRequirements:
    workload: str
    client: str
    modality: str
    project_id: str
    required_capabilities: tuple[str, ...]
    verifier_model_id: str
    local_data_required: bool
    max_latency_ms: float
    max_cost: float
    max_tokens: float
    evidence_digest: str

    def __post_init__(self) -> None:
        values = (self.workload, self.client, self.modality, self.project_id)
        if any(not value.strip() for value in values):
            raise ValidationFailed("Model Decision requirements alanlari bos olamaz")
        if min(self.max_latency_ms, self.max_cost, self.max_tokens) < 0:
            raise ValidationFailed("Model Decision budget negatif olamaz")
        parse_digest(self.evidence_digest)


@dataclass(frozen=True, slots=True)
class ModelDecision:
    selected_model_id: str | None
    selected_score: float | None
    candidates: tuple[ModelCandidate, ...]
    rejected: dict[str, tuple[str, ...]]
    evidence_digest: str
    authority_granted: bool = False

    def __post_init__(self) -> None:
        if self.authority_granted:
            raise PolicyViolation("Model Decision authority uretemez")


def decide_model(
    candidates: tuple[ModelCandidate, ...],
    observations: tuple[QuotaObservation, ...],
    *,
    now: dt.datetime | None = None,
) -> ModelDecision:
    pools = quota_pool_order(observations, now=now)
    rejected: dict[str, tuple[str, ...]] = {}
    qualified: list[ModelCandidate] = []
    effective_candidates: list[ModelCandidate] = []
    for original in candidates:
        effective_gates = dict(original.gates)
        effective_gates[CandidateGate.QUOTA] = original.quota_pool in pools
        candidate = replace(original, gates=effective_gates)
        effective_candidates.append(candidate)
        reasons = candidate.rejected_reasons
        if reasons:
            rejected[candidate.model_id] = reasons
        else:
            qualified.append(candidate)
    qualified.sort(key=lambda row: (-row.score, row.model_id))
    selected = qualified[0] if qualified else None
    evidence_digest = digest(
        {
            "pools": [pool.value for pool in pools],
            "quota_observations": [
                _quota_body(row)
                for row in sorted(
                    observations,
                    key=lambda item: (
                        item.pool.value,
                        item.observed_at,
                        digest(_quota_body(item)),
                    ),
                )
            ],
            "candidates": [
                {
                    "model_id": row.model_id,
                    "score": row.score,
                    "evidence": list(row.evidence_digests),
                    "gates": {gate.value: row.gates[gate] for gate in HARD_GATE_ORDER},
                    "rejected": list(rejected.get(row.model_id, ())),
                }
                for row in sorted(effective_candidates, key=lambda item: item.model_id)
            ],
        }
    )
    return ModelDecision(
        selected_model_id=None if selected is None else selected.model_id,
        selected_score=None if selected is None else selected.score,
        candidates=tuple(effective_candidates),
        rejected=rejected,
        evidence_digest=evidence_digest,
    )


@dataclass(frozen=True, slots=True)
class DeliberationBudget:
    max_rounds: int
    max_seconds: int
    max_tokens: int
    max_cost: float
    max_evidence_items: int

    def __post_init__(self) -> None:
        if not 1 <= self.max_rounds <= MAX_DELIBERATION_ROUNDS:
            raise PolicyViolation("Deliberation en fazla iki tur olabilir")
        if not 1 <= self.max_seconds <= MAX_DELIBERATION_SECONDS:
            raise PolicyViolation("Deliberation en fazla on dakika olabilir")
        if self.max_tokens < 1 or self.max_cost < 0 or self.max_evidence_items < 1:
            raise ValidationFailed("Token, cost ve evidence budget acik ve gecerli olmali")


@dataclass(frozen=True, slots=True)
class DeliberationFinding:
    participant_id: str
    round_number: int
    finding_digest: str
    objection: bool

    def __post_init__(self) -> None:
        if not self.participant_id.strip() or self.round_number < 1:
            raise ValidationFailed("Finding participant ve round ister")
        parse_digest(self.finding_digest)


@dataclass(frozen=True, slots=True)
class DeliberationResult:
    question_digest: str
    evidence_packet_digest: str
    consensus_digests: tuple[str, ...]
    contradiction_digests: tuple[str, ...]
    synthesizer_identity: str
    human_or_verifier_review_required: bool
    authority_granted: bool = False

    def __post_init__(self) -> None:
        if self.authority_granted:
            raise PolicyViolation("Deliberation authority veya mutation approval uretemez")
        if not self.synthesizer_identity.strip():
            raise ValidationFailed("Deliberation synthesizer identity ister")


def synthesize_deliberation(
    *,
    question_digest: str,
    evidence_packet_digest: str,
    budget: DeliberationBudget,
    findings: tuple[DeliberationFinding, ...],
    elapsed_seconds: int,
    token_count: int,
    cost: float,
    synthesizer_identity: str,
) -> DeliberationResult:
    parse_digest(question_digest)
    parse_digest(evidence_packet_digest)
    if not findings:
        raise ValidationFailed("Deliberation en az bir finding ister")
    participants = {item.participant_id for item in findings}
    if len(participants) < 2:
        raise PolicyViolation("Deliberation en az iki distinct participant ister")
    if not synthesizer_identity.strip() or synthesizer_identity in participants:
        raise PolicyViolation("Synthesizer participantlardan ayri identity olmali")
    if elapsed_seconds < 0 or token_count < 0 or cost < 0:
        raise ValidationFailed("Deliberation usage negatif olamaz")
    if max(item.round_number for item in findings) > budget.max_rounds:
        raise PolicyViolation("Deliberation round budget asildi")
    if (
        elapsed_seconds > budget.max_seconds
        or token_count > budget.max_tokens
        or cost > budget.max_cost
    ):
        raise PolicyViolation("Deliberation zaman/token/cost budget asildi")
    if len(findings) > budget.max_evidence_items:
        raise PolicyViolation("Deliberation evidence budget asildi")
    objections = tuple(sorted(item.finding_digest for item in findings if item.objection))
    consensus = tuple(sorted(item.finding_digest for item in findings if not item.objection))
    return DeliberationResult(
        question_digest=question_digest,
        evidence_packet_digest=evidence_packet_digest,
        consensus_digests=consensus,
        contradiction_digests=objections,
        synthesizer_identity=synthesizer_identity,
        human_or_verifier_review_required=bool(objections),
    )
