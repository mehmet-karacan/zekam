"""Fail-closed boundary for product surfaces not yet backed by a local adapter."""

from __future__ import annotations

from typing import Any, NoReturn

from zekam.domain.errors import PolicyViolation


class LocalCapabilityUnavailableError(Exception):
    """Compatibility exception used only by existing command error branches."""

    sqlstate: str | None = None


def unavailable(*_args: object, **_kwargs: object) -> NoReturn:
    """Reject a command before it can perform an external or legacy effect."""
    raise PolicyViolation("This capability has no active local implementation")


class _Unavailable:
    def __getattr__(self, _name: str) -> Any:
        return self

    def __call__(self, *_args: object, **_kwargs: object) -> NoReturn:
        unavailable()


UNAVAILABLE: Any = _Unavailable()

# Transitional names keep historical command modules importable while each command is
# rewired to a local adapter. Calling any of these values fails before an effect.
PSYCOPG_AVAILABLE = False
Error = LocalCapabilityUnavailableError
Connection: Any = UNAVAILABLE
dict_row: Any = UNAVAILABLE
migrations: Any = UNAVAILABLE
routine_integrity: Any = UNAVAILABLE
connect: Any = UNAVAILABLE
session: Any = UNAVAILABLE
read_server_info: Any = UNAVAILABLE
ServerInfo: Any = UNAVAILABLE
ActiveWorkProjectionRepository: Any = UNAVAILABLE
ActorRepository: Any = UNAVAILABLE
AuthorizationRepository: Any = UNAVAILABLE
BenchmarkRepository: Any = UNAVAILABLE
ClientLifecycleRepository: Any = UNAVAILABLE
ConfigProvenanceRepository: Any = UNAVAILABLE
ContextContinuityRepository: Any = UNAVAILABLE
ExecutionRunRepository: Any = UNAVAILABLE
HealthReportRepository: Any = UNAVAILABLE
IntegrationStateRepository: Any = UNAVAILABLE
JobRepository: Any = UNAVAILABLE
KnowledgeRepository: Any = UNAVAILABLE
MemoryContinuityRepository: Any = UNAVAILABLE
ModelCampaignRepository: Any = UNAVAILABLE
ModelCapabilityRepository: Any = UNAVAILABLE
ModelCapabilityRuntimeRepository: Any = UNAVAILABLE
ModelCatalogRepository: Any = UNAVAILABLE
ModelInventoryRepository: Any = UNAVAILABLE
ModelInvocationRepository: Any = UNAVAILABLE
ModelRoutingRepository: Any = UNAVAILABLE
PostgresAppNotificationRepository: Any = UNAVAILABLE
PostgresControlPlaneCompletionRepository: Any = UNAVAILABLE
PostgresDiagnosticTraceRepository: Any = UNAVAILABLE
PostgresLegacyRepositoryProvider: Any = UNAVAILABLE
PostgresMarkdownProjectionRepository: Any = UNAVAILABLE
PostgresMeasuredLoopRepository: Any = UNAVAILABLE
PostgresMemoryControlRepository: Any = UNAVAILABLE
PostgresMemoryHealthReader: Any = UNAVAILABLE
PostgresMemoryHookInstaller: Any = UNAVAILABLE
PostgresMemoryUpgradeRepository: Any = UNAVAILABLE
PostgresObservatoryProjectionReader: Any = UNAVAILABLE
PostgresRealmSessionOperations: Any = UNAVAILABLE
ProjectRepository: Any = UNAVAILABLE
ProjectResolver: Any = UNAVAILABLE
ProjectionClosureRepository: Any = UNAVAILABLE
RealmRepository: Any = UNAVAILABLE
ResearchRepository: Any = UNAVAILABLE
ResumeRepository: Any = UNAVAILABLE
RetrievalRepository: Any = UNAVAILABLE
SecretRefRepository: Any = UNAVAILABLE
compose_model_health_service: Any = UNAVAILABLE
