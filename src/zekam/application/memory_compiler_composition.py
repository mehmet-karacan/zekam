"""Production composition for the deterministic Memory Compiler worker."""

from __future__ import annotations

import datetime as dt

from zekam.application.memory_candidate_compiler import CompilerBudget, MemoryCandidateCompiler
from zekam.application.memory_continuity_orchestrator import (
    MemoryContinuityOrchestrator,
    MemoryLearningRepository,
)
from zekam.application.memory_policy import load_memory_policy
from zekam.application.worker import ScheduledHandler


def compose_memory_candidate_compile_handler(
    *, repository: MemoryLearningRepository
) -> ScheduledHandler:
    """Bind the existing scheduler to a provider-free candidate-only compiler."""

    orchestrator = MemoryContinuityOrchestrator(
        repository=repository,
        compiler=MemoryCandidateCompiler(CompilerBudget(max_model_calls=0)),
        policy=load_memory_policy(),
    )

    def compile_candidates(now: dt.datetime) -> str:
        return orchestrator.compile_due(now=now).detail()

    return compile_candidates
