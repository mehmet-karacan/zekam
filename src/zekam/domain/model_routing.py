"""Evidence-bound general, workload and project model routing.

The objects in this module are authority-free.  They only combine immutable,
digest-addressed evidence; provider calls and execution authority live outside
the routing decision.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed


class RoutingLayer(StrEnum):
    GENERAL = "general"
    WORKLOAD = "workload-technology"
    PROJECT = "project"


LAYER_ORDER: tuple[RoutingLayer, ...] = (
    RoutingLayer.GENERAL,
    RoutingLayer.WORKLOAD,
    RoutingLayer.PROJECT,
)


class AgentRole(StrEnum):
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    RESEARCHER = "researcher"
    VERIFIER = "verifier"


class RouteStatus(StrEnum):
    SELECTED = "selected"
    PENDING = "pending"


class CandidateDisposition(StrEnum):
    PRIMARY = "primary"
    FALLBACK = "fallback"
    ELIGIBLE = "eligible"
    REJECTED = "rejected"


class RouteCapabilityDimension(StrEnum):
    CONTEXT = "context"
    TOOL = "tool"
    STRUCTURED_OUTPUT = "structured-output"
    LONG_SESSION = "long-session"


RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high", "critical")


@dataclass(frozen=True, slots=True)
class ModelFamilyPolicy:
    model_families: tuple[tuple[str, str], ...]
    same_family_allowed_risks: tuple[str, ...]

    def __post_init__(self) -> None:
        model_ids = tuple(model_id for model_id, _ in self.model_families)
        if not model_ids or len(model_ids) != len(set(model_ids)):
            raise ValidationFailed("Model family policy model seti bos veya tekrarli")
        for model_id, family in self.model_families:
            _nonblank(model_id, "Model family model id")
            _nonblank(family, "Model family")
            if family != family.casefold():
                raise ValidationFailed("Model family normalize olmali")
        if len(self.same_family_allowed_risks) != len(set(self.same_family_allowed_risks)) or any(
            risk not in RISK_LEVELS for risk in self.same_family_allowed_risks
        ):
            raise ValidationFailed("Same-family risk policy gecersiz")

    @property
    def policy_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-model-family-policy/v1",
                "model_families": [list(item) for item in self.model_families],
                "same_family_allowed_risks": list(self.same_family_allowed_risks),
            }
        )

    def family_for(self, model_id: str) -> str | None:
        return dict(self.model_families).get(model_id)


class StaleReason(StrEnum):
    SOURCE_REVISION = "source-revision"
    TREE = "tree"
    CAPABILITY = "capability-profile"
    DEPENDENCY = "dependency"
    FRAMEWORK = "framework"
    TECHNOLOGY = "technology"
    ARCHITECTURE = "architecture"
    RULES = "rules"
    SUITE = "suite"
    INVENTORY = "inventory"
    POLICY = "policy"
    EXPIRED = "expired"


def _nonblank(value: str, label: str, *, maximum: int = 256) -> None:
    if not value.strip() or len(value) > maximum or "://" in value or "\\" in value:
        raise ValidationFailed(f"{label} portable ve bos olmayan metadata olmali")


def _digests(*values: str) -> None:
    for value in values:
        parse_digest(value)


def _aware(value: dt.datetime, label: str) -> None:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValidationFailed(f"{label} timezone tasimali")


@dataclass(frozen=True, slots=True)
class ProjectRoutingContext:
    project_id: UUID
    source_revision_id: UUID
    source_revision: str
    tree_digest: str
    capability_profile_digest: str
    dependency_digest: str
    framework_digest: str
    technology_digest: str
    architecture_digest: str
    rules_digest: str
    suite_digest: str
    inventory_digest: str
    policy_digest: str
    captured_at: dt.datetime
    expires_at: dt.datetime

    def __post_init__(self) -> None:
        _nonblank(self.source_revision, "Source revision", maximum=128)
        _digests(
            self.tree_digest,
            self.capability_profile_digest,
            self.dependency_digest,
            self.framework_digest,
            self.technology_digest,
            self.architecture_digest,
            self.rules_digest,
            self.suite_digest,
            self.inventory_digest,
            self.policy_digest,
        )
        _aware(self.captured_at, "Context captured_at")
        _aware(self.expires_at, "Context expires_at")
        if self.expires_at <= self.captured_at:
            raise ValidationFailed("Project context expiry capture sonrasinda olmali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "source_revision_id": self.source_revision_id,
            "source_revision": self.source_revision,
            "tree_digest": self.tree_digest,
            "capability_profile_digest": self.capability_profile_digest,
            "dependency_digest": self.dependency_digest,
            "framework_digest": self.framework_digest,
            "technology_digest": self.technology_digest,
            "architecture_digest": self.architecture_digest,
            "rules_digest": self.rules_digest,
            "suite_digest": self.suite_digest,
            "inventory_digest": self.inventory_digest,
            "policy_digest": self.policy_digest,
            "captured_at": self.captured_at,
            "expires_at": self.expires_at,
        }

    @property
    def context_digest(self) -> str:
        return digest(self.as_dict())

    def stale_reasons(
        self, current: ProjectRoutingContext, *, now: dt.datetime | None = None
    ) -> tuple[StaleReason, ...]:
        if self.project_id != current.project_id:
            raise PolicyViolation("Project context baska projeyle karsilastirilamaz")
        moment = now or dt.datetime.now(dt.UTC)
        _aware(moment, "Staleness now")
        checks = (
            (StaleReason.SOURCE_REVISION, self.source_revision != current.source_revision),
            (StaleReason.TREE, self.tree_digest != current.tree_digest),
            (
                StaleReason.CAPABILITY,
                self.capability_profile_digest != current.capability_profile_digest,
            ),
            (StaleReason.DEPENDENCY, self.dependency_digest != current.dependency_digest),
            (StaleReason.FRAMEWORK, self.framework_digest != current.framework_digest),
            (StaleReason.TECHNOLOGY, self.technology_digest != current.technology_digest),
            (StaleReason.ARCHITECTURE, self.architecture_digest != current.architecture_digest),
            (StaleReason.RULES, self.rules_digest != current.rules_digest),
            (StaleReason.SUITE, self.suite_digest != current.suite_digest),
            (StaleReason.INVENTORY, self.inventory_digest != current.inventory_digest),
            (StaleReason.POLICY, self.policy_digest != current.policy_digest),
            (StaleReason.EXPIRED, moment > self.expires_at),
        )
        return tuple(reason for reason, changed in checks if changed)


@dataclass(frozen=True, slots=True)
class RoleRoutingPolicy:
    role: AgentRole
    target_layer: RoutingLayer
    required_layers: tuple[RoutingLayer, ...]
    top_k: int
    fallback_model_ids: tuple[str, ...]
    max_cost: float
    max_latency_ms: float
    independent_from_roles: tuple[AgentRole, ...]
    policy_digest: str

    def __post_init__(self) -> None:
        expected = LAYER_ORDER[: LAYER_ORDER.index(self.target_layer) + 1]
        if self.required_layers != expected:
            raise ValidationFailed("Required layers general'dan target layer'a exact prefix olmali")
        if not 1 <= self.top_k <= 20:
            raise ValidationFailed("Routing top_k 1..20 olmali")
        if self.max_cost < 0 or self.max_latency_ms < 0:
            raise ValidationFailed("Routing budget negatif olamaz")
        if len(set(self.fallback_model_ids)) != len(self.fallback_model_ids):
            raise ValidationFailed("Fallback model scope tekrarli olamaz")
        for value in self.fallback_model_ids:
            _nonblank(value, "Fallback model id")
        if self.role in self.independent_from_roles:
            raise ValidationFailed("Rol kendisinden bagimsiz olamaz")
        parse_digest(self.policy_digest)


@dataclass(frozen=True, slots=True)
class ExecutionTargetSnapshot:
    client_id: str
    slot: str
    execution_mode: str
    model_selectable: bool
    structured_result: bool
    cancellation: bool
    max_concurrency: int
    cost_evidence_digest: str
    capability_digest: str
    captured_at: dt.datetime
    expires_at: dt.datetime

    def __post_init__(self) -> None:
        _nonblank(self.client_id, "Execution client")
        _nonblank(self.slot, "Execution slot")
        if self.execution_mode not in {
            "native-parallel",
            "native-sequential",
            "isolated-role-fallback",
        }:
            raise ValidationFailed("Execution mode gecersiz")
        if self.max_concurrency < 1:
            raise ValidationFailed("Execution concurrency pozitif olmali")
        _digests(self.cost_evidence_digest, self.capability_digest)
        _aware(self.captured_at, "Execution captured_at")
        _aware(self.expires_at, "Execution expires_at")
        if self.expires_at <= self.captured_at:
            raise ValidationFailed("Execution target expiry capture sonrasinda olmali")

    @property
    def execution_identity(self) -> str:
        return f"{self.client_id}:{self.slot}"

    @property
    def snapshot_digest(self) -> str:
        return digest(
            {
                "client_id": self.client_id,
                "slot": self.slot,
                "execution_mode": self.execution_mode,
                "model_selectable": self.model_selectable,
                "structured_result": self.structured_result,
                "cancellation": self.cancellation,
                "max_concurrency": self.max_concurrency,
                "cost_evidence_digest": self.cost_evidence_digest,
                "capability_digest": self.capability_digest,
                "captured_at": self.captured_at,
                "expires_at": self.expires_at,
            }
        )


@dataclass(frozen=True, slots=True)
class RoutingQualification:
    model_id: str
    layer: RoutingLayer
    role: AgentRole
    suite_digest: str
    aggregate_id: UUID
    aggregate_evidence_digest: str
    health_result_id: UUID
    health_evidence_digest: str
    inventory_digest: str
    policy_digest: str
    verifier_model_id: str
    verifier_execution_identity: str
    tested_execution_identity: str
    score: float
    mean_latency_ms: float
    mean_cost: float
    workload: str | None
    technology: str | None
    project_context_digest: str | None
    qualified: bool
    unsafe: bool
    valid_from: dt.datetime
    expires_at: dt.datetime

    def __post_init__(self) -> None:
        for value, label in (
            (self.model_id, "Model id"),
            (self.verifier_model_id, "Verifier model id"),
            (self.verifier_execution_identity, "Verifier execution identity"),
            (self.tested_execution_identity, "Tested execution identity"),
        ):
            _nonblank(value, label)
        if self.model_id == self.verifier_model_id:
            raise PolicyViolation("Tested model kendi verifier'i olamaz")
        if self.verifier_execution_identity == self.tested_execution_identity:
            raise PolicyViolation("Tested ve verifier execution identity bagimsiz olmali")
        _digests(
            self.suite_digest,
            self.aggregate_evidence_digest,
            self.health_evidence_digest,
            self.inventory_digest,
            self.policy_digest,
        )
        if not 0 <= self.score <= 1:
            raise ValidationFailed("Qualification score 0..1 olmali")
        if self.mean_latency_ms < 0 or self.mean_cost < 0:
            raise ValidationFailed("Qualification latency/cost negatif olamaz")
        _aware(self.valid_from, "Qualification valid_from")
        _aware(self.expires_at, "Qualification expires_at")
        if self.expires_at <= self.valid_from:
            raise ValidationFailed("Qualification expiry valid_from sonrasinda olmali")
        if self.unsafe and self.qualified:
            raise PolicyViolation("Unsafe qualification qualified olamaz")
        if self.layer is RoutingLayer.GENERAL:
            if any((self.workload, self.technology, self.project_context_digest)):
                raise ValidationFailed("General qualification scoped metadata tasiyamaz")
        elif self.layer is RoutingLayer.WORKLOAD:
            if not self.workload or not self.technology or self.project_context_digest:
                raise ValidationFailed("Workload qualification workload+technology ister")
        elif not self.workload or not self.technology or not self.project_context_digest:
            raise ValidationFailed("Project qualification exact workload/technology/context ister")
        if self.project_context_digest is not None:
            parse_digest(self.project_context_digest)

    @property
    def evidence_digest(self) -> str:
        return digest(
            {
                "model_id": self.model_id,
                "layer": self.layer,
                "role": self.role,
                "suite_digest": self.suite_digest,
                "aggregate_id": self.aggregate_id,
                "aggregate_evidence_digest": self.aggregate_evidence_digest,
                "health_result_id": self.health_result_id,
                "health_evidence_digest": self.health_evidence_digest,
                "inventory_digest": self.inventory_digest,
                "policy_digest": self.policy_digest,
                "verifier_model_id": self.verifier_model_id,
                "verifier_execution_identity": self.verifier_execution_identity,
                "tested_execution_identity": self.tested_execution_identity,
                "score": self.score,
                "mean_latency_ms": self.mean_latency_ms,
                "mean_cost": self.mean_cost,
                "workload": self.workload,
                "technology": self.technology,
                "project_context_digest": self.project_context_digest,
                "qualified": self.qualified,
                "unsafe": self.unsafe,
                "valid_from": self.valid_from,
                "expires_at": self.expires_at,
            }
        )


@dataclass(frozen=True, slots=True)
class RouteCapabilityRequirements:
    minimum_context_tokens: int = 0
    minimum_tool_score: float = 0
    minimum_structured_output_score: float = 0
    minimum_long_session_seconds: int = 0
    minimum_long_session_score: float = 0

    def __post_init__(self) -> None:
        if (
            isinstance(self.minimum_context_tokens, bool)
            or not isinstance(self.minimum_context_tokens, int)
            or isinstance(self.minimum_long_session_seconds, bool)
            or not isinstance(self.minimum_long_session_seconds, int)
        ):
            raise ValidationFailed("Route capability miktar esigi tam sayi olmali")
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in (
                self.minimum_tool_score,
                self.minimum_structured_output_score,
                self.minimum_long_session_score,
            )
        ):
            raise ValidationFailed("Route capability puan esigi sayisal olmali")
        if self.minimum_context_tokens < 0 or self.minimum_long_session_seconds < 0:
            raise ValidationFailed("Route capability miktar esigi negatif olamaz")
        scores = (
            self.minimum_tool_score,
            self.minimum_structured_output_score,
            self.minimum_long_session_score,
        )
        if any(not 0 <= value <= 1 for value in scores):
            raise ValidationFailed("Route capability puan esigi 0..1 olmali")
        if (self.minimum_long_session_seconds == 0) != (self.minimum_long_session_score == 0):
            raise ValidationFailed("Long-session sure ve puan esikleri birlikte verilmelidir")

    @property
    def required_dimensions(self) -> tuple[RouteCapabilityDimension, ...]:
        return tuple(
            dimension
            for dimension, required in (
                (RouteCapabilityDimension.CONTEXT, self.minimum_context_tokens > 0),
                (RouteCapabilityDimension.TOOL, self.minimum_tool_score > 0),
                (
                    RouteCapabilityDimension.STRUCTURED_OUTPUT,
                    self.minimum_structured_output_score > 0,
                ),
                (RouteCapabilityDimension.LONG_SESSION, self.minimum_long_session_seconds > 0),
            )
            if required
        )

    def as_dict(self) -> dict[str, int | float]:
        return {
            "minimum_context_tokens": self.minimum_context_tokens,
            "minimum_tool_score": self.minimum_tool_score,
            "minimum_structured_output_score": self.minimum_structured_output_score,
            "minimum_long_session_seconds": self.minimum_long_session_seconds,
            "minimum_long_session_score": self.minimum_long_session_score,
        }


@dataclass(frozen=True, slots=True)
class RouteCapabilityBinding:
    evidence_role: AgentRole
    source_revision: str
    suite_digest: str
    registry_digest: str
    execution_profile_digest: str
    evaluator_provenance_digest: str

    def __post_init__(self) -> None:
        if not self.source_revision.strip() or "://" in self.source_revision:
            raise ValidationFailed("Route capability source revision gecersiz")
        _digests(
            self.suite_digest,
            self.registry_digest,
            self.execution_profile_digest,
            self.evaluator_provenance_digest,
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "evidence_role": self.evidence_role.value,
            "source_revision": self.source_revision,
            "suite_digest": self.suite_digest,
            "registry_digest": self.registry_digest,
            "execution_profile_digest": self.execution_profile_digest,
            "evaluator_provenance_digest": self.evaluator_provenance_digest,
        }


@dataclass(frozen=True, slots=True)
class RouteCapabilityEvidence:
    model_id: str
    role: AgentRole
    dimension: RouteCapabilityDimension
    score: float
    observed_quantity: int
    receipt_count: int
    inventory_digest: str
    policy_digest: str
    source_revision: str
    suite_digest: str
    registry_digest: str
    execution_profile_digest: str
    evaluator_provenance_digest: str
    source_scorecard_digest: str
    episode_evidence_digests: tuple[str, ...]
    observed_at: dt.datetime
    expires_at: dt.datetime

    def __post_init__(self) -> None:
        _nonblank(self.model_id, "Capability model id")
        if not 0 <= self.score <= 1 or min(self.observed_quantity, self.receipt_count) < 0:
            raise ValidationFailed("Route capability olcumu gecersiz")
        _digests(
            self.inventory_digest,
            self.policy_digest,
            self.suite_digest,
            self.registry_digest,
            self.execution_profile_digest,
            self.evaluator_provenance_digest,
            self.source_scorecard_digest,
            *self.episode_evidence_digests,
        )
        if not self.episode_evidence_digests:
            raise ValidationFailed("Route capability episode kaniti ister")
        if not self.source_revision.strip() or "://" in self.source_revision:
            raise ValidationFailed("Route capability source revision gecersiz")
        _aware(self.observed_at, "Capability observed_at")
        _aware(self.expires_at, "Capability expires_at")
        if self.expires_at <= self.observed_at:
            raise ValidationFailed("Capability expiry observation sonrasinda olmali")

    @property
    def evidence_digest(self) -> str:
        return digest(
            {
                "model_id": self.model_id,
                "role": self.role,
                "dimension": self.dimension,
                "score": self.score,
                "observed_quantity": self.observed_quantity,
                "receipt_count": self.receipt_count,
                "inventory_digest": self.inventory_digest,
                "policy_digest": self.policy_digest,
                "source_revision": self.source_revision,
                "suite_digest": self.suite_digest,
                "registry_digest": self.registry_digest,
                "execution_profile_digest": self.execution_profile_digest,
                "evaluator_provenance_digest": self.evaluator_provenance_digest,
                "source_scorecard_digest": self.source_scorecard_digest,
                "episode_evidence_digests": list(self.episode_evidence_digests),
                "observed_at": self.observed_at,
                "expires_at": self.expires_at,
            }
        )


@dataclass(frozen=True, slots=True)
class LayeredRouteRequest:
    role: AgentRole
    target_layer: RoutingLayer
    workload: str | None
    technology: str | None
    project_id: UUID | None
    project_context_digest: str | None
    inventory_digest: str
    routing_policy_digest: str
    policy_digest: str
    execution_target_digest: str
    capability_requirements: RouteCapabilityRequirements = field(
        default_factory=RouteCapabilityRequirements
    )
    capability_binding: RouteCapabilityBinding | None = None
    risk: str = "medium"
    family_policy_digest: str | None = None
    excluded_model_families: tuple[str, ...] = ()
    excluded_model_ids: tuple[str, ...] = ()
    excluded_execution_identities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _digests(
            self.inventory_digest,
            self.routing_policy_digest,
            self.policy_digest,
            self.execution_target_digest,
        )
        if self.target_layer is RoutingLayer.GENERAL:
            if any((self.workload, self.technology, self.project_id, self.project_context_digest)):
                raise ValidationFailed("General route scoped metadata tasiyamaz")
        elif self.target_layer is RoutingLayer.WORKLOAD:
            if not self.workload or not self.technology:
                raise ValidationFailed("Workload route exact workload+technology ister")
            if self.project_id is not None or self.project_context_digest is not None:
                raise ValidationFailed("Workload route project binding tasiyamaz")
        elif (
            not self.workload
            or not self.technology
            or self.project_id is None
            or self.project_context_digest is None
        ):
            raise ValidationFailed("Project route exact project/context/workload/technology ister")
        if self.project_context_digest is not None:
            parse_digest(self.project_context_digest)
        if bool(self.capability_requirements.required_dimensions) != (
            self.capability_binding is not None
        ):
            raise ValidationFailed("Route capability gereksinimi current binding ister")
        if self.risk not in RISK_LEVELS:
            raise ValidationFailed("Route risk gecersiz")
        if self.family_policy_digest is not None:
            parse_digest(self.family_policy_digest)
        if len(self.excluded_model_families) != len(set(self.excluded_model_families)) or any(
            not value or value != value.casefold() for value in self.excluded_model_families
        ):
            raise ValidationFailed("Route excluded model family seti gecersiz")
        if self.excluded_model_families and self.family_policy_digest is None:
            raise ValidationFailed("Route family exclusion policy digest ister")


@dataclass(frozen=True, slots=True)
class LayerCandidateEvidence:
    model_id: str
    layer_scores: tuple[tuple[RoutingLayer, float], ...]
    evidence_digests: tuple[str, ...]
    rejection_reasons: tuple[str, ...]
    disposition: CandidateDisposition = CandidateDisposition.ELIGIBLE

    def __post_init__(self) -> None:
        _nonblank(self.model_id, "Candidate model id")
        for value in self.evidence_digests:
            parse_digest(value)
        if len({layer for layer, _ in self.layer_scores}) != len(self.layer_scores):
            raise ValidationFailed("Candidate layer score tekrarli olamaz")

    @property
    def score(self) -> float:
        return (
            0.0
            if not self.layer_scores
            else sum(v for _, v in self.layer_scores) / len(self.layer_scores)
        )


@dataclass(frozen=True, slots=True)
class LayeredModelDecision:
    request: LayeredRouteRequest
    policy_digest: str
    status: RouteStatus
    primary_model_id: str | None
    fallback_model_id: str | None
    candidates: tuple[LayerCandidateEvidence, ...]
    evidence_digest: str
    authority_granted: bool = False

    def __post_init__(self) -> None:
        parse_digest(self.policy_digest)
        parse_digest(self.evidence_digest)
        if self.authority_granted:
            raise PolicyViolation("Routing decision authority uretemez")
        if self.status is RouteStatus.PENDING and (
            self.primary_model_id is not None or self.fallback_model_id is not None
        ):
            raise ValidationFailed("Pending decision model secemez")
        if self.status is RouteStatus.SELECTED and self.primary_model_id is None:
            raise ValidationFailed("Selected decision primary ister")
        if self.fallback_model_id is not None and self.fallback_model_id == self.primary_model_id:
            raise ValidationFailed("Primary ve fallback ayni olamaz")


def _qualification_matches(item: RoutingQualification, request: LayeredRouteRequest) -> bool:
    if item.role is not request.role:
        return False
    if item.layer is RoutingLayer.GENERAL:
        return True
    if item.workload != request.workload or item.technology != request.technology:
        return False
    return item.layer is not RoutingLayer.PROJECT or (
        item.project_context_digest == request.project_context_digest
    )


def decide_layered_model(
    request: LayeredRouteRequest,
    policy: RoleRoutingPolicy,
    qualifications: tuple[RoutingQualification, ...],
    capability_evidence: tuple[RouteCapabilityEvidence, ...] = (),
    family_policy: ModelFamilyPolicy | None = None,
    *,
    now: dt.datetime | None = None,
) -> LayeredModelDecision:
    """Intersect fresh qualifications and return explicit primary/fallback/pending."""

    moment = now or dt.datetime.now(dt.UTC)
    _aware(moment, "Routing now")
    if policy.role is not request.role or policy.target_layer is not request.target_layer:
        raise PolicyViolation("Routing request/policy scope mismatch")
    if policy.policy_digest != request.routing_policy_digest:
        raise PolicyViolation("Routing role policy drift")
    if (family_policy is None) != (request.family_policy_digest is None):
        raise PolicyViolation("Routing family policy binding eksik")
    if family_policy is not None and family_policy.policy_digest != request.family_policy_digest:
        raise PolicyViolation("Routing family policy drift")
    independence_missing = bool(policy.independent_from_roles) and (
        not request.excluded_model_ids or not request.excluded_execution_identities
    )

    by_model: dict[str, list[RoutingQualification]] = {}
    for qualification in qualifications:
        if _qualification_matches(qualification, request):
            by_model.setdefault(qualification.model_id, []).append(qualification)
    capabilities_by_model: dict[str, list[RouteCapabilityEvidence]] = {}
    for capability in capability_evidence:
        capabilities_by_model.setdefault(capability.model_id, []).append(capability)

    candidates: list[LayerCandidateEvidence] = []
    for model_id in sorted(by_model):
        rows = by_model[model_id]
        reasons: list[str] = []
        if independence_missing:
            reasons.append("independence-evidence-missing")
        model_family = None if family_policy is None else family_policy.family_for(model_id)
        if family_policy is not None and model_family is None:
            reasons.append("model-family-missing")
        if (
            request.role is AgentRole.VERIFIER
            and request.risk in {"high", "critical"}
            and (
                family_policy is None or request.risk not in family_policy.same_family_allowed_risks
            )
        ):
            if not request.excluded_model_families:
                reasons.append("family-independence-evidence-missing")
            elif model_family in request.excluded_model_families:
                reasons.append("same-family-verifier")
        selected: list[RoutingQualification] = []
        for layer in policy.required_layers:
            layer_rows = [item for item in rows if item.layer is layer]
            if not layer_rows:
                reasons.append(f"missing:{layer.value}")
                continue
            newest_at = max(item.valid_from for item in layer_rows)
            newest_qualifications = [item for item in layer_rows if item.valid_from == newest_at]
            if len({item.evidence_digest for item in newest_qualifications}) != 1:
                reasons.append(f"ambiguous:{layer.value}")
                continue
            current = newest_qualifications[0]
            if moment < current.valid_from or moment > current.expires_at:
                reasons.append(f"stale:{layer.value}")
            if current.inventory_digest != request.inventory_digest:
                reasons.append(f"inventory-drift:{layer.value}")
            if current.policy_digest != request.policy_digest:
                reasons.append(f"policy-drift:{layer.value}")
            if current.mean_latency_ms > policy.max_latency_ms:
                reasons.append(f"latency-budget:{layer.value}")
            if current.mean_cost > policy.max_cost:
                reasons.append(f"cost-budget:{layer.value}")
            if not current.qualified or current.unsafe:
                reasons.append(f"unqualified:{layer.value}")
            if model_id in request.excluded_model_ids:
                reasons.append("model-independence")
            if current.tested_execution_identity in request.excluded_execution_identities:
                reasons.append("execution-independence")
            selected.append(current)
        selected_capabilities: list[RouteCapabilityEvidence] = []
        for dimension in request.capability_requirements.required_dimensions:
            if request.capability_binding is None:
                raise PolicyViolation("Route capability binding beklenmedik sekilde eksik")
            dimension_rows = [
                item
                for item in capabilities_by_model.get(model_id, ())
                if item.dimension is dimension
                and item.role is request.capability_binding.evidence_role
            ]
            if not dimension_rows:
                reasons.append(f"capability-missing:{dimension.value}")
                continue
            newest_at = max(item.observed_at for item in dimension_rows)
            newest_capabilities = [item for item in dimension_rows if item.observed_at == newest_at]
            if len({item.evidence_digest for item in newest_capabilities}) != 1:
                reasons.append(f"capability-ambiguous:{dimension.value}")
                continue
            current_capability = newest_capabilities[0]
            if moment < current_capability.observed_at or moment > current_capability.expires_at:
                reasons.append(f"capability-stale:{dimension.value}")
            if current_capability.inventory_digest != request.inventory_digest:
                reasons.append(f"capability-inventory-drift:{dimension.value}")
            if current_capability.policy_digest != request.policy_digest:
                reasons.append(f"capability-policy-drift:{dimension.value}")
            binding = request.capability_binding
            if current_capability.source_revision != binding.source_revision:
                reasons.append(f"capability-source-drift:{dimension.value}")
            if current_capability.suite_digest != binding.suite_digest:
                reasons.append(f"capability-suite-drift:{dimension.value}")
            if current_capability.registry_digest != binding.registry_digest:
                reasons.append(f"capability-registry-drift:{dimension.value}")
            if current_capability.execution_profile_digest != binding.execution_profile_digest:
                reasons.append(f"capability-profile-drift:{dimension.value}")
            if current_capability.evaluator_provenance_digest != (
                binding.evaluator_provenance_digest
            ):
                reasons.append(f"capability-evaluator-drift:{dimension.value}")
            requirements = request.capability_requirements
            if (
                dimension is RouteCapabilityDimension.CONTEXT
                and current_capability.observed_quantity < requirements.minimum_context_tokens
            ):
                reasons.append("context-capacity")
            elif dimension is RouteCapabilityDimension.TOOL:
                if current_capability.score < requirements.minimum_tool_score:
                    reasons.append("tool-score")
                if current_capability.receipt_count < 1:
                    reasons.append("tool-receipt-missing")
            elif (
                dimension is RouteCapabilityDimension.STRUCTURED_OUTPUT
                and current_capability.score < requirements.minimum_structured_output_score
            ):
                reasons.append("structured-output-score")
            elif dimension is RouteCapabilityDimension.LONG_SESSION:
                if current_capability.observed_quantity < requirements.minimum_long_session_seconds:
                    reasons.append("long-session-duration")
                if current_capability.score < requirements.minimum_long_session_score:
                    reasons.append("long-session-score")
                if current_capability.receipt_count < 1:
                    reasons.append("long-session-checkpoint-missing")
            selected_capabilities.append(current_capability)
        unique_reasons = tuple(dict.fromkeys(reasons))
        candidates.append(
            LayerCandidateEvidence(
                model_id=model_id,
                layer_scores=tuple((item.layer, item.score) for item in selected),
                evidence_digests=tuple(item.evidence_digest for item in selected)
                + tuple(item.evidence_digest for item in selected_capabilities),
                rejection_reasons=unique_reasons,
                disposition=(
                    CandidateDisposition.REJECTED
                    if unique_reasons
                    else CandidateDisposition.ELIGIBLE
                ),
            )
        )

    eligible = sorted(
        (item for item in candidates if not item.rejection_reasons),
        key=lambda item: (-item.score, item.model_id),
    )[: policy.top_k]
    primary = eligible[0] if eligible else None
    fallback = next(
        (item for item in eligible[1:] if item.model_id in frozenset(policy.fallback_model_ids)),
        None,
    )
    dispositions = {
        item.model_id: (
            CandidateDisposition.PRIMARY
            if primary is not None and item.model_id == primary.model_id
            else CandidateDisposition.FALLBACK
            if fallback is not None and item.model_id == fallback.model_id
            else item.disposition
        )
        for item in candidates
    }
    finalized = tuple(
        LayerCandidateEvidence(
            model_id=item.model_id,
            layer_scores=item.layer_scores,
            evidence_digests=item.evidence_digests,
            rejection_reasons=item.rejection_reasons,
            disposition=dispositions[item.model_id],
        )
        for item in candidates
    )
    evidence = digest(
        {
            "request": {
                "role": request.role,
                "target_layer": request.target_layer,
                "workload": request.workload,
                "technology": request.technology,
                "project_id": request.project_id,
                "project_context_digest": request.project_context_digest,
                "inventory_digest": request.inventory_digest,
                "routing_policy_digest": request.routing_policy_digest,
                "policy_digest": request.policy_digest,
                "execution_target_digest": request.execution_target_digest,
                "capability_requirements": request.capability_requirements.as_dict(),
                "capability_binding": (
                    None
                    if request.capability_binding is None
                    else request.capability_binding.as_dict()
                ),
                "risk": request.risk,
                "family_policy_digest": request.family_policy_digest,
                "excluded_model_families": list(request.excluded_model_families),
                "excluded_model_ids": list(request.excluded_model_ids),
                "excluded_execution_identities": list(request.excluded_execution_identities),
            },
            "policy_digest": policy.policy_digest,
            "primary": None if primary is None else primary.model_id,
            "fallback": None if fallback is None else fallback.model_id,
            "candidates": [
                {
                    "model_id": item.model_id,
                    "scores": [(layer.value, score) for layer, score in item.layer_scores],
                    "evidence": list(item.evidence_digests),
                    "rejected": list(item.rejection_reasons),
                    "disposition": dispositions[item.model_id],
                }
                for item in finalized
            ],
        }
    )
    return LayeredModelDecision(
        request=request,
        policy_digest=policy.policy_digest,
        status=RouteStatus.SELECTED if primary is not None else RouteStatus.PENDING,
        primary_model_id=None if primary is None else primary.model_id,
        fallback_model_id=None if fallback is None else fallback.model_id,
        candidates=finalized,
        evidence_digest=evidence,
    )
