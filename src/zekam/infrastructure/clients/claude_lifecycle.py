"""Exact content-free Claude Code command-hook lifecycle contract."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zekam.domain.canonical import digest
from zekam.domain.clients import ClientKind
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.hook_runtime import HookEventType

CLAUDE_CLIENT_ID = "claude-code"
CLAUDE_REVIEWED_VERSION = "2.1.224"
CLAUDE_HOOK_CONTRACT_SCHEMA = "zekam-claude-command-hook/v1"
MAX_CLAUDE_HOOK_INPUT_BYTES = 64 * 1024
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SOURCES = frozenset({"startup", "resume", "clear", "compact"})
_TRIGGERS = frozenset({"manual", "auto"})
_REASONS = frozenset({"clear", "logout", "prompt_input_exit", "other"})
_PERMISSION_MODES = frozenset(
    {"default", "acceptEdits", "auto", "manual", "plan", "dontAsk", "bypassPermissions"}
)
CLAUDE_EVENT_MAPPING = {
    "SessionStart": HookEventType.CONTINUITY_SESSION_START,
    "PreCompact": HookEventType.PRE_COMPACTION,
    "PostCompact": HookEventType.POST_COMPACTION,
    "Stop": HookEventType.PRE_CLOSE,
    "SessionEnd": HookEventType.POST_CLOSE,
}


def assert_reviewed_claude_version(version: str) -> str:
    normalized = version.strip()
    if normalized != CLAUDE_REVIEWED_VERSION:
        raise PolicyViolation("Claude lifecycle contract version drift")
    return normalized


def _optional_enum(value: Any, *, label: str, allowed: frozenset[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        raise ValidationFailed(f"Claude hook {label} reviewed enum disinda")
    return value


@dataclass(frozen=True, slots=True)
class ClaudeHookEnvelope:
    session_id: str
    hook_event_name: str
    source: str | None
    trigger: str | None
    reason: str | None
    stop_hook_active: bool
    permission_mode: str | None
    wire_digest: str

    @property
    def internal_event_type(self) -> HookEventType:
        return CLAUDE_EVENT_MAPPING[self.hook_event_name]

    def observation_body(self, *, client_version: str = CLAUDE_REVIEWED_VERSION) -> dict[str, Any]:
        version = assert_reviewed_claude_version(client_version)
        return {
            "schema": CLAUDE_HOOK_CONTRACT_SCHEMA,
            "client_id": CLAUDE_CLIENT_ID,
            "client_kind": ClientKind.CLAUDE_CODE.value,
            "client_version": version,
            "session_id": self.session_id,
            "external_event_type": self.hook_event_name,
            "internal_event_type": self.internal_event_type.value,
            "turn_id": None,
            "source": self.source,
            "trigger": self.trigger,
            "reason": self.reason,
            "stop_hook_active": self.stop_hook_active,
            "permission_mode": self.permission_mode,
            "wire_digest": self.wire_digest,
            "contains_prompt": False,
            "contains_response": False,
            "contains_transcript": False,
            "grants_authority": False,
        }

    def delivery_id(self, *, occurrence_id: str, client_version: str) -> str:
        assert_reviewed_claude_version(client_version)
        if not _UUID.fullmatch(occurrence_id):
            raise ValidationFailed("Claude hook occurrence_id lowercase UUID olmali")
        return digest(
            {
                "contract": CLAUDE_HOOK_CONTRACT_SCHEMA,
                "client_version": client_version,
                "occurrence_id": occurrence_id,
                "session_id": self.session_id,
                "external_event_type": self.hook_event_name,
                "source": self.source,
                "trigger": self.trigger,
                "reason": self.reason,
                "stop_hook_active": self.stop_hook_active,
                "wire_digest": self.wire_digest,
            }
        )


def parse_claude_hook_input(payload: bytes | str) -> ClaudeHookEnvelope:
    """Keep only reviewed structural fields; content-bearing input is discarded."""

    raw = payload.encode("utf-8") if isinstance(payload, str) else bytes(payload)
    if not raw or len(raw) > MAX_CLAUDE_HOOK_INPUT_BYTES:
        raise ValidationFailed("Claude hook input bounded disi")
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Claude hook input strict JSON olmali") from exc
    if not isinstance(document, dict):
        raise ValidationFailed("Claude hook input JSON object olmali")
    session_id = document.get("session_id")
    event = document.get("hook_event_name")
    if not isinstance(session_id, str) or not _UUID.fullmatch(session_id):
        raise ValidationFailed("Claude hook session_id lowercase UUID olmali")
    if not isinstance(event, str) or event not in CLAUDE_EVENT_MAPPING:
        raise PolicyViolation("Claude lifecycle event reviewed contract disinda")
    source = _optional_enum(document.get("source"), label="source", allowed=_SOURCES)
    trigger = _optional_enum(document.get("trigger"), label="trigger", allowed=_TRIGGERS)
    reason = _optional_enum(document.get("reason"), label="reason", allowed=_REASONS)
    permission_mode = _optional_enum(
        document.get("permission_mode"), label="permission_mode", allowed=_PERMISSION_MODES
    )
    stop_hook_active = document.get("stop_hook_active", False)
    if not isinstance(stop_hook_active, bool):
        raise ValidationFailed("Claude hook stop_hook_active boolean olmali")
    if event == "SessionStart":
        if source is None or trigger is not None or reason is not None or stop_hook_active:
            raise ValidationFailed("Claude SessionStart reviewed wire contract ile uyusmuyor")
    elif event in {"PreCompact", "PostCompact"}:
        if trigger is None or source is not None or reason is not None or stop_hook_active:
            raise ValidationFailed("Claude compact reviewed wire contract ile uyusmuyor")
    elif event == "Stop":
        if source is not None or trigger is not None or reason is not None:
            raise ValidationFailed("Claude Stop reviewed wire contract ile uyusmuyor")
    elif reason is None or source is not None or trigger is not None or stop_hook_active:
        raise ValidationFailed("Claude SessionEnd reviewed wire contract ile uyusmuyor")
    safe_wire = {
        "session_id": session_id,
        "hook_event_name": event,
        "source": source,
        "trigger": trigger,
        "reason": reason,
        "stop_hook_active": stop_hook_active,
        "permission_mode": permission_mode,
    }
    return ClaudeHookEnvelope(
        session_id,
        event,
        source,
        trigger,
        reason,
        stop_hook_active,
        permission_mode,
        digest(safe_wire),
    )


def load_claude_contract(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationFailed("Claude lifecycle contract okunamadi") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema") != "zekam-claude-lifecycle-contract/v1"
        or document.get("client_id") != CLAUDE_CLIENT_ID
        or document.get("installed_version") != CLAUDE_REVIEWED_VERSION
        or document.get("events") != list(CLAUDE_EVENT_MAPPING)
        or document.get("content_fields_persisted") is not False
        or document.get("grants_authority") is not False
    ):
        raise PolicyViolation("Claude lifecycle contract semantic drift")
    return document
