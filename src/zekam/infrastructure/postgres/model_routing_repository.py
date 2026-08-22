"""PostgreSQL ledger for authority-free layered model routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json
from zekam.domain.errors import ConcurrencyConflict, NotFound
from zekam.domain.identifiers import new_uuid7
from zekam.domain.model_routing import (
    AgentRole,
    CandidateDisposition,
    ExecutionTargetSnapshot,
    LayerCandidateEvidence,
    LayeredModelDecision,
    LayeredRouteRequest,
    ProjectRoutingContext,
    RoleRoutingPolicy,
    RouteStatus,
    RoutingLayer,
    RoutingQualification,
    StaleReason,
)


@dataclass(frozen=True, slots=True)
class StoredRouteDecision:
    id: UUID
    role_policy_id: UUID
    project_context_id: UUID | None
    execution_target_id: UUID | None
    decision: LayeredModelDecision


@dataclass(frozen=True, slots=True)
class ModelRoutingRepository:
    connection: Any
    realm_id: UUID

    def store_project_context(self, context: ProjectRoutingContext) -> tuple[UUID, bool]:
        record_id = new_uuid7(now=context.captured_at)
        values = (
            record_id,
            self.realm_id,
            context.project_id,
            context.source_revision_id,
            context.source_revision,
            context.tree_digest,
            context.capability_profile_digest,
            context.dependency_digest,
            context.framework_digest,
            context.technology_digest,
            context.architecture_digest,
            context.rules_digest,
            context.suite_digest,
            context.inventory_digest,
            context.policy_digest,
            context.context_digest,
            context.captured_at,
            context.expires_at,
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into projects.routing_context_snapshot"
                " (id, realm_id, project_id, source_revision_id, source_revision, tree_digest,"
                " capability_profile_digest, dependency_digest, framework_digest,"
                " technology_digest, architecture_digest, rules_digest, suite_digest,"
                " inventory_digest, policy_digest, context_digest, captured_at, expires_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " %s, %s, %s, %s) on conflict (realm_id, context_digest) do nothing returning id",
                values,
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                return UUID(str(inserted[0])), True
            cursor.execute(
                "select id, project_id, source_revision_id from projects.routing_context_snapshot"
                " where realm_id = %s and context_digest = %s",
                (self.realm_id, context.context_digest),
            )
            existing = cursor.fetchone()
        if existing is None:
            raise ConcurrencyConflict("Routing context concurrent replay kayboldu")
        if UUID(str(existing[1])) != context.project_id or UUID(str(existing[2])) != (
            context.source_revision_id
        ):
            raise ConcurrencyConflict("Routing context replay scope drift")
        return UUID(str(existing[0])), False

    def latest_context(self, project_id: UUID) -> tuple[UUID, ProjectRoutingContext] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, project_id, source_revision_id, source_revision, tree_digest,"
                " capability_profile_digest, dependency_digest, framework_digest,"
                " technology_digest, architecture_digest, rules_digest, suite_digest,"
                " inventory_digest, policy_digest, captured_at, expires_at"
                " from projects.routing_context_snapshot"
                " where realm_id = %s and project_id = %s"
                " order by captured_at desc, id desc limit 1",
                (self.realm_id, project_id),
            )
            row = cursor.fetchone()
        return None if row is None else (UUID(str(row[0])), _context(row[1:]))

    def context(self, context_id: UUID) -> ProjectRoutingContext:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select project_id, source_revision_id, source_revision, tree_digest,"
                " capability_profile_digest, dependency_digest, framework_digest,"
                " technology_digest, architecture_digest, rules_digest, suite_digest,"
                " inventory_digest, policy_digest, captured_at, expires_at"
                " from projects.routing_context_snapshot where realm_id = %s and id = %s",
                (self.realm_id, context_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise NotFound("Routing context bulunamadi")
        return _context(row)

    def staleness_of(
        self, context_id: UUID, current: ProjectRoutingContext
    ) -> tuple[StaleReason, ...]:
        return self.context(context_id).stale_reasons(current)

    def store_role_policy(self, policy: RoleRoutingPolicy, *, effective_from: Any) -> UUID:
        record_id = new_uuid7(now=effective_from)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.routing_role_policy"
                " (id, realm_id, role, target_layer, required_layers, top_k,"
                " fallback_model_ids, max_cost, max_latency_ms, independent_from_roles,"
                " policy_digest, effective_from) values (%s, %s, %s, %s, %s, %s, %s,"
                " %s, %s, %s, %s, %s) on conflict (realm_id, policy_digest) do nothing"
                " returning id",
                (
                    record_id,
                    self.realm_id,
                    policy.role.value,
                    policy.target_layer.value,
                    [item.value for item in policy.required_layers],
                    policy.top_k,
                    list(policy.fallback_model_ids),
                    policy.max_cost,
                    policy.max_latency_ms,
                    [item.value for item in policy.independent_from_roles],
                    policy.policy_digest,
                    effective_from,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0]))
            cursor.execute(
                "select id, role, target_layer, required_layers, top_k, fallback_model_ids,"
                " max_cost, max_latency_ms, independent_from_roles, policy_digest"
                " from models.routing_role_policy"
                " where realm_id = %s and policy_digest = %s",
                (self.realm_id, policy.policy_digest),
            )
            existing = cursor.fetchone()
        if existing is None:
            raise ConcurrencyConflict("Routing policy concurrent replay kayboldu")
        if _policy(existing[1:]) != policy:
            raise ConcurrencyConflict("Routing policy replay scope drift")
        return UUID(str(existing[0]))

    def store_execution_target(self, target: ExecutionTargetSnapshot) -> tuple[UUID, bool]:
        record_id = new_uuid7(now=target.captured_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.execution_target_snapshot"
                " (id, realm_id, client_id, slot, execution_mode, model_selectable,"
                " structured_result, cancellation, max_concurrency, cost_evidence_digest,"
                " capability_digest, snapshot_digest, captured_at, expires_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, snapshot_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    target.client_id,
                    target.slot,
                    target.execution_mode,
                    target.model_selectable,
                    target.structured_result,
                    target.cancellation,
                    target.max_concurrency,
                    target.cost_evidence_digest,
                    target.capability_digest,
                    target.snapshot_digest,
                    target.captured_at,
                    target.expires_at,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                "select id from models.execution_target_snapshot"
                " where realm_id=%s and snapshot_digest=%s",
                (self.realm_id, target.snapshot_digest),
            )
            existing = cursor.fetchone()
        if existing is None:
            raise ConcurrencyConflict("Execution target concurrent replay kayboldu")
        return UUID(str(existing[0])), False

    def latest_execution_target(
        self, client_id: str, *, at: Any
    ) -> tuple[UUID, ExecutionTargetSnapshot] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, client_id, slot, execution_mode, model_selectable,"
                " structured_result, cancellation, max_concurrency, cost_evidence_digest,"
                " capability_digest, captured_at, expires_at"
                " from models.execution_target_snapshot where realm_id=%s and client_id=%s"
                " and captured_at<=%s and expires_at>=%s"
                " order by captured_at desc, id desc limit 1",
                (self.realm_id, client_id, at, at),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return UUID(str(row[0])), _execution_target(row[1:])

    def execution_target_by_digest(
        self, snapshot_digest: str, *, at: Any
    ) -> tuple[UUID, ExecutionTargetSnapshot] | None:
        """Resolve the exact reviewed target without timestamp/UUID tie-breaking."""

        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, client_id, slot, execution_mode, model_selectable,"
                " structured_result, cancellation, max_concurrency, cost_evidence_digest,"
                " capability_digest, captured_at, expires_at"
                " from models.execution_target_snapshot where realm_id=%s"
                " and snapshot_digest=%s and captured_at<=%s and expires_at>=%s",
                (self.realm_id, snapshot_digest, at, at),
            )
            row = cursor.fetchone()
        return None if row is None else (UUID(str(row[0])), _execution_target(row[1:]))

    def latest_policy(
        self, role: AgentRole, target_layer: RoutingLayer, *, at: Any
    ) -> tuple[UUID, RoleRoutingPolicy] | None:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select id, role, target_layer, required_layers, top_k, fallback_model_ids,"
                " max_cost, max_latency_ms, independent_from_roles, policy_digest"
                " from models.routing_role_policy where realm_id = %s and role = %s"
                " and target_layer = %s and effective_from <= %s"
                " and (expires_at is null or expires_at >= %s)"
                " order by effective_from desc, id desc limit 1",
                (self.realm_id, role.value, target_layer.value, at, at),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return UUID(str(row[0])), _policy(row[1:])

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
    ) -> UUID:
        record_id = new_uuid7()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.routing_suite_binding"
                " (id, realm_id, benchmark_suite_id, suite_digest, layer, role, workload,"
                " technology, project_context_id, binding_digest)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, binding_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    benchmark_suite_id,
                    suite_digest,
                    layer.value,
                    role.value,
                    workload,
                    technology,
                    project_context_id,
                    binding_digest,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0]))
            cursor.execute(
                "select id, benchmark_suite_id, suite_digest, layer, role, workload,"
                " technology, project_context_id from models.routing_suite_binding"
                " where realm_id = %s and binding_digest = %s",
                (self.realm_id, binding_digest),
            )
            existing = cursor.fetchone()
        if existing is None:
            raise ConcurrencyConflict("Routing suite binding concurrent replay kayboldu")
        expected = (
            benchmark_suite_id,
            suite_digest,
            layer.value,
            role.value,
            workload,
            technology,
            project_context_id,
        )
        actual = (
            UUID(str(existing[1])),
            str(existing[2]),
            str(existing[3]),
            str(existing[4]),
            str(existing[5]) if existing[5] is not None else None,
            str(existing[6]) if existing[6] is not None else None,
            _uuid(existing[7]),
        )
        if actual != expected:
            raise ConcurrencyConflict("Routing suite binding replay scope drift")
        return UUID(str(existing[0]))

    def store_qualification(
        self, qualification: RoutingQualification, *, suite_binding_id: UUID
    ) -> tuple[UUID, bool]:
        record_id = new_uuid7(now=qualification.valid_from)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.model_routing_qualification"
                " (id, realm_id, model_id, suite_binding_id, aggregate_id,"
                " aggregate_evidence_digest, health_result_id,"
                " health_evidence_digest,"
                " inventory_digest, policy_digest, verifier_model_id,"
                " verifier_execution_identity, tested_execution_identity, score,"
                " mean_latency_ms, mean_cost, qualified, unsafe, evidence_digest,"
                " valid_from, expires_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, evidence_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    qualification.model_id,
                    suite_binding_id,
                    qualification.aggregate_id,
                    qualification.aggregate_evidence_digest,
                    qualification.health_result_id,
                    qualification.health_evidence_digest,
                    qualification.inventory_digest,
                    qualification.policy_digest,
                    qualification.verifier_model_id,
                    qualification.verifier_execution_identity,
                    qualification.tested_execution_identity,
                    qualification.score,
                    qualification.mean_latency_ms,
                    qualification.mean_cost,
                    qualification.qualified,
                    qualification.unsafe,
                    qualification.evidence_digest,
                    qualification.valid_from,
                    qualification.expires_at,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0])), True
            cursor.execute(
                "select id, suite_binding_id, aggregate_id"
                " from models.model_routing_qualification"
                " where realm_id = %s and evidence_digest = %s",
                (self.realm_id, qualification.evidence_digest),
            )
            existing = cursor.fetchone()
        if existing is None:
            raise ConcurrencyConflict("Routing qualification concurrent replay kayboldu")
        if UUID(str(existing[1])) != suite_binding_id or UUID(str(existing[2])) != (
            qualification.aggregate_id
        ):
            raise ConcurrencyConflict("Routing qualification replay scope drift")
        return UUID(str(existing[0])), False

    def qualifications_for(self, request: LayeredRouteRequest) -> tuple[RoutingQualification, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select q.model_id, b.layer, b.role, b.suite_digest, q.aggregate_id,"
                " q.aggregate_evidence_digest, q.health_result_id,"
                " q.health_evidence_digest,"
                " q.inventory_digest, q.policy_digest, q.verifier_model_id,"
                " q.verifier_execution_identity, q.tested_execution_identity, q.score,"
                " q.mean_latency_ms, q.mean_cost, b.workload, b.technology, c.context_digest,"
                " q.qualified, q.unsafe,"
                " q.valid_from, q.expires_at"
                " from models.model_routing_qualification q"
                " join models.routing_suite_binding b"
                "   on b.realm_id = q.realm_id and b.id = q.suite_binding_id"
                " left join projects.routing_context_snapshot c"
                "   on c.realm_id = b.realm_id and c.id = b.project_context_id"
                " where q.realm_id = %s and b.role = %s"
                " and (b.layer = 'general' or (b.workload = %s and b.technology = %s))"
                " and (b.layer <> 'project' or c.context_digest = %s)"
                " order by q.model_id, b.layer, q.valid_from, q.id",
                (
                    self.realm_id,
                    request.role.value,
                    request.workload,
                    request.technology,
                    request.project_context_digest,
                ),
            )
            rows = cursor.fetchall()
        return tuple(_qualification(row) for row in rows)

    def record_decision(
        self,
        decision: LayeredModelDecision,
        *,
        role_policy_id: UUID,
        project_context_id: UUID | None = None,
        execution_target_id: UUID | None = None,
        decided_at: Any,
    ) -> tuple[UUID, bool]:
        record_id = new_uuid7(now=decided_at)
        request = decision.request
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.model_route_decision"
                " (id, realm_id, role_policy_id, execution_target_id, project_id,"
                " project_context_id, role, target_layer, workload, technology,"
                " inventory_digest, routing_policy_digest, policy_digest,"
                " execution_target_digest, excluded_model_ids,"
                " excluded_execution_identities, status, primary_model_id,"
                " fallback_model_id, evidence_digest, authority_granted, decided_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " %s, %s, %s, %s, %s, %s, false, %s)"
                " on conflict (realm_id, evidence_digest) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    role_policy_id,
                    execution_target_id,
                    request.project_id,
                    project_context_id,
                    request.role.value,
                    request.target_layer.value,
                    request.workload,
                    request.technology,
                    request.inventory_digest,
                    request.routing_policy_digest,
                    request.policy_digest,
                    request.execution_target_digest,
                    list(request.excluded_model_ids),
                    list(request.excluded_execution_identities),
                    decision.status.value,
                    decision.primary_model_id,
                    decision.fallback_model_id,
                    decision.evidence_digest,
                    decided_at,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                cursor.execute(
                    "select id, role_policy_id, project_context_id, execution_target_id"
                    " from models.model_route_decision"
                    " where realm_id = %s and evidence_digest = %s",
                    (self.realm_id, decision.evidence_digest),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise ConcurrencyConflict("Route decision concurrent replay kayboldu")
                if (
                    UUID(str(existing[1])) != role_policy_id
                    or _uuid(existing[2]) != (project_context_id)
                    or _uuid(existing[3]) != execution_target_id
                ):
                    raise ConcurrencyConflict("Route decision replay scope drift")
                return UUID(str(existing[0])), False
            ranked = sorted(
                (item for item in decision.candidates if not item.rejection_reasons),
                key=lambda item: (-item.score, item.model_id),
            )
            ranks = {item.model_id: index for index, item in enumerate(ranked, start=1)}
            for item in decision.candidates:
                cursor.execute(
                    "insert into models.model_route_candidate"
                    " (id, realm_id, decision_id, model_id, disposition, score, layer_scores,"
                    " evidence_digests, rejection_reasons, rank)"
                    " values (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)",
                    (
                        new_uuid7(now=decided_at),
                        self.realm_id,
                        record_id,
                        item.model_id,
                        item.disposition.value,
                        item.score,
                        canonical_json({layer.value: score for layer, score in item.layer_scores}),
                        list(item.evidence_digests),
                        list(item.rejection_reasons),
                        ranks.get(item.model_id),
                    ),
                )
        return record_id, True

    def decision(self, decision_id: UUID) -> StoredRouteDecision:
        return self._load_decision("d.id = %s", (decision_id,))

    def decision_by_evidence(self, evidence_digest: str) -> StoredRouteDecision | None:
        try:
            return self._load_decision("d.evidence_digest = %s", (evidence_digest,))
        except NotFound:
            return None

    def latest_decision(
        self,
        role: AgentRole,
        target_layer: RoutingLayer,
        *,
        project_id: UUID | None = None,
        workload: str | None = None,
        technology: str | None = None,
    ) -> StoredRouteDecision | None:
        try:
            project_predicate = (
                " and d.project_id is null" if project_id is None else " and d.project_id = %s"
            )
            scope_parameters: tuple[Any, ...] = (
                (role.value, target_layer.value)
                if project_id is None
                else (role.value, target_layer.value, project_id)
            )
            workload_predicate = (
                " and d.workload is null and d.technology is null"
                if workload is None and technology is None
                else " and d.workload = %s and d.technology = %s"
            )
            if (workload is None) != (technology is None):
                raise NotFound("Route workload/technology scope eksik")
            parameters = (
                scope_parameters if workload is None else (*scope_parameters, workload, technology)
            )
            return self._load_decision(
                "d.role = %s and d.target_layer = %s"
                f"{project_predicate}{workload_predicate}"
                " order by d.decided_at desc, d.id desc limit 1",
                parameters,
            )
        except NotFound:
            return None

    def _load_decision(self, predicate: str, parameters: tuple[Any, ...]) -> StoredRouteDecision:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select d.id, d.role_policy_id, d.project_context_id, d.execution_target_id,"
                " d.role, d.target_layer, d.workload, d.technology, d.project_id,"
                " c.context_digest, d.inventory_digest, d.routing_policy_digest,"
                " d.policy_digest, d.execution_target_digest, d.excluded_model_ids,"
                " d.excluded_execution_identities, d.status, d.primary_model_id,"
                " d.fallback_model_id, d.evidence_digest"
                " from models.model_route_decision d"
                " left join projects.routing_context_snapshot c"
                "  on c.realm_id = d.realm_id and c.id = d.project_context_id"
                f" where d.realm_id = %s and {predicate}",
                (self.realm_id, *parameters),
            )
            row = cursor.fetchone()
            if row is None:
                raise NotFound("Route decision bulunamadi")
            cursor.execute(
                "select model_id, disposition, layer_scores, evidence_digests,"
                " rejection_reasons from models.model_route_candidate"
                " where realm_id = %s and decision_id = %s order by model_id",
                (self.realm_id, row[0]),
            )
            candidate_rows = cursor.fetchall()
        candidates = tuple(
            LayerCandidateEvidence(
                model_id=str(item[0]),
                disposition=CandidateDisposition(str(item[1])),
                layer_scores=tuple(
                    (RoutingLayer(str(layer)), float(score))
                    for layer, score in dict(item[2]).items()
                ),
                evidence_digests=tuple(str(value) for value in item[3]),
                rejection_reasons=tuple(str(value) for value in item[4]),
            )
            for item in candidate_rows
        )
        request = LayeredRouteRequest(
            role=AgentRole(str(row[4])),
            target_layer=RoutingLayer(str(row[5])),
            workload=str(row[6]) if row[6] is not None else None,
            technology=str(row[7]) if row[7] is not None else None,
            project_id=_uuid(row[8]),
            project_context_digest=str(row[9]) if row[9] is not None else None,
            inventory_digest=str(row[10]),
            routing_policy_digest=str(row[11]),
            policy_digest=str(row[12]),
            execution_target_digest=str(row[13]),
            excluded_model_ids=tuple(str(value) for value in row[14]),
            excluded_execution_identities=tuple(str(value) for value in row[15]),
        )
        decision = LayeredModelDecision(
            request=request,
            policy_digest=str(row[11]),
            status=RouteStatus(str(row[16])),
            primary_model_id=str(row[17]) if row[17] is not None else None,
            fallback_model_id=str(row[18]) if row[18] is not None else None,
            candidates=candidates,
            evidence_digest=str(row[19]),
        )
        return StoredRouteDecision(
            id=UUID(str(row[0])),
            role_policy_id=UUID(str(row[1])),
            project_context_id=_uuid(row[2]),
            execution_target_id=_uuid(row[3]),
            decision=decision,
        )


def _uuid(value: Any) -> UUID | None:
    return None if value is None else UUID(str(value))


def _context(row: Any) -> ProjectRoutingContext:
    return ProjectRoutingContext(
        project_id=UUID(str(row[0])),
        source_revision_id=UUID(str(row[1])),
        source_revision=str(row[2]),
        tree_digest=str(row[3]),
        capability_profile_digest=str(row[4]),
        dependency_digest=str(row[5]),
        framework_digest=str(row[6]),
        technology_digest=str(row[7]),
        architecture_digest=str(row[8]),
        rules_digest=str(row[9]),
        suite_digest=str(row[10]),
        inventory_digest=str(row[11]),
        policy_digest=str(row[12]),
        captured_at=row[13],
        expires_at=row[14],
    )


def _policy(row: Any) -> RoleRoutingPolicy:
    return RoleRoutingPolicy(
        role=AgentRole(str(row[0])),
        target_layer=RoutingLayer(str(row[1])),
        required_layers=tuple(RoutingLayer(str(value)) for value in row[2]),
        top_k=int(row[3]),
        fallback_model_ids=tuple(str(value) for value in row[4]),
        max_cost=float(row[5]),
        max_latency_ms=float(row[6]),
        independent_from_roles=tuple(AgentRole(str(value)) for value in row[7]),
        policy_digest=str(row[8]),
    )


def _execution_target(row: Any) -> ExecutionTargetSnapshot:
    return ExecutionTargetSnapshot(
        client_id=str(row[0]),
        slot=str(row[1]),
        execution_mode=str(row[2]),
        model_selectable=bool(row[3]),
        structured_result=bool(row[4]),
        cancellation=bool(row[5]),
        max_concurrency=int(row[6]),
        cost_evidence_digest=str(row[7]),
        capability_digest=str(row[8]),
        captured_at=row[9],
        expires_at=row[10],
    )


def _qualification(row: Any) -> RoutingQualification:
    return RoutingQualification(
        model_id=str(row[0]),
        layer=RoutingLayer(str(row[1])),
        role=AgentRole(str(row[2])),
        suite_digest=str(row[3]),
        aggregate_id=UUID(str(row[4])),
        aggregate_evidence_digest=str(row[5]),
        health_result_id=UUID(str(row[6])),
        health_evidence_digest=str(row[7]),
        inventory_digest=str(row[8]),
        policy_digest=str(row[9]),
        verifier_model_id=str(row[10]),
        verifier_execution_identity=str(row[11]),
        tested_execution_identity=str(row[12]),
        score=float(row[13]),
        mean_latency_ms=float(row[14]),
        mean_cost=float(row[15]),
        workload=str(row[16]) if row[16] is not None else None,
        technology=str(row[17]) if row[17] is not None else None,
        project_context_digest=str(row[18]) if row[18] is not None else None,
        qualified=bool(row[19]),
        unsafe=bool(row[20]),
        valid_from=row[21],
        expires_at=row[22],
    )
