"""Context manifest ve transcript-free continuity orchestration."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from zekam.application.context_ranking import ContextCandidateSet, ContextRankingSnapshot
from zekam.application.context_recipe import (
    ContextRecipeRegistry,
    ContextRecipeRole,
    RecipeContextPacket,
)
from zekam.domain.clients import ClientDescriptor
from zekam.domain.context_continuity import (
    AuthorityLevel,
    Checkpoint,
    ContinuitySnapshot,
    FinalizedHandoff,
    TargetRouteBinding,
    validate_resume,
)
from zekam.domain.errors import PolicyViolation

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


class CrossClientRouteGuard(Protocol):
    def require_current(
        self,
        decision_id: UUID,
        *,
        target_model_ref: str,
        target_client_id: str,
        project_id: str,
        at: dt.datetime | None,
    ) -> TargetRouteBinding: ...


@dataclass(frozen=True, slots=True)
class ContextContinuityService:
    recipe_registry: ContextRecipeRegistry = field(default_factory=ContextRecipeRegistry)
    route_guard: CrossClientRouteGuard | None = None

    def finalize_cross_client_handoff(
        self,
        *,
        source_client: ClientDescriptor,
        target_client: ClientDescriptor,
        source_model_ref: str,
        target_model_ref: str,
        snapshot: ContinuitySnapshot,
        checkpoint: Checkpoint,
        required_capabilities: tuple[str, ...],
        required_permissions: tuple[str, ...],
        target_route_decision_id: UUID,
        created_at: dt.datetime,
    ) -> FinalizedHandoff:
        """Capability ve fresh route kanitli transcript-free handoff uretir."""

        if source_client.client_id == target_client.client_id:
            raise PolicyViolation("Cross-client handoff iki farkli client ister")
        source_manifest = source_client.capability_manifest
        target_manifest = target_client.capability_manifest
        unsupported = target_manifest.unsupported(required_capabilities)
        source_permissions = source_client.permission_manifest
        target_permissions = target_client.permission_manifest
        unsupported_permissions = (
            required_permissions
            if target_permissions is None
            else target_permissions.unsupported(required_permissions)
        )
        if self.route_guard is None:
            raise PolicyViolation("Cross-client handoff canonical route guard ister")
        route = self.route_guard.require_current(
            target_route_decision_id,
            target_model_ref=target_model_ref,
            target_client_id=target_client.client_id,
            project_id=snapshot.project_id,
            at=created_at,
        )
        replan = (("client-capability-reroute",) if unsupported else ()) + (
            ("client-permission-reroute",)
            if unsupported_permissions or source_permissions is None or target_permissions is None
            else ()
        )
        return FinalizedHandoff(
            from_client=source_client.client_id,
            to_client=target_client.client_id,
            from_model_ref=source_model_ref,
            to_model_ref=target_model_ref,
            snapshot_digest=snapshot.snapshot_digest,
            checkpoint_digest=checkpoint.checkpoint_digest,
            source_revision=checkpoint.source_revision,
            created_at=created_at,
            source_client_capability_digest=source_manifest.capability_digest,
            target_client_capability_digest=target_manifest.capability_digest,
            source_client_permission_digest=(
                None if source_permissions is None else source_permissions.permission_digest
            ),
            target_client_permission_digest=(
                None if target_permissions is None else target_permissions.permission_digest
            ),
            unsupported_capabilities=unsupported,
            unsupported_permissions=tuple(sorted(unsupported_permissions)),
            required_replan_items=tuple(sorted(replan)),
            target_route_decision_id=route.decision_id,
            target_route_decision_digest=route.evidence_digest,
            target_route_valid_until=route.valid_until,
            target_route_fresh=True,
        )

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
        target_client: ClientDescriptor | None = None,
        observed_at: dt.datetime | None = None,
    ) -> ResumeInstructions:
        validate_resume(
            handoff,
            snapshot,
            checkpoint,
            current_source_revision=current_source_revision,
        )
        if handoff.from_client != handoff.to_client:
            if target_client is None:
                raise PolicyViolation("Cross-client resume target adapter manifest ister")
            if target_client.client_id != handoff.to_client:
                raise PolicyViolation("Cross-client resume target client binding uyusmuyor")
            if (
                target_client.capability_manifest.capability_digest
                != handoff.target_client_capability_digest
            ):
                raise PolicyViolation("Cross-client resume target capability digest drift")
            if target_client.permission_manifest is None:
                raise PolicyViolation("Cross-client resume target permission manifest ister")
            if (
                target_client.permission_manifest.permission_digest
                != handoff.target_client_permission_digest
            ):
                raise PolicyViolation("Cross-client resume target permission digest drift")
            if self.route_guard is None or handoff.target_route_decision_id is None:
                raise PolicyViolation("Cross-client resume canonical route guard ister")
            route = self.route_guard.require_current(
                handoff.target_route_decision_id,
                target_model_ref=handoff.to_model_ref,
                target_client_id=target_client.client_id,
                project_id=snapshot.project_id,
                at=observed_at,
            )
            if (
                route.evidence_digest != handoff.target_route_decision_digest
                or route.valid_until != handoff.target_route_valid_until
            ):
                raise PolicyViolation("Cross-client resume canonical route drift")
        return ResumeInstructions(
            client=handoff.to_client,
            model_ref=handoff.to_model_ref,
            first_reads=snapshot.first_reads,
            next_safe_actions=snapshot.next_safe_actions,
            evidence_digests=tuple(item.evidence_digest for item in snapshot.evidence_refs),
        )
