from __future__ import annotations

import json
from pathlib import Path

import pytest

from zekam.infrastructure.clients.claude_lifecycle import (
    CLAUDE_REVIEWED_VERSION,
    load_claude_contract,
    parse_claude_hook_input,
)


@pytest.mark.parametrize(
    ("event", "extra", "internal"),
    (
        ("SessionStart", {"source": "startup", "permission_mode": "default"}, "session_start"),
        ("PreCompact", {"trigger": "auto"}, "pre_compaction"),
        ("PostCompact", {"trigger": "manual"}, "post_compaction"),
        ("Stop", {"stop_hook_active": True, "permission_mode": "plan"}, "pre_close"),
        ("SessionEnd", {"reason": "other"}, "post_close"),
    ),
)
def test_claude_wire_maps_without_content(
    event: str, extra: dict[str, object], internal: str
) -> None:
    document = {
        "session_id": "00000000-0000-8000-8000-000000000001",
        "hook_event_name": event,
        "cwd": "C:/private",
        "transcript_path": "C:/secret/transcript.jsonl",
        "prompt": "secret prompt",
        "summary": "secret summary",
        "last_assistant_message": "secret answer",
        **extra,
    }
    envelope = parse_claude_hook_input(json.dumps(document))
    observation = envelope.observation_body(client_version=CLAUDE_REVIEWED_VERSION)
    assert observation["internal_event_type"] == internal
    rendered = json.dumps(observation)
    for secret in (
        "C:/private",
        "C:/secret/transcript.jsonl",
        "secret prompt",
        "secret summary",
        "secret answer",
    ):
        assert secret not in rendered
    assert observation["contains_prompt"] is False
    assert observation["contains_transcript"] is False


def test_tracked_claude_contract_matches_parser() -> None:
    root = Path(__file__).resolve().parents[2]
    document = load_claude_contract(
        root / "config" / "client-lifecycle" / "claude-code-2.1.224.json"
    )
    assert document["external_provider_required"] is False
