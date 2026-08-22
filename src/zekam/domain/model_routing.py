"""Evidence-bound general, workload and project model routing.

The objects in this module are authority-free.  They only combine immutable,
digest-addressed evidence; provider calls and execution authority live outside
the routing decision.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
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
    independence_missing = bool(policy.independent_from_roles) and (
        not request.excluded_model_ids or not request.excluded_execution_identities
    )

    by_model: dict[str, list[RoutingQualification]] = {}
    for item in qualifications:
        if _qualification_matches(item, request):
            by_model.setdefault(item.model_id, []).append(item)

    candidates: list[LayerCandidateEvidence] = []
    for model_id in sorted(by_model):
        rows = by_model[model_id]
        reasons: list[str] = []
        if independence_missing:
            reasons.append("independence-evidence-missing")
        selected: list[RoutingQualification] = []
        for layer in policy.required_layers:
            layer_rows = [item for item in rows if item.layer is layer]
            if not layer_rows:
                reasons.append(f"missing:{layer.value}")
                continue
            newest_at = max(item.valid_from for item in layer_rows)
            newest = [item for item in layer_rows if item.valid_from == newest_at]
            if len({item.evidence_digest for item in newest}) != 1:
                reasons.append(f"ambiguous:{layer.value}")
                continue
            current = newest[0]
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
        unique_reasons = tuple(dict.fromkeys(reasons))
        candidates.append(
            LayerCandidateEvidence(
                model_id=model_id,
                layer_scores=tuple((item.layer, item.score) for item in selected),
                evidence_digests=tuple(item.evidence_digest for item in selected),
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
