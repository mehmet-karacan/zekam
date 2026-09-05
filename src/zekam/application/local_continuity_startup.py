"""Provider-free required startup fragments; learned state is not invented here."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Protocol

from zekam.application.context_compiler import compile_context_v2
from zekam.application.context_ranking import ContextRankingRequest
from zekam.application.local_continuity import (
    ContinuityBinding,
    LocalContext,
    bounded_int,
    logical,
)
from zekam.application.local_continuity_service import LocalLifecycleContinuity
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    DEFAULT_TOKENIZER_PROFILE_DIGEST,
    AuthorityLevel,
    ContextCandidate,
    ContextCandidateKind,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed


@dataclass(frozen=True, slots=True)
class StartupRequest:
    source_refs: tuple[str, ...]
    token_budget: int
    idempotency_key: str
    observed_at: dt.datetime
    note_limit: int = 0
    retrieval_query: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_refs, tuple) or not 1 <= len(self.source_refs) <= 8:
            raise ValidationFailed("Startup requires 1..8 explicit source references")
        for ref in self.source_refs:
            logical(ref, "Startup source")
        if len(set(self.source_refs)) != len(self.source_refs):
            raise ValidationFailed("Startup duplicate source references")
        bounded_int(self.token_budget, maximum=131072)
        bounded_int(self.note_limit, minimum=0, maximum=8)
        if self.retrieval_query is not None and (
            not isinstance(self.retrieval_query, str)
            or not self.retrieval_query.strip()
            or len(self.retrieval_query.encode()) > 2048
            or "\x00" in self.retrieval_query
        ):
            raise ValidationFailed("Startup retrieval query bounded nonempty text required")
        logical(self.idempotency_key, "Startup hydration key")
        if not isinstance(self.observed_at, dt.datetime) or self.observed_at.tzinfo is None:
            raise ValidationFailed("Startup timezone-aware observation time required")


@dataclass(frozen=True, slots=True)
class StartupSnapshot:
    candidates: tuple[ContextCandidate, ...]
    fragments: tuple[tuple[str, str], ...]
    source_revision: str
    retrieval_report: dict[str, Any] | None = None
    checkpoint_report: dict[str, Any] | None = None


class StartupSourcePort(Protocol):
    def preflight(self, binding: ContinuityBinding) -> dict[str, Any] | None: ...

    def snapshot(self, binding: ContinuityBinding, request: StartupRequest) -> StartupSnapshot: ...
    def assert_current(self, binding: ContinuityBinding, snapshot: StartupSnapshot) -> None: ...


class LocalStartupService:
    def __init__(self, lifecycle: LocalLifecycleContinuity, sources: StartupSourcePort) -> None:
        self.lifecycle, self.sources = lifecycle, sources

    def hydrate(self, request: StartupRequest) -> dict[str, Any]:
        if not isinstance(request, StartupRequest):
            raise ValidationFailed("Typed startup request required")
        request.__post_init__()
        lifecycle, binding = self.lifecycle, self.lifecycle.binding
        preflight = getattr(self.sources, "preflight", None)
        if not callable(preflight):
            raise ValidationFailed("Startup requires an environment preflight contract")
        environment_report = preflight(binding)
        if lifecycle.entry_validator is None:
            raise PolicyViolation("Startup requires a reviewed lifecycle decoder")
        lifecycle.assert_current_source()
        # This is a consumer of required durable events, not an implicit backfill writer.
        with lifecycle.spool.frozen_session_entries(
            client_id=binding.client_id, session_id=binding.external_session_id
        ) as entries:
            for entry in entries:
                lifecycle._event(entry)
            if not entries or lifecycle._event(entries[0]).kind != "SESSION_START":
                raise PolicyViolation("Startup required SESSION_START evidence missing")
            if lifecycle.store.spool_digests(binding) != tuple(e.entry_digest for e in entries):
                raise PolicyViolation("Startup unpersisted spool delta blocks hydration")
            snapshot = self.sources.snapshot(binding, request)
            if not isinstance(snapshot, StartupSnapshot):
                raise ValidationFailed("Startup typed source snapshot required")
            candidates = snapshot.candidates
            if not isinstance(candidates, tuple) or any(
                not isinstance(candidate, ContextCandidate) for candidate in candidates
            ):
                raise ValidationFailed("Startup typed bounded candidates required")
            required_kinds = {
                ContextCandidateKind.SYSTEM_POLICY,
                ContextCandidateKind.WORK_CONTRACT,
                ContextCandidateKind.RUN_STATUS,
            }
            source_candidates = [
                c for c in candidates if c.kind is ContextCandidateKind.SOURCE_SLICE
            ]
            note_candidates = [c for c in candidates if c.kind is ContextCandidateKind.KNOWLEDGE]
            citations = [c for c in candidates if c.kind is ContextCandidateKind.CITATION]
            checkpoints = [c for c in candidates if c.kind is ContextCandidateKind.CHECKPOINT]
            if (
                len(candidates)
                != 3
                + len(request.source_refs)
                + len(note_candidates)
                + len(citations)
                + len(checkpoints)
                or len(checkpoints) > (1 if snapshot.checkpoint_report is not None else 0)
                or len(citations) > (8 if request.retrieval_query is not None else 0)
                or len(note_candidates) > request.note_limit
                or any(sum(c.kind is kind for c in candidates) != 1 for kind in required_kinds)
                or len(source_candidates) != len(request.source_refs)
                or {c.source_ref for c in source_candidates} != set(request.source_refs)
                or any(
                    not c.required
                    for c in candidates
                    if c.kind
                    not in {
                        ContextCandidateKind.KNOWLEDGE,
                        ContextCandidateKind.CITATION,
                        ContextCandidateKind.CHECKPOINT,
                    }
                )
                or any(c.required for c in (*note_candidates, *citations, *checkpoints))
            ):
                raise PolicyViolation("Startup exact required policy/work/run/source set missing")
            ranking = ContextRankingRequest(
                role="builder",
                target_identity_refs=(f"work/{binding.work_item_id}",),
                step_scope_ref=None,
                work_scope_ref=f"work/{binding.work_item_id}",
                project_scope_ref=f"project/{binding.project_id}",
                realm_scope_ref=f"realm/{binding.realm_id}",
                current_source_revision=snapshot.source_revision,
                compatible_source_revisions=tuple(sorted({c.source_revision for c in candidates})),
                task_terms=(),
                tokenizer_profile_digest=DEFAULT_TOKENIZER_PROFILE_DIGEST,
                additional_scope_refs=(
                    ("global-user",)
                    if any(c.scope_ref == "global-user" for c in note_candidates)
                    else ()
                ),
            )
            manifest = compile_context_v2(
                candidates,
                ranking_request=ranking,
                token_budget=request.token_budget,
                minimum_authority=AuthorityLevel.OBSERVED,
                now=request.observed_at,
                recipe_id="local-startup-required-v1",
                recipe_digest=digest("local-startup-required-v1"),
                target_role="builder",
                contents=dict(snapshot.fragments),
                ranking_snapshot_digest=digest(ranking.body()),
                candidate_set_digest=digest([c.candidate_digest for c in candidates]),
            )
            if {c.candidate_id for c in candidates if c.required} - {
                item.candidate_id for item in manifest.selected
            }:
                raise PolicyViolation("Startup required fragments cannot be omitted")
            selected = {item.candidate_id for item in manifest.selected}
            context = LocalContext(
                manifest,
                tuple(item for item in snapshot.fragments if item[0] in selected),
                ranking,
                tuple(c for c in candidates if c.candidate_id in selected),
            )
            self.sources.assert_current(binding, snapshot)
            lifecycle.assert_current_source()
            result = lifecycle.hydrate(context, key=request.idempotency_key)
        checkpoint_selected = sum(c.candidate_id in selected for c in checkpoints)
        checkpoint_report = (
            None
            if snapshot.checkpoint_report is None
            else snapshot.checkpoint_report | {"selected_count": checkpoint_selected}
        )
        return {
            "schema": "zekam-local-startup-required/v1",
            "manifest_digest": result,
            "token_count": sum(item.token_count for item in manifest.selected),
            "selected_count": len(manifest.selected),
            "grants_authority": False,
            "scope": "required-startup-fragments",
            "provider_called": False,
            "learned_state": "not-implemented",
            "retrieval": snapshot.retrieval_report,
            "environment": environment_report,
            "prior_checkpoint": checkpoint_report,
            "remaining_gates": (
                ([] if environment_report is not None else ["home-config-composition"])
                + (
                    []
                    if checkpoint_report is not None and checkpoint_selected == len(checkpoints)
                    else ["prior-checkpoint"]
                )
                + (
                    []
                    if snapshot.retrieval_report is not None
                    and snapshot.retrieval_report.get("state")
                    in {"source-verified-candidates", "abstained-insufficient-evidence"}
                    else ["knowledge-retrieval"]
                )
            ),
        }
