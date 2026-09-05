"""Cross-harness local/domain parity; canonical DB admission is out of scope."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from uuid import UUID

import pytest

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.continuity_projection import (
    HydrationCategory,
    HydrationItem,
    HydrationPriority,
    build_hydration_recipe,
)
from zekam.application.memory_candidate_compiler import (
    CompilerPreparation,
    CompilerSourceFragment,
    MemoryCandidateCompiler,
)
from zekam.application.memory_continuity_orchestrator import (
    LifecycleCompilerRecord,
    MemoryContinuityOrchestrator,
    plan_memory_hook,
)
from zekam.application.opencode_lifecycle import record_event
from zekam.domain.canonical import digest
from zekam.domain.hook_runtime import HookEventType
from zekam.domain.memory_compiler import MemoryCompilerOutput
from zekam.domain.session_continuity import (
    DataClassification,
    SessionLifecycleEvent,
)
from zekam.infrastructure.clients.codex_lifecycle import parse_codex_hook_input

pytestmark = pytest.mark.e2e

NOW = dt.datetime(2026, 8, 28, 12, 0, tzinfo=dt.UTC)
SESSION_ID = "0198f2ad-3d10-7a11-b515-4c5c1733f7c1"
TURN_ID = "0198f2ad-3d10-7a11-b515-4c5c1733f7c2"
DELIVERY_ID = "0198f2ad-3d10-7a11-b515-4c5c1733f7c3"
IDS = tuple(UUID(int=value) for value in range(101, 107))


def _compiler_record(
    client: str,
    source_event_digest: str,
    *,
    identity_offset: int,
) -> LifecycleCompilerRecord:
    structured = {
        "schema": "zekam-cross-harness-lifecycle-data/v1",
        "client_id": client,
        "source_event_digest": source_event_digest,
        "canonical_event_type": HookEventType.PRE_COMPACTION.value,
        "hydration_category": HydrationCategory.CHECKPOINT.value,
        "grants_authority": False,
    }
    event = SessionLifecycleEvent(
        realm_id=IDS[1],
        project_id=IDS[2],
        work_item_id=IDS[3],
        run_id=IDS[4],
        session_id=SESSION_ID,
        client_id=client,
        event_id=UUID(int=200 + identity_offset),
        event_type=HookEventType.PRE_COMPACTION.value,
        sequence=1,
        previous_digest=None,
        origin=f"client:{client}",
        causation_id=f"client-event:{client}",
        correlation_id="run:cross-harness",
        recursion_depth=0,
        source_revision="git:cross-harness",
        plan_ref="work-plan:cross-harness",
        checkpoint_ref="checkpoint:cross-harness",
        context_ref="context:bounded",
        payload_digest=digest(structured),
        metadata=(),
        classification=DataClassification.INTERNAL,
        occurred_at=NOW,
        ingested_at=NOW,
    )
    hook_input = {"lifecycle": event.body(), "data": structured}
    command = plan_memory_hook(HookEventType.PRE_COMPACTION, hook_input)
    hook_output = {
        "event_type": HookEventType.PRE_COMPACTION.value,
        "accepted": True,
        "command": command.body(),
        "command_digest": command.command_digest,
        "grants_authority": False,
    }
    return LifecycleCompilerRecord(
        event_id=event.event_id,
        outbox_id=UUID(int=300 + identity_offset),
        project_id=event.project_id,
        work_item_id=event.work_item_id,
        run_id=event.run_id,
        session_id=event.session_id,
        client_id=event.client_id,
        event_type=event.event_type,
        sequence=event.sequence,
        previous_digest=event.previous_digest,
        predecessor_digest=None,
        event_digest=event.event_digest,
        event_body=event.body(),
        source_revision=event.source_revision,
        classification=event.classification,
        invocation_id=UUID(int=400 + identity_offset),
        structured_data=structured,
        input_digest=digest(hook_input),
        hook_receipt_id=UUID(int=500 + identity_offset),
        hook_output=hook_output,
        hook_output_digest=digest(hook_output),
        hook_receipt_count=1,
        lifecycle_receipt_digest=digest({"client_id": client, "outbox": 300 + identity_offset}),
        occurred_at=NOW,
        completed_at=NOW,
    )


def _prepare(
    fragments: tuple[CompilerSourceFragment, ...],
    *,
    output_id: UUID,
    prior_output: MemoryCompilerOutput | None = None,
) -> CompilerPreparation:
    known = frozenset(
        (reference.ref, reference.digest_value)
        for fragment in fragments
        for reference in (fragment.source, *fragment.evidence_refs)
    )
    return MemoryCandidateCompiler().prepare(
        fragments,
        output_id=output_id,
        realm_id=IDS[1],
        project_id=IDS[2],
        work_item_id=IDS[3],
        run_id=IDS[4],
        parser_digest=digest("cross-harness-parser-v1"),
        policy_digest=digest("cross-harness-policy-v1"),
        profile_digest=digest("cross-harness-profile-v1"),
        known_references=known,
        created_at=NOW,
        prior_output=prior_output,
    )


def test_cross_harness_local_domain_candidate_and_replay_parity(
    tmp_path: Path,
) -> None:
    opencode_home = tmp_path / "opencode-home"
    opencode = record_event(
        opencode_home,
        event_type="session.compacting",
        session_id=SESSION_ID,
        delivery_id=DELIVERY_ID,
        now=NOW,
    )
    opencode_replay = record_event(
        opencode_home,
        event_type="session.compacting",
        session_id=SESSION_ID,
        delivery_id=DELIVERY_ID,
        now=NOW + dt.timedelta(seconds=1),
    )

    codex_envelope = parse_codex_hook_input(
        json.dumps(
            {
                "session_id": SESSION_ID,
                "hook_event_name": "PreCompact",
                "turn_id": TURN_ID,
                "trigger": "manual",
            }
        )
    )
    codex_spool = ClientLifecycleSpool(tmp_path / "codex-home", client_id="codex")
    codex_delivery = codex_envelope.delivery_id(occurrence_id=DELIVERY_ID)
    codex = codex_spool.stage(
        codex_envelope.observation_body(),
        delivery_id=codex_delivery,
        occurred_at=NOW,
    )
    codex_replay = codex_spool.stage(
        codex_envelope.observation_body(),
        delivery_id=codex_delivery,
        occurred_at=NOW + dt.timedelta(seconds=1),
    )

    # Both real harness adapters preserve the same content/authority/chain
    # invariants while retaining their exact raw event provenance.
    assert opencode.document()["contains_prompt"] is False
    assert opencode.document()["contains_response"] is False
    assert opencode.document()["grants_authority"] is False
    assert opencode.event_type == "session.compacting"
    assert codex.observation["contains_prompt"] is False
    assert codex.observation["contains_response"] is False
    assert codex.observation["contains_transcript"] is False
    assert codex.observation["grants_authority"] is False
    assert codex.external_event_type == "PreCompact"
    assert codex.internal_event_type == "pre_compaction"
    assert opencode.sequence == codex.sequence == 1
    assert opencode.previous_digest is codex.previous_entry_digest is None

    # Delivery replay is duplicate-free in both spools.
    assert opencode_replay.document()["event_digest"] == opencode.document()["event_digest"]
    assert codex_replay.entry_digest == codex.entry_digest
    opencode_events = opencode_home / "global" / "runtime" / "opencode-lifecycle"
    assert len(list(opencode_events.glob("*.json"))) == 1
    assert len(list(codex_spool.events_directory.glob("*.json"))) == 1

    # These compiler records deliberately exercise the production domain
    # types, orchestrator fragment projection and candidate compiler. They are
    # not evidence that either observation passed canonical PostgreSQL
    # lifecycle admission, runtime.execution_run binding or terminal outbox.
    records = (
        _compiler_record(
            "opencode",
            opencode.document()["event_digest"],
            identity_offset=1,
        ),
        _compiler_record("codex", codex.entry_digest, identity_offset=2),
    )
    assert {(item.project_id, item.work_item_id, item.run_id) for item in records} == {
        (IDS[2], IDS[3], IDS[4])
    }
    assert {item.event_type for item in records} == {HookEventType.PRE_COMPACTION.value}
    assert {item.structured_data["hydration_category"] for item in records} == {
        HydrationCategory.CHECKPOINT.value
    }
    fragments = tuple(MemoryContinuityOrchestrator._fragment(item) for item in records)
    first = _prepare(fragments, output_id=IDS[0])
    replay = _prepare(
        tuple(reversed(fragments)),
        output_id=IDS[5],
        prior_output=first.output,
    )

    assert len(first.output.candidates) == 2
    assert first.candidate_queue_digest == replay.candidate_queue_digest
    assert replay.replayed is True
    assert replay.output is first.output
    assert first.provider_calls == 0
    assert first.direct_promotion is False
    assert all(candidate.review_required for candidate in first.output.candidates)
    candidate_source_digests = {
        candidate.source_refs[0].digest_value for candidate in first.output.candidates
    }
    assert candidate_source_digests == {digest(item.structured_data) for item in records}
    assert {str(item.structured_data["source_event_digest"]) for item in records} == {
        codex.entry_digest,
        opencode.document()["event_digest"],
    }

    hydration = build_hydration_recipe(
        tuple(
            HydrationItem(
                item_id=candidate.logical_key,
                category=HydrationCategory.CHECKPOINT,
                content_ref=candidate.content_ref,
                source=candidate.source_refs[0],
                classification=candidate.classification,
                token_cost=1,
            )
            for candidate in first.output.candidates
        ),
        token_budget=2,
    )
    assert len(hydration.selected) == 2
    assert {item.category for item in hydration.selected} == {HydrationCategory.CHECKPOINT}
    assert {item.priority for item in hydration.selected} == {HydrationPriority.MUST_LOAD}
    assert {item.classification for item in hydration.selected} == {DataClassification.INTERNAL}
