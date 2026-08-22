"""Model Decision ve runtime observation uygulama servisi."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from zekam.domain.model_benchmark import (
    DecisionRequirements,
    ModelDecision,
    RuntimeObservation,
    decide_model,
)
from zekam.infrastructure.postgres.model_benchmark_repository import BenchmarkRepository


@dataclass(frozen=True, slots=True)
class ModelDecisionService:
    repository: BenchmarkRepository

    def decide(
        self,
        requirements: DecisionRequirements,
    ) -> tuple[UUID, ModelDecision]:
        """Hard gate'leri kanonik repository evidence'inden kurup kaydeder."""
        candidates = self.repository.load_decision_candidates(requirements)
        observations = self.repository.load_quota_observations()
        decision = decide_model(candidates, observations)
        return self.repository.store_decision(decision), decision

    def observe(self, observation: RuntimeObservation) -> UUID:
        """Route scoring icin derived runtime kaniti kaydeder."""
        return self.repository.record_runtime_observation(observation)
