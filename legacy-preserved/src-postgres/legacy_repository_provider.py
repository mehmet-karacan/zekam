"""PostgreSQL implementation of the historical repository provider port."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.errors import ConfigurationError
from zekam.infrastructure.postgres import migrations, routine_integrity
from zekam.infrastructure.postgres.agent_assignment_repository import (
    AgentAssignmentRepository,
)
from zekam.infrastructure.postgres.client_lifecycle_repository import (
    ClientLifecycleRepository,
)
from zekam.infrastructure.postgres.connection import configure_session, reset_role
from zekam.infrastructure.postgres.context_continuity_repository import (
    ContextContinuityRepository,
)
from zekam.infrastructure.postgres.control_plane_completion_repository import (
    PostgresControlPlaneCompletionRepository,
)
from zekam.infrastructure.postgres.core_repository import ActorRepository, EventStore, RevisionStore
from zekam.infrastructure.postgres.diagnostic_trace_repository import (
    PostgresDiagnosticTraceRepository,
)
from zekam.infrastructure.postgres.execution_run_repository import ExecutionRunRepository
from zekam.infrastructure.postgres.hook_runtime_repository import HookRuntimeRepository
from zekam.infrastructure.postgres.knowledge_repository import KnowledgeRepository
from zekam.infrastructure.postgres.lifecycle_runtime_template_repository import (
    LifecycleRuntimeTemplateRepository,
)
from zekam.infrastructure.postgres.loop_policy_repository import PostgresLoopPolicyRepository
from zekam.infrastructure.postgres.measured_loop_repository import (
    PostgresMeasuredLoopRepository,
)
from zekam.infrastructure.postgres.memory_continuity_repository import (
    MemoryContinuityRepository,
)
from zekam.infrastructure.postgres.model_repository import ModelInventoryRepository
from zekam.infrastructure.postgres.model_routing_repository import ModelRoutingRepository
from zekam.infrastructure.postgres.project_repository import (
    CapabilityProfileRepository,
    IntegrationStateRepository,
    ProjectRepository,
    SourceBindingRepository,
)
from zekam.infrastructure.postgres.projection_closure_repository import (
    ProjectionClosureRepository,
)
from zekam.infrastructure.postgres.resume_apply_repository import ResumeApplyRepository
from zekam.infrastructure.postgres.resume_repository import ResumeRepository
from zekam.infrastructure.postgres.retrieval_repository import RetrievalRepository
from zekam.infrastructure.postgres.runtime_repository import (
    EffectLedger,
    JobRepository,
    ResourceLockRepository,
)
from zekam.infrastructure.postgres.security_repository import (
    AuditRepository,
    AuthorizationRepository,
    CapabilityRepository,
    OutboundRequestRepository,
    PolicyRepository,
    SecretRefRepository,
)
from zekam.infrastructure.postgres.tool_registry_repository import ToolRegistryRepository
from zekam.infrastructure.postgres.work_repository import (
    DecisionRepository,
    IntentRepository,
    TaskPlanRepository,
    WorkItemRepository,
    WorkQueryRepository,
    WorkRelationRepository,
)

RepositoryConstructor = Callable[..., Any]

_REPOSITORIES: dict[str, RepositoryConstructor] = {
    "actor": ActorRepository,
    "agent_assignment": AgentAssignmentRepository,
    "audit": AuditRepository,
    "authorization": AuthorizationRepository,
    "capability": CapabilityRepository,
    "client_lifecycle": ClientLifecycleRepository,
    "context_continuity": ContextContinuityRepository,
    "control_plane_completion": PostgresControlPlaneCompletionRepository,
    "decision": DecisionRepository,
    "diagnostic_trace": PostgresDiagnosticTraceRepository,
    "effect_ledger": EffectLedger,
    "event": EventStore,
    "execution_run": ExecutionRunRepository,
    "hook_runtime": HookRuntimeRepository,
    "integration_state": IntegrationStateRepository,
    "intent": IntentRepository,
    "job": JobRepository,
    "knowledge": KnowledgeRepository,
    "lifecycle_runtime_template": LifecycleRuntimeTemplateRepository,
    "loop_policy": PostgresLoopPolicyRepository,
    "measured_loop": PostgresMeasuredLoopRepository,
    "memory_continuity": MemoryContinuityRepository,
    "model_inventory": ModelInventoryRepository,
    "model_routing": ModelRoutingRepository,
    "outbound_request": OutboundRequestRepository,
    "policy": PolicyRepository,
    "projection_closure": ProjectionClosureRepository,
    "project": ProjectRepository,
    "project_capability_profile": CapabilityProfileRepository,
    "resource_lock": ResourceLockRepository,
    "resume": ResumeRepository,
    "resume_apply": ResumeApplyRepository,
    "retrieval": RetrievalRepository,
    "revision": RevisionStore,
    "secret_ref": SecretRefRepository,
    "source_binding": SourceBindingRepository,
    "task_plan": TaskPlanRepository,
    "tool_registry": ToolRegistryRepository,
    "work_item": WorkItemRepository,
    "work_query": WorkQueryRepository,
    "work_relation": WorkRelationRepository,
}


@dataclass(frozen=True, slots=True)
class PostgresLegacyRepositoryProvider:
    def build(
        self,
        kind: str,
        connection: Any,
        realm_id: UUID,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        constructor = _REPOSITORIES.get(kind)
        if constructor is None:
            raise ConfigurationError(f"Bilinmeyen legacy repository portu: {kind}")
        return constructor(connection, realm_id, *args, **kwargs)

    def maintain(self, operation: str, connection: Any, *args: Any, **kwargs: Any) -> Any:
        if operation == "migration-status":
            return migrations.status(connection, *args, **kwargs)
        if operation == "migration-upgrade":
            return migrations.upgrade(connection, *args, **kwargs)
        if operation == "routine-repair":
            return routine_integrity.repair_missing_routines(connection, *args, **kwargs)
        if operation == "session-reset-role":
            return reset_role(connection)
        if operation == "session-configure":
            return configure_session(connection, *args, **kwargs)
        raise ConfigurationError(f"Bilinmeyen legacy maintenance portu: {operation}")
