from __future__ import annotations

import datetime as dt
import json

import pytest

from zekam.application.opencode_lifecycle import record_event, resume_projection
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

    projection = resume_projection(tmp_path)

    assert event.document()["contains_prompt"] is False
    assert projection["interrupted_count"] == 1
    assert projection["sessions"][0]["status"] == "interrupted"
    assert projection["sessions"][0]["last_tool"] == "bash"
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

    projection = resume_projection(tmp_path)

    assert projection["interrupted_count"] == 0
    assert projection["sessions"][0]["status"] == "checkpointed"


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

    with pytest.raises(ValidationFailed, match="portable"):
        record_event(
            tmp_path,
            event_type="tool.execute.before",
            session_id="ses_2",
            resource="C:/secret.txt",
            now=NOW,
        )
