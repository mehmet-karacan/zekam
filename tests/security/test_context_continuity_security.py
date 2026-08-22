"""Continuity packet secret/path/authority negatifleri."""

from __future__ import annotations

import datetime as dt

import pytest

from zekam.domain.canonical import digest
from zekam.domain.context_continuity import (
    ContinuitySnapshot,
    EvidenceReference,
    FinalizedHandoff,
)
from zekam.domain.errors import PolicyViolation

NOW = dt.datetime(2026, 8, 20, tzinfo=dt.UTC)
DIGEST = digest("security")


@pytest.mark.parametrize(
    "unsafe",
    ("C:\\private\\file.txt", "/private/file.txt", "../escape", "owner-token-value"),
)
def test_snapshot_rejects_absolute_traversal_and_sensitive_content(unsafe: str) -> None:
    with pytest.raises(PolicyViolation):
        ContinuitySnapshot(
            "project-1",
            "work-1",
            DIGEST,
            DIGEST,
            DIGEST,
            "revision-1",
            (unsafe,),
            ("reacquire-work",),
            (EvidenceReference("source", "docs/context.md", DIGEST),),
            NOW,
        )


def test_snapshot_and_handoff_cannot_carry_authority_lease_approval_or_transcript() -> None:
    with pytest.raises(PolicyViolation):
        ContinuitySnapshot(
            "project-1",
            "work-1",
            DIGEST,
            DIGEST,
            DIGEST,
            "revision-1",
            ("docs/context.md",),
            ("reacquire-work",),
            (EvidenceReference("source", "docs/context.md", DIGEST),),
            NOW,
            carries_active_lease=True,
        )
    with pytest.raises(PolicyViolation):
        FinalizedHandoff(
            "codex",
            "claude",
            "model-a",
            "model-b",
            DIGEST,
            DIGEST,
            "revision-1",
            NOW,
            transcript_included=True,
        )
