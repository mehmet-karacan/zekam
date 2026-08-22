"""Crash-safe, content-free OpenCode lifecycle ledger and resume projection."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.identifiers import new_uuid7

SCHEMA = "zekam-opencode-lifecycle-event/v1"
_ID = re.compile(r"^[A-Za-z0-9_./:-]{1,160}$")
_EVENTS = frozenset(
    {
        "session.created",
        "session.compacted",
        "session.deleted",
        "session.error",
        "session.idle",
        "session.status",
        "tool.execute.before",
        "tool.execute.after",
    }
)


def lifecycle_root(home: Path) -> Path:
    return home / "global" / "runtime" / "opencode-lifecycle"


def _bounded(value: str | None, *, label: str, maximum: int = 160) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > maximum or not _ID.fullmatch(cleaned):
        raise ValidationFailed(f"OpenCode lifecycle {label} gecersiz")
    return cleaned


def _relative_resource(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    normalized = value.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or re.match(r"^[A-Za-z]:/", normalized)
        or ".." in path.parts
        or len(normalized) > 512
    ):
        raise ValidationFailed("OpenCode lifecycle resource portable olmali")
    return normalized


@dataclass(frozen=True, slots=True)
class OpenCodeLifecycleEvent:
    event_id: str
    event_type: str
    session_id: str
    parent_session_id: str | None
    agent: str | None
    model_ref: str | None
    tool: str | None
    resource: str | None
    status: str | None
    error_category: str | None
    occurred_at: dt.datetime

    def __post_init__(self) -> None:
        if self.event_type not in _EVENTS:
            raise ValidationFailed("OpenCode lifecycle event type desteklenmiyor")
        _bounded(self.session_id, label="session_id")
        for label, value in (
            ("parent_session_id", self.parent_session_id),
            ("agent", self.agent),
            ("model_ref", self.model_ref),
            ("tool", self.tool),
            ("status", self.status),
            ("error_category", self.error_category),
        ):
            _bounded(value, label=label)
        _relative_resource(self.resource)
        if self.occurred_at.tzinfo is None:
            raise ValidationFailed("OpenCode lifecycle zamani timezone ister")

    def body(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id,
            "agent": self.agent,
            "model_ref": self.model_ref,
            "tool": self.tool,
            "resource": self.resource,
            "status": self.status,
            "error_category": self.error_category,
            "occurred_at": self.occurred_at.astimezone(dt.UTC).isoformat(),
            "contains_prompt": False,
            "contains_response": False,
            "grants_authority": False,
        }

    def document(self) -> dict[str, Any]:
        body = self.body()
        return body | {"event_digest": digest(body)}


def record_event(
    home: Path,
    *,
    event_type: str,
    session_id: str,
    parent_session_id: str | None = None,
    agent: str | None = None,
    model_ref: str | None = None,
    tool: str | None = None,
    resource: str | None = None,
    status: str | None = None,
    error_category: str | None = None,
    now: dt.datetime | None = None,
) -> OpenCodeLifecycleEvent:
    event = OpenCodeLifecycleEvent(
        event_id=str(new_uuid7()),
        event_type=event_type,
        session_id=session_id,
        parent_session_id=parent_session_id,
        agent=agent,
        model_ref=model_ref,
        tool=tool,
        resource=_relative_resource(resource),
        status=status,
        error_category=error_category,
        occurred_at=now or dt.datetime.now(dt.UTC),
    )
    root = lifecycle_root(home)
    root.mkdir(parents=True, exist_ok=True)
    content = json.dumps(event.document(), ensure_ascii=False, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".event-", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(root / f"{event.event_id}.json")
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return event


def resume_projection(home: Path, *, limit: int = 20) -> dict[str, Any]:
    root = lifecycle_root(home)
    if not root.is_dir():
        return {"source": "opencode-lifecycle", "sessions": [], "interrupted_count": 0}
    events: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), reverse=True)[:500]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        event_digest = document.pop("event_digest", None)
        if document.get("schema") != SCHEMA or event_digest != digest(document):
            continue
        events.append(document | {"event_digest": event_digest})
    events.sort(key=lambda item: (item["occurred_at"], item["event_id"]))
    sessions: dict[str, dict[str, Any]] = {}
    pending_tools: dict[str, str] = {}
    for event in events:
        session_id = str(event["session_id"])
        current = sessions.setdefault(
            session_id,
            {
                "session_id": session_id,
                "parent_session_id": event.get("parent_session_id"),
                "agent": event.get("agent"),
                "model_ref": event.get("model_ref"),
                "last_event": event["event_type"],
                "last_tool": None,
                "last_resource": None,
                "status": "running",
                "error_category": None,
                "updated_at": event["occurred_at"],
            },
        )
        for key in ("parent_session_id", "agent", "model_ref"):
            current[key] = event.get(key) or current.get(key)
        current["last_event"] = event["event_type"]
        current["updated_at"] = event["occurred_at"]
        if event["event_type"] == "tool.execute.before":
            pending_tools[session_id] = str(event.get("tool") or "unknown")
            current["last_tool"] = event.get("tool")
            current["last_resource"] = event.get("resource")
            current["status"] = "running"
        elif event["event_type"] == "tool.execute.after":
            pending_tools.pop(session_id, None)
            current["last_tool"] = event.get("tool") or current.get("last_tool")
            current["status"] = event.get("status") or "running"
        elif event["event_type"] == "session.error":
            current["status"] = "failed"
            current["error_category"] = event.get("error_category") or "session-error"
        elif event["event_type"] in {"session.idle", "session.compacted"}:
            current["status"] = "checkpointed"
        elif event["event_type"] == "session.deleted":
            current["status"] = "closed"
    for session_id, tool in pending_tools.items():
        sessions[session_id]["status"] = "interrupted"
        sessions[session_id]["next_safe_action"] = f"{tool} etkisini dogrula; sessiz retry yapma"
    selected = sorted(sessions.values(), key=lambda item: item["updated_at"], reverse=True)[:limit]
    return {
        "source": "opencode-lifecycle",
        "sessions": selected,
        "interrupted_count": sum(item["status"] == "interrupted" for item in selected),
        "failed_count": sum(item["status"] == "failed" for item in selected),
    }
