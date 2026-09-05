"""Current local OpenCode ledger decoding; no live client or legacy DB involved."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from tests.unit.test_local_continuity import NOW, ROOT, SOURCE_REF, _resolver
from tests.unit.test_local_continuity import continuity as continuity

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool
from zekam.application.local_continuity_service import LocalLifecycleContinuity
from zekam.application.opencode_lifecycle import OpenCodeForwardEvent, record_event
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.local_opencode_continuity import LocalOpenCodeObservation
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore


def test_current_opencode_start_compaction_and_restart_keep_real_project_context(
    continuity: Any, tmp_path: Path
) -> None:
    store, original, context = continuity
    binding = replace(original, session_id=str(uuid4()), client_id="opencode")
    store.bind_session(binding)
    spool = ClientLifecycleSpool(tmp_path / "spool", client_id="opencode")
    bridge = LocalLifecycleContinuity(
        store, spool, binding, source_probe=lambda: digest((ROOT / SOURCE_REF).read_text())
    )
    for kind in ("session.created", "session.compacting"):
        event = record_event(
            tmp_path / "current-client",
            event_type=kind,
            session_id=binding.external_session_id,
            now=NOW,
        )
        observed = LocalOpenCodeObservation(
            OpenCodeForwardEvent.capture(event.document()), "fixture-ledger-v2"
        )
        item = spool.stage(
            observed.observation_body(),
            delivery_id=observed.delivery_id,
            occurred_at=observed.occurred_at,
        )
        assert (
            spool.stage(
                observed.observation_body(),
                delivery_id=observed.delivery_id,
                occurred_at=observed.occurred_at,
            )
            == item
        )
    manifest = bridge.hydrate(context, key="hydrate")
    bridge.drain()
    checkpoint = bridge.pre_compaction(context_digest=manifest, key="compact")
    reopened = SQLiteContinuityStore(store.path, source_resolver=_resolver(binding))
    resumed = reopened.resume(binding, checkpoint)
    assert resumed["context"]["context"]["fragments"] == dict(context.fragments)
    assert resumed["grants_authority"] is False


@pytest.mark.parametrize(
    "kind",
    [
        "session.idle",
        "session.status",
        "session.error",
        "tool.execute.before",
        "tool.execute.after",
    ],
)
def test_ambiguous_status_and_tool_observations_do_not_create_authority(
    tmp_path: Path, kind: str
) -> None:
    event = record_event(tmp_path, event_type=kind, session_id="session_local", now=NOW)
    with pytest.raises(PolicyViolation, match="mapping"):
        LocalOpenCodeObservation(
            OpenCodeForwardEvent.capture(event.document()), "fixture-ledger-v2"
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"sequence": True},
        {"session_id": None},
        {"occurred_at": 1},
        {"contains_prompt": True},
        {"extra": "raw-text"},
        {"status": 17},
    ],
)
def test_corrupt_or_wrong_typed_current_event_is_rejected(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    body = (
        record_event(
            tmp_path, event_type="session.created", session_id="session_local", now=NOW
        ).body()
        | changes
    )
    with pytest.raises((PolicyViolation, ValidationFailed)):
        event = OpenCodeForwardEvent.capture(body | {"event_digest": digest(body)})
        LocalOpenCodeObservation(event, "fixture-ledger-v2")


def test_spool_observation_never_copies_summary_text(tmp_path: Path) -> None:
    event = record_event(
        tmp_path,
        event_type="session.checkpoint",
        session_id="session_local",
        completed_summary="Read health endpoint",
        pending_summary="More tests",
        next_action="Continue validation",
        now=NOW,
    )
    observation = LocalOpenCodeObservation(
        OpenCodeForwardEvent.capture(event.document()), "fixture-ledger-v2"
    )
    assert "Read health endpoint" not in str(observation.observation_body())
    assert observation.observation_body()["wire_digest"] == event.document()["event_digest"]
    with pytest.raises(ValidationFailed):
        LocalOpenCodeObservation(observation.event, "")


@pytest.mark.parametrize("document", ["{", "[]", "x" * 16385, None, 1])
def test_forward_envelope_is_bounded_and_typed_before_decoding(
    tmp_path: Path, document: Any
) -> None:
    event = record_event(
        tmp_path, event_type="session.created", session_id="session_local", now=NOW
    )
    captured = OpenCodeForwardEvent.capture(event.document())
    with pytest.raises(ValidationFailed):
        LocalOpenCodeObservation(
            replace(captured, canonical_document=document), "fixture-ledger-v2"
        )
