"""Revalidate persisted observations against the reviewed structural parsers."""

from __future__ import annotations

from zekam.application.client_lifecycle_spool import LifecycleSpoolEntry
from zekam.domain.canonical import canonical_json
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.clients.claude_lifecycle import (
    CLAUDE_CLIENT_ID,
    CLAUDE_EVENT_MAPPING,
    parse_claude_hook_input,
)
from zekam.infrastructure.clients.codex_lifecycle import (
    CODEX_CLIENT_ID,
    CODEX_EVENT_MAPPING,
    parse_codex_hook_input,
)


def validate_reviewed_control_entry(entry: LifecycleSpoolEntry) -> None:
    """Digest validity alone does not prove an external/internal event mapping.

    OpenCode needs its separately reviewed version/source decoder at composition;
    its caller-declared version is not silently treated as installed evidence.
    """
    if not isinstance(entry, LifecycleSpoolEntry):
        raise ValidationFailed("Typed lifecycle spool entry required")
    entry.assert_integrity()
    observation = entry.observation
    document = {
        key: observation[key]
        for key in (
            "session_id",
            "turn_id",
            "source",
            "trigger",
            "reason",
            "stop_hook_active",
            "permission_mode",
        )
    }
    document["hook_event_name"] = entry.external_event_type
    if entry.client_id == CODEX_CLIENT_ID:
        if entry.external_event_type not in dict(CODEX_EVENT_MAPPING):
            raise PolicyViolation("Control external event outside reviewed mapping")
        expected = parse_codex_hook_input(canonical_json(document)).observation_body(
            client_version=entry.client_version
        )
    elif entry.client_id == CLAUDE_CLIENT_ID:
        if entry.external_event_type not in CLAUDE_EVENT_MAPPING:
            raise PolicyViolation("Control external event outside reviewed mapping")
        expected = parse_claude_hook_input(canonical_json(document)).observation_body(
            client_version=entry.client_version
        )
    else:
        raise PolicyViolation("Control observation needs an exact reviewed client decoder")
    if canonical_json(expected) != canonical_json(observation):
        raise PolicyViolation("Control observation reviewed mapping/schema/wire digest drift")
