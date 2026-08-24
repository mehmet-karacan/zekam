"""Context manifest ve transcript-free continuity orchestration."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from zekam.application.context_ranking import ContextCandidateSet, ContextRankingSnapshot
from zekam.application.context_recipe import (
    ContextRecipeRegistry,
    ContextRecipeRole,
    RecipeContextPacket,
)
from zekam.domain.context_continuity import (
    AuthorityLevel,
    Checkpoint,
    ContinuitySnapshot,
    FinalizedHandoff,
    validate_resume,
)

if TYPE_CHECKING:
    from zekam.infrastructure.postgres.context_ranking_repository import (
        ContextRankingRepository,
    )


@dataclass(frozen=True, slots=True)
class ResumeInstructions:
    client: str
    model_ref: str
    first_reads: tuple[str, ...]
    next_safe_actions: tuple[str, ...]
    evidence_digests: tuple[str, ...]
    reacquire_work: bool = True
    transcript_used: bool = False
    grants_authority: bool = False


@dataclass(frozen=True, slots=True)
class ContextContinuityService:
    recipe_registry: ContextRecipeRegistry = field(default_factory=ContextRecipeRegistry)

    def compile(
        self,
        candidate_set: ContextCandidateSet,
        *,
        role: ContextRecipeRole,
        token_budget: int,
        minimum_authority: AuthorityLevel,
        now: dt.datetime,
        ranking_snapshot: ContextRankingSnapshot,
        repository: ContextRankingRepository,
    ) -> RecipeContextPacket:
        del now
        return repository.compile_current(
            ranking_snapshot,
            candidate_set,
            role=role,
            token_budget=token_budget,
            minimum_authority=minimum_authority,
        )

    def resume(
        self,
        *,
        handoff: FinalizedHandoff,
        snapshot: ContinuitySnapshot,
        checkpoint: Checkpoint,
        current_source_revision: str,
    ) -> ResumeInstructions:
        validate_resume(
            handoff,
            snapshot,
            checkpoint,
            current_source_revision=current_source_revision,
        )
        return ResumeInstructions(
            client=handoff.to_client,
            model_ref=handoff.to_model_ref,
            first_reads=snapshot.first_reads,
            next_safe_actions=snapshot.next_safe_actions,
            evidence_digests=tuple(item.evidence_digest for item in snapshot.evidence_refs),
        )
