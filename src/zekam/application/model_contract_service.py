"""Provider adapter'larindan gelen observation'lari nicel contract kanitina cevirir."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from zekam.application.model_health_service import ModelHealthService
from zekam.domain.errors import ValidationFailed
from zekam.domain.model_contract import (
    ContractEvaluation,
    ContractObservation,
    ContractThresholds,
    evaluate_observation,
)
from zekam.domain.model_inventory import ModelRecord


class ModelContractAdapter(Protocol):
    """Authorization/outbound/receipt zinciri disarida tamamlanmis adapter."""

    def observe(self, record: ModelRecord) -> ContractObservation: ...


@dataclass(frozen=True, slots=True)
class ModelContractRunner:
    """Caller-supplied verified bayragi kabul etmeyen evaluator runner."""

    health: ModelHealthService
    adapter: ModelContractAdapter
    thresholds: ContractThresholds = field(default_factory=ContractThresholds)

    def run(self, model_id: str) -> ContractEvaluation:
        record = self.health.inventory.get(model_id)
        observation = self.adapter.observe(record)
        if observation.modality is not record.modality:
            raise ValidationFailed("contract observation model modalitesiyle eslesmiyor")
        if not observation.fixture_digest or not observation.response_digest:
            raise ValidationFailed("contract observation fixture/response digest ister")
        evaluation = evaluate_observation(observation, self.thresholds)
        self.health.record_capability(
            model_id,
            capability=evaluation.capability,
            verified=evaluation.verified,
            evidence=evaluation.evidence_digest,
        )
        return evaluation
