"""Measured loop progress'i mevcut context compiler'a bounded fragment olarak baglar."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from zekam.application.context_ranking import count_context_tokens
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.context_continuity import (
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
    EvidenceReference,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.loop_progress import LoopProgressPacket, require_progress_packet


@dataclass(frozen=True, slots=True)
class CurrentLoopContextBinding:
    """Provider-free current binding read from the active loop contract."""

    objective_digest: str
    source_revision: str
    plan_digest: str
    policy_revision_digest: str
    validator_asset_manifest_digest: str


@dataclass(frozen=True, slots=True)
class LoopProgressHydrationFragment:
    candidate: ContextCandidate
    content: str
    packet_digest: str


def build_loop_progress_hydration(
    packet: LoopProgressPacket,
    *,
    current: CurrentLoopContextBinding,
    observed_at: dt.datetime,
    identity_refs: tuple[str, ...],
    scope_ref: str,
    role: str,
    authority: AuthorityLevel = AuthorityLevel.VERIFIED,
) -> LoopProgressHydrationFragment:
    """Produce one required, transcript-free candidate for attempt 2+."""

    if role != "builder":
        raise PolicyViolation("Loop progress hydration yalniz builder attempt context'indedir")
    if observed_at.tzinfo is None:
        raise ValidationFailed("Loop progress hydration timezone ister")
    require_progress_packet(
        attempt_ordinal=packet.attempt_ordinal,
        packet=packet,
        objective_digest=current.objective_digest,
        source_revision=current.source_revision,
        plan_digest=current.plan_digest,
        policy_revision_digest=current.policy_revision_digest,
        validator_asset_manifest_digest=current.validator_asset_manifest_digest,
    )
    content = canonical_json(packet.as_dict())
    packet_ref = f"loop-progress/{packet.predecessor_attempt_id}/{packet.packet_digest[7:23]}"
    candidate = ContextCandidate(
        candidate_id=f"{packet_ref}/attempt-{packet.attempt_ordinal}",
        authority=authority,
        observed_at=observed_at,
        source_revision=current.source_revision,
        content_digest=digest(content),
        token_count=count_context_tokens(content),
        required=True,
        kind=ContextCandidateKind.LOOP_PROGRESS_PACKET,
        source_ref=packet_ref,
        identity_refs=identity_refs,
        scope_ref=scope_ref,
        applicable_roles=(role,),
        evidence_refs=(EvidenceReference("artifact", packet_ref, packet.packet_digest),),
    )
    return LoopProgressHydrationFragment(candidate, content, packet.packet_digest)
