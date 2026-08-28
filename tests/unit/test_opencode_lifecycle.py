from __future__ import annotations

import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from zekam.application.opencode_lifecycle import (
    SCHEMA_V1,
    OpenCodeForwardBatch,
    lifecycle_client_instance_id,
    lifecycle_root,
    recent_events,
    record_canonical_ack,
    record_event,
    resume_projection,
)
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed

NOW = dt.datetime(2026, 8, 23, 12, 0, tzinfo=dt.UTC)


def test_interrupted_tool_is_model_independent_resume_evidence(tmp_path) -> None:
    event = record_event(
        tmp_path,
        event_type="tool.execute.before",
        session_id="ses_parent",
        parent_session_id="ses_root",
        agent="zekam-implementer-model-a",
        model_ref="litellm/model-a",
        tool="bash",
        resource="src/app.py",
        now=NOW,
    )

    projection = resume_projection(tmp_path, now=NOW + dt.timedelta(seconds=46))

    assert event.document()["contains_prompt"] is False
    assert projection["interrupted_count"] == 1
    assert projection["sessions"][0]["status"] == "interrupted"
    assert projection["sessions"][0]["last_tool"] == "bash"
    assert projection["sessions"][0]["active_tool"] is None
    assert projection["sessions"][0]["last_resource"] == "src/app.py"
    assert "sessiz retry" in projection["sessions"][0]["next_safe_action"]


def test_completed_tool_and_idle_session_are_checkpointed(tmp_path) -> None:
    for index, event_type in enumerate(
        ("session.created", "tool.execute.before", "tool.execute.after", "session.idle")
    ):
        record_event(
            tmp_path,
            event_type=event_type,
            session_id="ses_1",
            tool="read" if event_type.startswith("tool.") else None,
            status="completed" if event_type == "tool.execute.after" else None,
            now=NOW + dt.timedelta(seconds=index),
        )

    projection = resume_projection(tmp_path, now=NOW + dt.timedelta(seconds=4))

    assert projection["interrupted_count"] == 0
    assert projection["sessions"][0]["status"] == "checkpointed"
    assert projection["sessions"][0]["active_tool"] is None


def test_tool_after_alone_clears_active_tool(tmp_path) -> None:
    record_event(
        tmp_path,
        event_type="tool.execute.before",
        session_id="ses_1",
        tool="bash",
        now=NOW,
    )
    record_event(
        tmp_path,
        event_type="tool.execute.after",
        session_id="ses_1",
        tool="bash",
        status="completed",
        now=NOW + dt.timedelta(seconds=1),
    )

    session = resume_projection(tmp_path, now=NOW + dt.timedelta(seconds=2))["sessions"][0]

    assert session["active_tool"] is None
    assert session["last_tool"] == "bash"


def test_recent_pending_tool_is_active_and_sanitized(tmp_path) -> None:
    record_event(
        tmp_path,
        event_type="tool.execute.before",
        session_id="ses_live",
        tool="bash",
        now=NOW,
    )

    session = resume_projection(tmp_path, now=NOW + dt.timedelta(seconds=10))["sessions"][0]

    assert session["status"] == "running"
    assert session["active_tool"] == "bash"


def test_unknown_pending_tool_uses_generic_name(tmp_path) -> None:
    record_event(
        tmp_path,
        event_type="tool.execute.before",
        session_id="ses_live",
        tool="private-custom-tool",
        now=NOW,
    )

    session = resume_projection(tmp_path, now=NOW + dt.timedelta(seconds=10))["sessions"][0]

    assert session["active_tool"] == "tool"
    assert session["last_tool"] == "tool"


@pytest.mark.parametrize(
    "terminal_event",
    [
        "session.error",
        "session.checkpoint",
        "session.idle",
        "session.compacted",
        "session.deleted",
    ],
)
def test_terminal_session_event_clears_active_tool(tmp_path, terminal_event: str) -> None:
    record_event(
        tmp_path,
        event_type="tool.execute.before",
        session_id="ses_live",
        tool="glob",
        now=NOW,
    )
    record_event(
        tmp_path,
        event_type=terminal_event,
        session_id="ses_live",
        now=NOW + dt.timedelta(seconds=1),
    )

    session = resume_projection(tmp_path, now=NOW + dt.timedelta(seconds=2))["sessions"][0]

    assert session["active_tool"] is None


def test_error_then_deleted_remains_failed(tmp_path) -> None:
    record_event(tmp_path, event_type="session.error", session_id="ses_1", now=NOW)
    record_event(
        tmp_path,
        event_type="session.deleted",
        session_id="ses_1",
        now=NOW + dt.timedelta(seconds=1),
    )
    session = resume_projection(tmp_path)["sessions"][0]
    assert session["status"] == "failed"


def test_pending_tool_then_deleted_is_interrupted(tmp_path) -> None:
    record_event(
        tmp_path, event_type="tool.execute.before", session_id="ses_1", tool="bash", now=NOW
    )
    record_event(
        tmp_path,
        event_type="session.deleted",
        session_id="ses_1",
        now=NOW + dt.timedelta(seconds=1),
    )
    session = resume_projection(tmp_path)["sessions"][0]
    assert session["status"] == "interrupted"
    assert "sessiz retry" in session["next_safe_action"]


def test_checkpoint_then_deleted_preserves_checkpoint_state(tmp_path) -> None:
    record_event(tmp_path, event_type="session.checkpoint", session_id="ses_1", now=NOW)
    record_event(
        tmp_path,
        event_type="session.deleted",
        session_id="ses_1",
        now=NOW + dt.timedelta(seconds=1),
    )
    assert resume_projection(tmp_path)["sessions"][0]["status"] == "closed-checkpointed"


def test_semantic_checkpoint_preserves_completed_pending_and_next_action(tmp_path) -> None:
    record_event(
        tmp_path,
        event_type="session.checkpoint",
        session_id="ses_1",
        completed_summary="Iki test gecti",
        pending_summary="Verifier bekleniyor",
        next_action="Verifier sonucunu dogrula",
        now=NOW,
    )

    session = resume_projection(tmp_path)["sessions"][0]
    assert session["completed_summary"] == "Iki test gecti"
    assert session["pending_summary"] == "Verifier bekleniyor"
    assert session["next_safe_action"] == "Verifier sonucunu dogrula"


@pytest.mark.parametrize(
    "summary",
    ["token=super-secret", r"C:\\Users\\name\\secret.txt", "/home/name/secret.txt"],
)
def test_semantic_checkpoint_rejects_sensitive_or_absolute_content(tmp_path, summary: str) -> None:
    with pytest.raises(ValidationFailed):
        record_event(
            tmp_path,
            event_type="session.checkpoint",
            session_id="ses_1",
            completed_summary=summary,
            now=NOW,
        )


def test_tampered_event_and_unsafe_resource_are_rejected(tmp_path) -> None:
    event = record_event(
        tmp_path,
        event_type="session.created",
        session_id="ses_1",
        now=NOW,
    )
    path = next((tmp_path / "global" / "runtime" / "opencode-lifecycle").glob("*.json"))
    document = json.loads(path.read_text(encoding="utf-8"))
    document["status"] = "forged"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert event.session_id == "ses_1"
    assert resume_projection(tmp_path)["sessions"] == []
    quarantine = tmp_path / "global" / "runtime" / "opencode-lifecycle" / "quarantine"
    assert list(quarantine.glob("*.json"))

    with pytest.raises(ValidationFailed, match="portable"):
        record_event(
            tmp_path,
            event_type="tool.execute.before",
            session_id="ses_2",
            resource="C:/secret.txt",
            now=NOW,
        )


def test_v2_events_form_a_monotonic_hash_chain(tmp_path) -> None:
    first = record_event(tmp_path, event_type="session.created", session_id="ses_1", now=NOW)
    second = record_event(
        tmp_path,
        event_type="session.idle",
        session_id="ses_1",
        now=NOW + dt.timedelta(seconds=1),
    )

    assert first.sequence == 1
    assert first.previous_digest is None
    assert second.sequence == 2
    assert second.previous_digest == first.document()["event_digest"]


def test_delivery_id_replay_returns_existing_event_without_duplicate(tmp_path) -> None:
    first = record_event(
        tmp_path,
        event_type="session.created",
        session_id="ses_delivery",
        delivery_id="delivery-one",
        now=NOW,
    )
    replay = record_event(
        tmp_path,
        event_type="session.created",
        session_id="ses_delivery",
        delivery_id="delivery-one",
        now=NOW + dt.timedelta(seconds=1),
    )

    assert replay.event_id == first.event_id
    assert replay.document()["event_digest"] == first.document()["event_digest"]
    assert len(recent_events(tmp_path)) == 1


def test_delivery_id_payload_drift_is_rejected(tmp_path) -> None:
    record_event(
        tmp_path,
        event_type="session.created",
        session_id="ses_delivery",
        delivery_id="delivery-one",
        now=NOW,
    )

    with pytest.raises(ValidationFailed, match="delivery_id payload drift"):
        record_event(
            tmp_path,
            event_type="session.idle",
            session_id="ses_delivery",
            delivery_id="delivery-one",
            now=NOW + dt.timedelta(seconds=1),
        )


def test_concurrent_delivery_id_replay_is_exactly_once(tmp_path) -> None:
    def write(_: int):
        return record_event(
            tmp_path,
            event_type="session.status",
            session_id="ses_delivery",
            delivery_id="delivery-concurrent",
            status="running",
            now=NOW,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        events = tuple(pool.map(write, range(24)))

    assert len({item.event_id for item in events}) == 1
    assert len(recent_events(tmp_path)) == 1


def test_each_session_has_an_independent_hash_chain(tmp_path) -> None:
    first = record_event(tmp_path, event_type="session.created", session_id="ses_a", now=NOW)
    second = record_event(tmp_path, event_type="session.created", session_id="ses_b", now=NOW)

    assert (first.sequence, first.previous_digest) == (1, None)
    assert (second.sequence, second.previous_digest) == (1, None)


def test_legacy_v1_event_remains_readable(tmp_path) -> None:
    root = lifecycle_root(tmp_path)
    root.mkdir(parents=True)
    body = {
        "schema": SCHEMA_V1,
        "event_id": "legacy-event",
        "event_type": "session.created",
        "session_id": "legacy-session",
        "parent_session_id": None,
        "agent": None,
        "model_ref": None,
        "tool": None,
        "resource": None,
        "status": None,
        "error_category": None,
        "completed_summary": None,
        "pending_summary": None,
        "next_action": None,
        "task_label": None,
        "occurred_at": NOW.isoformat(),
        "contains_prompt": False,
        "contains_response": False,
        "grants_authority": False,
    }
    (root / "legacy.json").write_text(
        json.dumps(body | {"event_digest": digest(body)}), encoding="utf-8"
    )

    assert recent_events(tmp_path)[0]["event_id"] == "legacy-event"


def test_client_instance_and_canonical_ack_are_durable(tmp_path) -> None:
    first = lifecycle_client_instance_id(tmp_path)
    second = lifecycle_client_instance_id(tmp_path)
    local_digest = digest("local-event")
    record_canonical_ack(
        tmp_path,
        {
            "event_id": "event-1",
            "local_event_digest": local_digest,
            "canonical_digest": digest("canonical-event"),
            "acknowledged_at": NOW.isoformat(),
        },
    )

    assert first == second
    ack = lifecycle_root(tmp_path) / "acked" / f"{local_digest.removeprefix('sha256:')}.json"
    assert json.loads(ack.read_text(encoding="utf-8"))["event_id"] == "event-1"


def test_forward_batch_is_immutable_and_binds_exact_first_created_event(tmp_path) -> None:
    first = record_event(
        tmp_path,
        event_type="session.created",
        session_id="ses_forward_batch",
        now=NOW,
    ).document()
    second = record_event(
        tmp_path,
        event_type="session.status",
        session_id="ses_forward_batch",
        status="running",
        now=NOW + dt.timedelta(seconds=1),
    ).document()

    batch = OpenCodeForwardBatch.capture((first, second))
    first["event_type"] = "session.deleted"

    assert batch.events[0].document()["event_type"] == "session.created"
    assert batch.events[0].exact_first_session_created
    assert not batch.events[1].exact_first_session_created
    assert batch.batch_digest.startswith("sha256:")


def test_forward_batch_rejects_digest_drift() -> None:
    with pytest.raises(ValidationFailed, match="digest drift"):
        OpenCodeForwardBatch.capture(
            (
                {
                    "schema": "zekam-opencode-lifecycle-event/v2",
                    "event_digest": "sha256:" + "0" * 64,
                },
            )
        )


def test_invalid_persisted_client_instance_is_rejected(tmp_path) -> None:
    root = lifecycle_root(tmp_path)
    root.mkdir(parents=True)
    (root / "client-instance-id").write_text("   ", encoding="utf-8")

    with pytest.raises(ValidationFailed, match="client_instance_id"):
        lifecycle_client_instance_id(tmp_path)


def test_concurrent_writers_allocate_unique_contiguous_sequences(tmp_path) -> None:
    def write(index: int):
        return record_event(
            tmp_path,
            event_type="session.status",
            session_id="ses_concurrent",
            now=NOW + dt.timedelta(milliseconds=index),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        events = tuple(pool.map(write, range(24)))

    assert sorted(item.sequence for item in events) == list(range(1, 25))
    stored = sorted(recent_events(tmp_path, limit=24), key=lambda item: item["sequence"])
    assert [item["sequence"] for item in stored] == list(range(1, 25))
    assert all(
        stored[index]["previous_digest"] == stored[index - 1]["event_digest"]
        for index in range(1, len(stored))
    )
