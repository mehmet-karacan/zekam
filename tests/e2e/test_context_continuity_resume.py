"""Codex/Claude/OpenCode transcript-free continuity E2E."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.application.context_continuity_service import ContextContinuityService
from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    Checkpoint,
    ContinuitySnapshot,
    EvidenceReference,
    FinalizedHandoff,
)

pytestmark = pytest.mark.e2e
NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)


def test_codex_claude_opencode_resume_without_transcript_or_inherited_authority() -> None:
    checkpoint = Checkpoint(
        "checkpoint-1",
        "project-zekam",
        "work-1",
        "plan-1",
        "revision-1",
        ("inspect", "implement", "verify"),
        ("inspect",),
        ("implement", "verify"),
        (("inspect", digest("inspection")),),
        digest("context-manifest"),
        digest("journal-head"),
        "reacquire-work-and-implement",
        NOW,
    )
    snapshot = ContinuitySnapshot(
        "project-zekam",
        "work-1",
        checkpoint.checkpoint_digest,
        checkpoint.journal_head_digest,
        checkpoint.context_manifest_digest,
        "revision-1",
        ("docs/CONTEXT_COMPILER_VE_CONTINUITY.md",),
        ("reacquire-work-and-implement",),
        (EvidenceReference("benchmark", "model-decision:latest", digest("decision")),),
        NOW,
    )
    service = ContextContinuityService()
    current_client = "codex"
    for next_client in ("claude", "opencode", "codex"):
        handoff = FinalizedHandoff(
            current_client,
            next_client,
            f"model-ref-{current_client}",
            f"model-ref-{next_client}",
            snapshot.snapshot_digest,
            checkpoint.checkpoint_digest,
            "revision-1",
            NOW,
        )
        instructions = service.resume(
            handoff=handoff,
            snapshot=snapshot,
            checkpoint=checkpoint,
            current_source_revision="revision-1",
        )
        assert instructions.client == next_client
        assert instructions.reacquire_work
        assert not instructions.transcript_used
        assert not instructions.grants_authority
        current_client = next_client
