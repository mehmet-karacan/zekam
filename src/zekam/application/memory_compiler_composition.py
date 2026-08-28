"""Production composition for the deterministic Memory Compiler worker."""

from __future__ import annotations

import datetime as dt
from typing import Any, cast
from uuid import UUID

from zekam.application.memory_candidate_compiler import CompilerBudget, MemoryCandidateCompiler
from zekam.application.memory_continuity_orchestrator import (
    MemoryContinuityOrchestrator,
    MemoryLearningRepository,
)
from zekam.application.memory_policy import load_memory_policy
from zekam.application.worker import ScheduledHandler
from zekam.infrastructure.postgres.memory_continuity_repository import (
    MemoryContinuityRepository,
)


def compose_memory_candidate_compile_handler(
    *, connection: Any, realm_id: UUID
) -> ScheduledHandler:
    """Bind the existing scheduler to a provider-free candidate-only compiler."""

    orchestrator = MemoryContinuityOrchestrator(
        repository=cast(MemoryLearningRepository, MemoryContinuityRepository(connection, realm_id)),
        compiler=MemoryCandidateCompiler(CompilerBudget(max_model_calls=0)),
        policy=load_memory_policy(),
    )

    def compile_candidates(now: dt.datetime) -> str:
        return orchestrator.compile_due(now=now).detail()

    return compile_candidates
