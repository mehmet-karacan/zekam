"""PostgreSQL adapter composition for the application model-health ports."""

from __future__ import annotations

from typing import Any

from zekam.application.model_health_service import ModelHealthService, ProviderProbe
from zekam.domain.model_health import QuarantinePolicy
from zekam.domain.realm import Realm
from zekam.infrastructure.postgres.model_repository import (
    CapabilityCheckRepository,
    HealthProbeRepository,
    ModelInventoryRepository,
    QuarantineRepository,
)


def compose_model_health_service(
    connection: Any,
    realm: Realm,
    *,
    probe: ProviderProbe,
    policy: QuarantinePolicy | None = None,
) -> ModelHealthService:
    return ModelHealthService(
        inventory=ModelInventoryRepository(connection, realm.id),
        probes=HealthProbeRepository(connection, realm.id),
        capabilities=CapabilityCheckRepository(connection, realm.id),
        quarantine=QuarantineRepository(connection, realm.id),
        probe=probe,
        policy=policy or QuarantinePolicy(),
    )
