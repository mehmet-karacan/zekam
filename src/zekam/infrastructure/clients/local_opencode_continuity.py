"""Content-free OpenCode v2 observations for the local continuity spool.

This decoder is not a live-client contract approval or a legacy-ledger importer.
Callers supply one current, source-chain-verified event. It does not infer effect
claims from tool observations or infer a pre-close gate from idle/status/deleted.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from zekam.application.local_continuity import bounded_int, logical, uuid_text
from zekam.application.opencode_lifecycle import OpenCodeForwardEvent, OpenCodeLifecycleEvent
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

SCHEMA = "zekam-local-opencode-observation/v1"
EVENT_MAPPING = {
    "session.created": "session_start",
    "session.compacting": "pre_compaction",
    "session.compacted": "post_compaction",
    "session.checkpoint": "CHECKPOINT_REQUESTED",
    "session.deleted": "post_close",
}


@dataclass(frozen=True, slots=True)
class LocalOpenCodeObservation:
    event: OpenCodeForwardEvent
    client_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.event, OpenCodeForwardEvent):
            raise ValidationFailed("Typed OpenCode forward event required")
        logical(self.client_version, "Observed OpenCode client version")
        if (
            not isinstance(self.event.canonical_document, str)
            or len(self.event.canonical_document.encode("utf-8")) > 16384
        ):
            raise ValidationFailed("OpenCode local event byte bound exceeded")
        try:
            document = self.event.document()
            if OpenCodeForwardEvent.capture(document) != self.event:
                raise PolicyViolation("OpenCode forward event envelope drift")
        except (ValueError, TypeError) as exc:
            raise ValidationFailed("OpenCode local forward envelope malformed") from exc
        fields = set(OpenCodeLifecycleEvent.__dataclass_fields__)
        if set(document) != fields | {
            "schema",
            "event_digest",
            "contains_prompt",
            "contains_response",
            "grants_authority",
        }:
            raise ValidationFailed("OpenCode local adapter exact v2 fields required")
        if len(canonical_json(document).encode("utf-8")) > 16384:
            raise ValidationFailed("OpenCode local event byte bound exceeded")
        if self.event.event_type not in EVENT_MAPPING:
            raise PolicyViolation("OpenCode event has no reviewed local continuity mapping")
        uuid_text(document["event_id"], "OpenCode event id")
        logical(document["session_id"], "OpenCode session id")
        bounded_int(document["sequence"], maximum=2**63 - 1)
        for name in fields - {"occurred_at", "sequence"}:
            if document[name] is not None and not isinstance(document[name], str):
                raise ValidationFailed("OpenCode local event fields must be text or null")
        if not isinstance(document["occurred_at"], str):
            raise ValidationFailed("OpenCode event timestamp required")
        try:
            values = {name: document[name] for name in fields}
            values["occurred_at"] = dt.datetime.fromisoformat(document["occurred_at"])
            parsed = OpenCodeLifecycleEvent(**values)
            if canonical_json(parsed.document()) != self.event.canonical_document:
                raise ValueError("Event canonical round-trip drift")
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValidationFailed("OpenCode local event malformed") from exc

    @property
    def delivery_id(self) -> str:
        return digest(
            {
                "adapter": SCHEMA,
                "client_version": self.client_version,
                "source_event_digest": self.event.event_digest,
            }
        )

    @property
    def occurred_at(self) -> dt.datetime:
        return dt.datetime.fromisoformat(self.event.document()["occurred_at"])

    def observation_body(self) -> dict[str, Any]:
        self.__post_init__()
        return {
            "schema": SCHEMA,
            "client_id": "opencode",
            "client_kind": "opencode",
            "client_version": self.client_version,
            "session_id": self.event.session_id,
            "external_event_type": self.event.event_type,
            "internal_event_type": EVENT_MAPPING[self.event.event_type],
            "turn_id": None,
            "source": None,
            "trigger": None,
            "reason": None,
            "stop_hook_active": False,
            "permission_mode": None,
            "wire_digest": self.event.event_digest,
            "contains_prompt": False,
            "contains_response": False,
            "contains_transcript": False,
            "grants_authority": False,
        }
