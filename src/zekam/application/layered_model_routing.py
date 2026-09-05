"""Application orchestration for evidence-bound layered model routing."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from zekam.application.capability_profile import profile_from_mapping
from zekam.application.model_benchmark_service import load_fixture_registry
from zekam.application.model_family_policy import load_model_family_policy
from zekam.application.project_integration import ProjectIntegrationService
from zekam.application.project_routing_context import (
    ProjectRoutingEvidence,
    build_project_routing_evidence,
)
from zekam.application.project_routing_targets import workloads_for_profile
from zekam.application.source_discovery import discover
from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation
from zekam.domain.model_benchmark import build_project_suite
from zekam.domain.model_catalog import ModelCatalogSnapshot
from zekam.domain.model_routing import (
    LAYER_ORDER,
    AgentRole,
    LayeredModelDecision,
    LayeredRouteRequest,
    ProjectRoutingContext,
    RoleRoutingPolicy,
    RouteCapabilityEvidence,
    RoutingLayer,
    RoutingQualification,
    StaleReason,
    decide_layered_model,
)

CONTEXT_TTL = dt.timedelta(days=7)
QUALIFICATION_TTL = dt.timedelta(days=7)


@dataclass(frozen=True, slots=True)
class GeneralCampaignQualificationEvidence:
    model_id: str
    aggregate_id: UUID
    aggregate_evidence_digest: str
    metrics: dict[str, Any]
    verifier_model_id: str
    verifier_execution_identity: str
    approved: bool
    unsafe: bool
    created_at: dt.datetime
    suite_id: UUID
    suite_digest: str
    health_result_id: UUID
    health_evidence_digest: str
    inventory_digest: str
    policy_digest: str
    adapter_digests: tuple[str, ...]


class LayeredRoutingStore(Protocol):
    def latest_policy(
        self, role: AgentRole, target_layer: RoutingLayer, *, at: dt.datetime
    ) -> tuple[UUID, RoleRoutingPolicy] | None: ...

    def qualifications_for(
        self, request: LayeredRouteRequest
    ) -> tuple[RoutingQualification, ...]: ...

    def capability_evidence_for(
        self, request: LayeredRouteRequest
    ) -> tuple[RouteCapabilityEvidence, ...]: ...

    def latest_context(self, project_id: UUID) -> tuple[UUID, ProjectRoutingContext] | None: ...

    def general_campaign_qualification_evidence(
        self, campaign_id: UUID
    ) -> tuple[GeneralCampaignQualificationEvidence, ...]: ...

    def store_suite_binding(
        self,
        *,
        benchmark_suite_id: UUID,
        suite_digest: str,
        layer: RoutingLayer,
        role: AgentRole,
        workload: str | None,
        technology: str | None,
        project_context_id: UUID | None,
        binding_digest: str,
    ) -> UUID: ...

    def store_qualification(
        self, qualification: RoutingQualification, *, suite_binding_id: UUID
    ) -> tuple[UUID, bool]: ...


class ModelCatalogStore(Protocol):
    def latest(self, provider_id: str) -> ModelCatalogSnapshot | None: ...


@dataclass(frozen=True, slots=True)
class PreparedProjectRoutingContext:
    project_slug: str
    workloads: tuple[str, ...]
    evidence: ProjectRoutingEvidence
    context: ProjectRoutingContext

    def sanitized(self) -> dict[str, Any]:
        return {
            "project_slug": self.project_slug,
            "workloads": list(self.workloads),
            "evidence": self.evidence.sanitized(),
            "context_digest": self.context.context_digest,
            "expires_at": self.context.expires_at.isoformat(),
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class RoutePreview:
    decision: LayeredModelDecision
    stale_reasons: tuple[StaleReason, ...]
    qualification_count: int

    def sanitized(self) -> dict[str, Any]:
        return {
            "status": self.decision.status.value,
            "primary_model_id": self.decision.primary_model_id,
            "fallback_model_id": self.decision.fallback_model_id,
            "evidence_digest": self.decision.evidence_digest,
            "qualification_count": self.qualification_count,
            "stale_reasons": [item.value for item in self.stale_reasons],
            "candidates": [
                {
                    "model_id": item.model_id,
                    "score": item.score,
                    "disposition": item.disposition.value,
                    "rejection_reasons": list(item.rejection_reasons),
                }
                for item in self.decision.candidates
            ],
            "grants_authority": False,
        }


def build_role_policy(
    role: AgentRole,
    target_layer: RoutingLayer,
    *,
    fallback_model_ids: tuple[str, ...] = (),
) -> RoleRoutingPolicy:
    required = LAYER_ORDER[: LAYER_ORDER.index(target_layer) + 1]
    independence = {
        AgentRole.IMPLEMENTER: (),
        AgentRole.REVIEWER: (AgentRole.IMPLEMENTER,),
        AgentRole.RESEARCHER: (),
        AgentRole.VERIFIER: (AgentRole.IMPLEMENTER, AgentRole.REVIEWER),
    }[role]
    body = {
        "schema": "zekam-routing-role-policy/v1",
        "role": role.value,
        "target_layer": target_layer.value,
        "required_layers": [item.value for item in required],
        "top_k": 4,
        "fallback_model_ids": list(fallback_model_ids),
        "max_cost": 0.0,
        "max_latency_ms": 30_000.0,
        "independent_from_roles": [item.value for item in independence],
        "fallback_scope_widening": False,
    }
    return RoleRoutingPolicy(
        role=role,
        target_layer=target_layer,
        required_layers=required,
        top_k=4,
        fallback_model_ids=fallback_model_ids,
        max_cost=0.0,
        max_latency_ms=30_000.0,
        independent_from_roles=independence,
        policy_digest=digest(body),
    )


def prepare_project_context(
    integration: ProjectIntegrationService,
    project_id: UUID,
    *,
    inventory_digest: str,
    policy_digest: str,
    now: dt.datetime | None = None,
) -> PreparedProjectRoutingContext:
    """Recompute a current source context; stale integrations are rejected."""

    parse_digest(inventory_digest)
    parse_digest(policy_digest)
    moment = now or dt.datetime.now(dt.UTC)
    report = integration.evaluate(project_id)
    if report.is_stale or not report.is_current or report.current_revision is None:
        raise PolicyViolation("Project routing context current integration ister")
    stored_profile = integration.profiles.latest_for_project(project_id)
    if stored_profile is None or stored_profile[0] != report.profile_digest:
        raise PolicyViolation("Project routing capability profile current degil")
    profile = profile_from_mapping(stored_profile[1])
    root = integration.resolve_source_root(project_id)
    discovery = discover(root)
    if discovery.tree_digest != report.current_revision.tree_digest:
        raise PolicyViolation("Project source routing hazirligi sirasinda drift oldu")
    workloads = workloads_for_profile(profile)
    fixture_registry = load_fixture_registry()
    project_suite = build_project_suite(
        project_id=str(project_id),
        capability_profile_digest=profile.digest,
        registry=fixture_registry,
    )
    evidence = build_project_routing_evidence(
        project_id=project_id,
        source_revision_id=report.current_revision.id,
        source_revision=report.current_revision.revision,
        report=discovery,
        profile=profile,
        workloads=workloads,
        project_suite_digest=project_suite.suite_digest,
    )
    context = ProjectRoutingContext(
        project_id=project_id,
        source_revision_id=report.current_revision.id,
        source_revision=report.current_revision.revision,
        tree_digest=evidence.tree_digest,
        capability_profile_digest=evidence.capability_profile_digest,
        dependency_digest=digest(
            {
                "dependency_set": evidence.dependency_set_digest,
                "dependency_lock": evidence.dependency_lock_digest,
            }
        ),
        framework_digest=evidence.framework_set_digest,
        technology_digest=evidence.technology_profile_digest,
        architecture_digest=evidence.architecture_digest,
        rules_digest=evidence.rule_set_digest,
        suite_digest=evidence.project_suite_digest,
        inventory_digest=inventory_digest,
        policy_digest=policy_digest,
        captured_at=moment,
        expires_at=moment + CONTEXT_TTL,
    )
    return PreparedProjectRoutingContext(
        project_slug=report.project.slug,
        workloads=workloads,
        evidence=evidence,
        context=context,
    )


def preview_route(
    repository: LayeredRoutingStore,
    request: LayeredRouteRequest,
    *,
    current_context: ProjectRoutingContext | None = None,
    provider_id: str | None = None,
    catalog: ModelCatalogStore | None = None,
    now: dt.datetime | None = None,
) -> RoutePreview:
    moment = now or dt.datetime.now(dt.UTC)
    stored_policy = repository.latest_policy(request.role, request.target_layer, at=moment)
    if stored_policy is None:
        policy = build_role_policy(request.role, request.target_layer)
    else:
        _, policy = stored_policy
    qualifications = repository.qualifications_for(request)
    capability_evidence = repository.capability_evidence_for(request)
    family_policy = None if request.family_policy_digest is None else load_model_family_policy()
    if provider_id is not None and catalog is None:
        raise PolicyViolation("Provider-scoped route preview model catalog store ister")
    catalog_snapshot = None
    if provider_id is not None:
        assert catalog is not None
        catalog_snapshot = catalog.latest(provider_id)
    decision = decide_layered_model(
        request,
        policy,
        qualifications,
        capability_evidence,
        family_policy,
        catalog_snapshot=catalog_snapshot,
        require_catalog=provider_id is not None,
        now=moment,
    )
    stale: tuple[StaleReason, ...] = ()
    if request.project_id is not None and current_context is not None:
        latest = repository.latest_context(request.project_id)
        stale = (
            (StaleReason.SOURCE_REVISION,)
            if latest is None
            else latest[1].stale_reasons(current_context, now=moment)
        )
    return RoutePreview(
        decision=decision,
        stale_reasons=stale,
        qualification_count=len(qualifications),
    )


def adopt_general_campaign_qualifications(
    repository: LayeredRoutingStore,
    *,
    campaign_id: UUID,
    role: AgentRole = AgentRole.IMPLEMENTER,
) -> tuple[UUID, ...]:
    """Adopt verified campaign aggregates into the general routing layer.

    This performs no provider call.  Every value is re-bound to the immutable
    campaign member result, benchmark suite, aggregate and tested claim adapter.
    """

    rows = repository.general_campaign_qualification_evidence(campaign_id)
    record_ids: list[UUID] = []
    for row in rows:
        adapter_digests = row.adapter_digests
        if len(adapter_digests) != 1:
            raise PolicyViolation("General routing tested execution identity ambiguous")
        metrics = row.metrics
        quality = float(dict(metrics.get("quality", {})).get("mean", -1))
        reliability = float(dict(metrics.get("reliability", {})).get("mean", -1))
        latency = float(dict(metrics.get("latency_ms", {})).get("mean", -1))
        cost = float(dict(metrics.get("cost", {})).get("mean", -1))
        if min(quality, reliability, latency, cost) < 0:
            raise PolicyViolation("General routing aggregate metric seti eksik")
        suite_binding_id = repository.store_suite_binding(
            benchmark_suite_id=row.suite_id,
            suite_digest=row.suite_digest,
            layer=RoutingLayer.GENERAL,
            role=role,
            workload=None,
            technology=None,
            project_context_id=None,
            binding_digest=digest(
                {
                    "schema": "zekam-general-routing-suite-binding/v1",
                    "campaign_id": str(campaign_id),
                    "model_id": row.model_id,
                    "suite_digest": row.suite_digest,
                    "role": role.value,
                }
            ),
        )
        qualification = RoutingQualification(
            model_id=row.model_id,
            layer=RoutingLayer.GENERAL,
            role=role,
            suite_digest=row.suite_digest,
            aggregate_id=row.aggregate_id,
            aggregate_evidence_digest=row.aggregate_evidence_digest,
            health_result_id=row.health_result_id,
            health_evidence_digest=row.health_evidence_digest,
            inventory_digest=row.inventory_digest,
            policy_digest=row.policy_digest,
            verifier_model_id=row.verifier_model_id,
            verifier_execution_identity=row.verifier_execution_identity,
            tested_execution_identity=f"provider-adapter:{adapter_digests[0]}",
            score=(quality + reliability) / 2.0,
            mean_latency_ms=latency,
            mean_cost=cost,
            workload=None,
            technology=None,
            project_context_digest=None,
            qualified=row.approved,
            unsafe=row.unsafe,
            valid_from=row.created_at,
            expires_at=row.created_at + QUALIFICATION_TTL,
        )
        record_id, _ = repository.store_qualification(
            qualification, suite_binding_id=suite_binding_id
        )
        record_ids.append(record_id)
    if not record_ids:
        raise PolicyViolation("Campaign qualified general routing sonucu tasimiyor")
    return tuple(record_ids)
