"""PostgreSQL model benchmark repository'leri."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json
from zekam.domain.errors import ConcurrencyConflict, NotFound
from zekam.domain.identifiers import new_uuid7
from zekam.domain.model_benchmark import (
    HARD_GATE_ORDER,
    BenchmarkAggregate,
    BenchmarkPlan,
    BenchmarkSuite,
    CandidateGate,
    DecisionRequirements,
    DeliberationBudget,
    DeliberationResult,
    FixtureRegistry,
    ModelCandidate,
    ModelDecision,
    QuotaObservation,
    QuotaPool,
    QuotaTrust,
    RuntimeObservation,
    TrialResult,
    TrialStatus,
    VerifierVerdict,
    benchmark_effect_digest,
    benchmark_verifier_effect_digest,
)


def benchmark_effect_digest_for_plan(
    cursor: Any, plan_id: UUID, fixture_digest: str, repetition: int
) -> str:
    cursor.execute("select plan_digest from models.benchmark_plan where id = %s", (plan_id,))
    row = cursor.fetchone()
    if row is None:
        raise NotFound("Benchmark plan bulunamadi")
    return benchmark_effect_digest(str(row[0]), fixture_digest, repetition)


def _efficiency(observed: float, maximum: float) -> float:
    if maximum == 0:
        return 1.0 if observed == 0 else 0.0
    if observed == float("inf"):
        return 0.0
    return max(0.0, min(1.0, 1.0 - observed / maximum))


@dataclass(frozen=True, slots=True)
class BenchmarkRepository:
    connection: Any
    realm_id: UUID

    def _plan_digest(self, cursor: Any, plan_id: UUID) -> str:
        cursor.execute(
            "select plan_digest from models.benchmark_plan where id = %s and realm_id = %s",
            (plan_id, self.realm_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise NotFound("Benchmark plan bulunamadi")
        return str(row[0])

    def _plan_model_id(self, cursor: Any, plan_id: UUID) -> str:
        cursor.execute(
            "select model_id from models.benchmark_plan where id = %s and realm_id = %s",
            (plan_id, self.realm_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise NotFound("Benchmark plan bulunamadi")
        return str(row[0])

    def _verifier_provenance(self, cursor: Any, claim_id: UUID) -> str:
        cursor.execute(
            "select adapter_digest from runtime.effect_claim where id = %s and realm_id = %s",
            (claim_id, self.realm_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise NotFound("Verifier claim bulunamadi")
        return str(row[0])

    def ensure_plan(
        self, *, registry: FixtureRegistry, suite: BenchmarkSuite, plan: BenchmarkPlan
    ) -> tuple[UUID, bool]:
        """Suite ve plan'i idempotent ekler; bool yeni plan olustugunu belirtir."""
        suite_record_id = new_uuid7()
        plan_record_id = new_uuid7()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.benchmark_fixture_registry"
                " (id, realm_id, schema_version, fixtures, registry_digest)"
                " values (%s, %s, %s, %s::jsonb, %s)"
                " on conflict (realm_id, registry_digest) do nothing",
                (
                    new_uuid7(),
                    self.realm_id,
                    registry.schema_version,
                    canonical_json([item.as_dict() for item in registry.fixtures]),
                    registry.registry_digest,
                ),
            )
            cursor.execute(
                "insert into models.benchmark_suite"
                " (id, realm_id, suite_id, suite_version, suite_kind, project_id,"
                "  capability_profile_digest, fixture_registry_digest, fixture_digests,"
                "  suite_digest)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, suite_digest) do nothing returning id",
                (
                    suite_record_id,
                    self.realm_id,
                    suite.suite_id,
                    suite.version,
                    suite.kind.value,
                    suite.project_id,
                    suite.capability_profile_digest,
                    registry.registry_digest,
                    list(suite.fixture_digests),
                    suite.suite_digest,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    "select id from models.benchmark_suite where suite_digest = %s",
                    (suite.suite_digest,),
                )
                row = cursor.fetchone()
            if row is None:  # pragma: no cover
                raise NotFound("Benchmark suite kaydedilemedi")
            canonical_suite_id = UUID(str(row[0]))
            cursor.execute(
                "insert into models.benchmark_plan"
                " (id, realm_id, suite_id, model_id, repetitions, inventory_digest,"
                "  policy_digest, fixture_registry_digest, plan_digest, remote_execution)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, plan_digest) do nothing returning id",
                (
                    plan_record_id,
                    self.realm_id,
                    canonical_suite_id,
                    plan.model_id,
                    plan.repetitions,
                    plan.inventory_digest,
                    plan.policy_digest,
                    plan.fixture_registry_digest,
                    plan.plan_digest,
                    plan.remote_execution,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                return UUID(str(inserted[0])), True
            cursor.execute(
                "select id from models.benchmark_plan where plan_digest = %s",
                (plan.plan_digest,),
            )
            existing = cursor.fetchone()
        if existing is None:  # pragma: no cover
            raise NotFound("Benchmark plan kaydedilemedi")
        return UUID(str(existing[0])), False

    def trial_receipt_matches(
        self,
        *,
        plan_id: UUID,
        tested_claim_id: UUID,
        verifier_claim_id: UUID,
        verdict: VerifierVerdict,
        result: TrialResult,
    ) -> bool:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select exists ("
                " select 1 from models.benchmark_plan p"
                " join runtime.effect_claim c on c.realm_id = p.realm_id"
                " join runtime.effect_receipt r on r.claim_id = c.id and r.realm_id = c.realm_id"
                " where p.id = %s and p.realm_id = %s and c.id = %s"
                " and c.operation = 'model-benchmark-tested' and c.effect_digest = %s"
                " and r.status = 'completed' and r.result_digest = %s)"
                " and exists (select 1 from models.benchmark_plan p"
                " join runtime.effect_claim c on c.realm_id = p.realm_id"
                " join runtime.effect_receipt r on r.claim_id = c.id and r.realm_id = c.realm_id"
                " where p.id = %s and p.realm_id = %s and c.id = %s"
                " and c.operation = 'model-benchmark-verifier' and c.effect_digest = %s"
                " and c.adapter_digest = %s and r.status = 'completed'"
                " and r.result_digest = %s)",
                (
                    plan_id,
                    self.realm_id,
                    tested_claim_id,
                    benchmark_effect_digest_for_plan(
                        cursor, plan_id, result.fixture_digest, result.repetition
                    ),
                    result.response_digest,
                    plan_id,
                    self.realm_id,
                    verifier_claim_id,
                    benchmark_verifier_effect_digest(
                        str(self._plan_digest(cursor, plan_id)),
                        result.fixture_digest,
                        result.repetition,
                        verdict.verifier_model_id,
                        result.response_digest,
                    ),
                    self._verifier_provenance(cursor, verifier_claim_id),
                    verdict.evidence_digest,
                ),
            )
            row = cursor.fetchone()
        return bool(row and row[0])

    def record_trial(
        self,
        *,
        plan_id: UUID,
        tested_claim_id: UUID,
        verifier_claim_id: UUID,
        verdict: VerifierVerdict,
        result: TrialResult,
        observed_at: dt.datetime | None = None,
    ) -> tuple[UUID, bool]:
        moment = observed_at or dt.datetime.now(dt.UTC)
        record_id = new_uuid7(now=moment)
        with self.connection.cursor() as cursor:
            if result.verifier_approved != verdict.approved:
                raise ConcurrencyConflict(
                    "Trial verifier approval canonical verdict ile eslesmiyor"
                )
            cursor.execute(
                "insert into models.benchmark_verifier_result"
                " (id, realm_id, claim_id, tested_model_id, verifier_model_id,"
                "  execution_identity, tested_response_digest, approved, evidence_digest)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, claim_id) do nothing",
                (
                    new_uuid7(),
                    self.realm_id,
                    verifier_claim_id,
                    verdict.tested_model_id,
                    verdict.verifier_model_id,
                    verdict.execution_identity,
                    verdict.tested_response_digest,
                    verdict.approved,
                    verdict.evidence_digest,
                ),
            )
            cursor.execute(
                "insert into models.benchmark_trial"
                " (id, realm_id, plan_id, tested_claim_id, verifier_claim_id, tested_model_id,"
                "  verifier_model_id, verifier_execution_identity, verifier_provenance_digest,"
                "  verifier_evidence_digest, fixture_digest, repetition, status,"
                "  parse_ok, format_ok,"
                "  evidence_ok, verifier_approved, quality, reliability, latency_ms, input_tokens,"
                "  output_tokens, retry_count, human_corrections, estimated_cost, actual_cost,"
                "  response_digest, evidence_digest, failure_category, observed_at)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                " %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " on conflict (realm_id, plan_id, fixture_digest, repetition)"
                " do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    plan_id,
                    tested_claim_id,
                    verifier_claim_id,
                    self._plan_model_id(cursor, plan_id),
                    verdict.verifier_model_id,
                    verdict.execution_identity,
                    self._verifier_provenance(cursor, verifier_claim_id),
                    verdict.evidence_digest,
                    result.fixture_digest,
                    result.repetition,
                    result.status.value,
                    result.parse_ok,
                    result.format_ok,
                    result.evidence_ok,
                    result.verifier_approved,
                    result.quality,
                    result.reliability,
                    result.latency_ms,
                    result.input_tokens,
                    result.output_tokens,
                    result.retry_count,
                    result.human_corrections,
                    result.estimated_cost,
                    result.actual_cost,
                    result.response_digest,
                    result.evidence_digest,
                    result.failure_category,
                    moment,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is not None:
                return UUID(str(inserted[0])), True
            cursor.execute(
                "select id, tested_claim_id, verifier_claim_id, evidence_digest"
                " from models.benchmark_trial"
                " where plan_id = %s and fixture_digest = %s and repetition = %s",
                (plan_id, result.fixture_digest, result.repetition),
            )
            existing = cursor.fetchone()
        if existing is None:  # pragma: no cover
            raise NotFound("Benchmark trial kaydedilemedi")
        if (
            UUID(str(existing[1])) != tested_claim_id
            or UUID(str(existing[2])) != verifier_claim_id
            or existing[3] != result.evidence_digest
        ):
            raise ConcurrencyConflict("Ayni repetition farkli claim veya evidence ile kayitli")
        return UUID(str(existing[0])), False

    def list_trials(self, plan_id: UUID) -> tuple[TrialResult, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select fixture_digest, repetition, status, parse_ok, format_ok, evidence_ok,"
                " verifier_approved,"
                " quality, reliability, latency_ms, input_tokens, output_tokens, retry_count,"
                " human_corrections, estimated_cost, actual_cost, response_digest, evidence_digest,"
                " failure_category from models.benchmark_trial where plan_id = %s"
                " order by repetition",
                (plan_id,),
            )
            rows = cursor.fetchall()
        return tuple(
            TrialResult(
                fixture_digest=row[0],
                repetition=row[1],
                status=TrialStatus(row[2]),
                parse_ok=row[3],
                format_ok=row[4],
                evidence_ok=row[5],
                verifier_approved=row[6],
                quality=row[7],
                reliability=row[8],
                latency_ms=row[9],
                input_tokens=row[10],
                output_tokens=row[11],
                retry_count=row[12],
                human_corrections=row[13],
                estimated_cost=row[14],
                actual_cost=row[15],
                response_digest=row[16],
                evidence_digest=row[17],
                failure_category=row[18],
            )
            for row in rows
        )

    def store_aggregate(self, *, plan_id: UUID, aggregate: BenchmarkAggregate) -> UUID:
        record_id = new_uuid7()
        metrics = aggregate.as_dict()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.benchmark_aggregate"
                " (id, realm_id, plan_id, tested_model_id, verifier_model_id,"
                "  verifier_execution_identity, verifier_provenance_digest, approved, unsafe,"
                "  metrics, evidence_digest)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)"
                " on conflict (realm_id, plan_id) do nothing returning id",
                (
                    record_id,
                    self.realm_id,
                    plan_id,
                    aggregate.tested_model_id,
                    aggregate.verifier_model_id,
                    aggregate.verifier_execution_identity,
                    aggregate.verifier_provenance_digest,
                    aggregate.approved,
                    aggregate.unsafe,
                    canonical_json(metrics),
                    aggregate.evidence_digest,
                ),
            )
            row = cursor.fetchone()
            if row is not None:
                return UUID(str(row[0]))
            cursor.execute(
                "select id, evidence_digest from models.benchmark_aggregate where plan_id = %s",
                (plan_id,),
            )
            existing = cursor.fetchone()
        if existing is None:  # pragma: no cover
            raise NotFound("Benchmark aggregate kaydedilemedi")
        if existing[1] != aggregate.evidence_digest:
            raise ConcurrencyConflict("Benchmark aggregate evidence drift")
        return UUID(str(existing[0]))

    def record_quota(self, observation: QuotaObservation) -> UUID:
        record_id = new_uuid7(now=observation.observed_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.quota_observation"
                " (id, realm_id, quota_pool, trust, remaining_ratio, source_digest, observed_at)"
                " values (%s, %s, %s, %s, %s, %s, %s)",
                (
                    record_id,
                    self.realm_id,
                    observation.pool.value,
                    observation.trust.value,
                    observation.remaining_ratio,
                    observation.source_digest,
                    observation.observed_at,
                ),
            )
        return record_id

    def bind_quota_pool(self, *, model_id: str, pool: QuotaPool, evidence_digest: str) -> UUID:
        record_id = new_uuid7()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.model_quota_pool_binding"
                " (id, realm_id, model_id, quota_pool, evidence_digest)"
                " values (%s, %s, %s, %s, %s)",
                (record_id, self.realm_id, model_id, pool.value, evidence_digest),
            )
        return record_id

    def load_quota_observations(self) -> tuple[QuotaObservation, ...]:
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select quota_pool, trust, remaining_ratio, source_digest, observed_at"
                " from models.quota_observation order by observed_at"
            )
            rows = cursor.fetchall()
        return tuple(
            QuotaObservation(
                pool=QuotaPool(row[0]),
                trust=QuotaTrust(row[1]),
                remaining_ratio=row[2],
                source_digest=row[3],
                observed_at=row[4],
            )
            for row in rows
        )

    def load_decision_candidates(
        self, requirements: DecisionRequirements
    ) -> tuple[ModelCandidate, ...]:
        """Aday gate ve skorlarini yalniz kanonik model/benchmark/runtime ledger'dan kurar."""
        with self.connection.cursor() as cursor:
            cursor.execute(
                "select m.model_id, b.quota_pool, b.evidence_digest, m.inventory_digest,"
                " m.enabled, m.health_state, m.last_health_inventory_digest, m.modality,"
                " m.endpoint_scope, m.capabilities_verified, a.approved, a.evidence_digest,"
                " a.metrics, a.project_id, a.inventory_digest, a.policy_digest,"
                " a.capability_profile_digest, a.fixture_registry_digest, a.suite_digest,"
                " coalesce(o.success_ratio, 0), coalesce(o.correction_average, 1),"
                " o.latest_observed_at, o.evidence_digests, cp.profile_digest,"
                " pol.policy_digest, fr.registry_digest, m.last_health_policy_digest,"
                " m.last_health_at, pol.effective_from"
                " from models.model_inventory m"
                " join models.model_quota_pool_binding b"
                "   on b.realm_id = m.realm_id and b.model_id = m.model_id"
                " left join lateral ("
                "   select ba.approved, ba.evidence_digest, ba.metrics, bp.inventory_digest,"
                "          bp.policy_digest, bs.capability_profile_digest,"
                "          bp.fixture_registry_digest, bs.suite_digest, bs.project_id"
                "   from models.benchmark_aggregate ba"
                "   join models.benchmark_plan bp on bp.id = ba.plan_id"
                "     and bp.realm_id = ba.realm_id"
                "   join models.benchmark_suite bs on bs.id = bp.suite_id"
                "     and bs.realm_id = bp.realm_id"
                "   where ba.tested_model_id = m.model_id and bs.project_id = %s"
                "   order by ba.created_at desc limit 1"
                " ) a on true"
                " left join lateral ("
                "   select avg(case when outcome = 'succeeded' then 1.0 else 0.0 end)"
                "          success_ratio,"
                "          avg(human_corrections::double precision) correction_average"
                "          , max(observed_at) latest_observed_at,"
                "          array_agg(evidence_digest order by observed_at) evidence_digests"
                "   from models.runtime_observation ro where ro.model_id = m.model_id"
                "     and ro.workload = %s"
                " ) o on true"
                " left join lateral (select profile_digest from projects.capability_profile"
                "   where realm_id = m.realm_id and project_id::text = %s"
                "   order by generated_at desc limit 1) cp on true"
                " left join lateral (select policy_digest, effective_from from security.policy"
                "   where realm_id = m.realm_id and effective_from <= now()"
                "   order by effective_from desc, revision desc limit 1) pol on true"
                " left join lateral (select registry_digest"
                "   from models.benchmark_fixture_registry where realm_id = m.realm_id"
                "   order by created_at desc limit 1) fr on true order by m.model_id",
                (requirements.project_id, requirements.workload, requirements.project_id),
            )
            rows = cursor.fetchall()
        candidates: list[ModelCandidate] = []
        for row in rows:
            metrics = row[12] or {}
            latency = float(metrics.get("latency_ms", {}).get("p95", float("inf")))
            cost = float(metrics.get("cost", {}).get("p95", float("inf")))
            tokens = float(metrics.get("token_count", {}).get("p95", float("inf")))
            quality = float(metrics.get("quality", {}).get("mean", 0))
            reliability = float(metrics.get("reliability", {}).get("mean", 0))
            capabilities = set(row[9] or ())
            required = set(requirements.required_capabilities)
            benchmark_current = bool(
                row[10]
                and row[13] == requirements.project_id
                and row[14] == row[3]
                and row[15] == row[24]
                and row[16] == row[23]
                and row[17] == row[25]
                and row[18]
                and row[11]
                and row[21] is not None
                and row[21] >= dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
            )
            gates = dict.fromkeys(HARD_GATE_ORDER, True)
            gates[CandidateGate.ENABLED] = bool(row[4])
            gates[CandidateGate.HEALTH] = (
                row[5]
                in {
                    "benchmark-eligible",
                    "project-qualified",
                    "active-candidate",
                }
                and row[6] == row[3]
                and row[26] == row[24]
                and row[27] is not None
                and row[28] is not None
                and row[27] >= row[28]
                and row[27] >= dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
            )
            gates[CandidateGate.SUPPORT] = (
                row[7] == requirements.modality
                and f"workload:{requirements.workload}" in capabilities
                and f"client:{requirements.client}" in capabilities
            )
            gates[CandidateGate.PROJECT_BENCHMARK] = benchmark_current
            gates[CandidateGate.SECURITY] = (
                not requirements.local_data_required or row[8] == "local"
            )
            gates[CandidateGate.REQUIREMENTS] = required <= capabilities
            gates[CandidateGate.VERIFIER_EXCLUSION] = row[0] != requirements.verifier_model_id
            gates[CandidateGate.BUDGET] = (
                latency <= requirements.max_latency_ms
                and cost <= requirements.max_cost
                and tokens <= requirements.max_tokens
            )
            gates[CandidateGate.QUOTA] = True
            evidence = [str(row[2]), str(row[3]), requirements.evidence_digest]
            evidence.extend(
                str(value)
                for value in (
                    row[11],
                    row[15],
                    row[16],
                    row[17],
                    row[18],
                    row[26],
                    *(row[22] or ()),
                )
                if value
            )
            candidates.append(
                ModelCandidate(
                    model_id=row[0],
                    quota_pool=QuotaPool(row[1]),
                    evidence_digests=tuple(evidence),
                    gates=gates,
                    quality=quality,
                    reliability=reliability,
                    project_specialization=1.0 if benchmark_current else 0.0,
                    observed_success=float(row[19]),
                    latency_efficiency=_efficiency(latency, requirements.max_latency_ms),
                    token_efficiency=_efficiency(tokens, requirements.max_tokens),
                    cost_efficiency=_efficiency(cost, requirements.max_cost),
                    correction_efficiency=1 / (1 + float(row[20])),
                )
            )
        return tuple(candidates)

    def store_decision(self, decision: ModelDecision) -> UUID:
        record_id = new_uuid7()
        candidates = [
            {
                "model_id": row.model_id,
                "quota_pool": row.quota_pool.value,
                "score": row.score,
                "gates": {gate.value: row.gates[gate] for gate in row.gates},
                "evidence_digests": list(row.evidence_digests),
            }
            for row in decision.candidates
        ]
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.model_decision"
                " (id, realm_id, selected_model_id, selected_score, candidates, rejected,"
                " evidence_digest, authority_granted)"
                " values (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, false)",
                (
                    record_id,
                    self.realm_id,
                    decision.selected_model_id,
                    decision.selected_score,
                    canonical_json(candidates),
                    canonical_json(decision.rejected),
                    decision.evidence_digest,
                ),
            )
        return record_id

    def record_runtime_observation(self, observation: RuntimeObservation) -> UUID:
        record_id = new_uuid7(now=observation.observed_at)
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.runtime_observation"
                " (id, realm_id, model_id, workload, outcome, latency_ms, input_tokens,"
                " output_tokens, cost, human_corrections, evidence_digest, authority_granted,"
                " observed_at) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false, %s)",
                (
                    record_id,
                    self.realm_id,
                    observation.model_id,
                    observation.workload,
                    observation.outcome.value,
                    observation.latency_ms,
                    observation.input_tokens,
                    observation.output_tokens,
                    observation.cost,
                    observation.human_corrections,
                    observation.evidence_digest,
                    observation.observed_at,
                ),
            )
        return record_id

    def store_deliberation(self, *, budget: DeliberationBudget, result: DeliberationResult) -> UUID:
        record_id = new_uuid7()
        with self.connection.cursor() as cursor:
            cursor.execute(
                "insert into models.deliberation_result"
                " (id, realm_id, question_digest, evidence_packet_digest, max_rounds, max_seconds,"
                " max_tokens, max_cost, max_evidence_items, consensus_digests,"
                " contradiction_digests, synthesizer_identity, review_required, authority_granted)"
                " values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false)",
                (
                    record_id,
                    self.realm_id,
                    result.question_digest,
                    result.evidence_packet_digest,
                    budget.max_rounds,
                    budget.max_seconds,
                    budget.max_tokens,
                    budget.max_cost,
                    budget.max_evidence_items,
                    list(result.consensus_digests),
                    list(result.contradiction_digests),
                    result.synthesizer_identity,
                    result.human_or_verifier_review_required,
                ),
            )
        return record_id
