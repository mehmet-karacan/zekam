"""Crash-safe, content-free OpenCode lifecycle ledger and resume projection."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tempfile
import time
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ValidationFailed
from zekam.domain.identifiers import new_uuid7

SCHEMA_V1 = "zekam-opencode-lifecycle-event/v1"
SCHEMA = "zekam-opencode-lifecycle-event/v2"
_ID = re.compile(r"^[A-Za-z0-9_./:-]{1,160}$")
_EVENTS = frozenset(
    {
        "session.created",
        "session.compacted",
        "session.compacting",
        "session.checkpoint",
        "session.deleted",
        "session.error",
        "session.idle",
        "session.status",
        "tool.execute.before",
        "tool.execute.after",
    }
)
_SENSITIVE_SUMMARY = re.compile(r"(?i)(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+")
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|(?:^|\s)/(?:Users|home|root|etc|var)/)")


def lifecycle_root(home: Path) -> Path:
    return home / "global" / "runtime" / "opencode-lifecycle"


def lifecycle_client_instance_id(home: Path) -> str:
    """Makine-yerel, secret olmayan kalici OpenCode instance kimligi."""
    root = lifecycle_root(home)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "client-instance-id"
    if path.is_file():
        return _bounded(path.read_text(encoding="utf-8"), label="client_instance_id") or ""
    value = f"opencode-{new_uuid7()}"
    descriptor, temporary = tempfile.mkstemp(prefix=".client-instance-", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(value + "\n")
            output.flush()
            os.fsync(output.fileno())
        with suppress(FileExistsError):
            os.link(temporary, path)
        return path.read_text(encoding="utf-8").strip()
    finally:
        Path(temporary).unlink(missing_ok=True)


def record_canonical_ack(home: Path, ack: dict[str, str]) -> None:
    """Canonical ACK receipt'ini local spool retention bilgisi olarak yazar."""
    root = lifecycle_root(home) / "acked"
    root.mkdir(parents=True, exist_ok=True)
    local_digest = ack["local_event_digest"]
    parse_digest(local_digest)
    name = local_digest.removeprefix("sha256:") + ".json"
    content = json.dumps(ack, ensure_ascii=False, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".ack-", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        Path(temporary).replace(root / name)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


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


def _safe_summary(value: str | None, *, label: str) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if (
        not cleaned
        or len(cleaned) > 500
        or "\n" in cleaned
        or "\r" in cleaned
        or _SENSITIVE_SUMMARY.search(cleaned)
        or _ABSOLUTE_PATH.search(cleaned)
    ):
        raise ValidationFailed(f"OpenCode lifecycle {label} gecersiz")
    return cleaned


@dataclass(frozen=True, slots=True)
class OpenCodeLifecycleEvent:
    event_id: str
    delivery_id: str | None
    event_type: str
    session_id: str
    parent_session_id: str | None
    agent: str | None
    model_ref: str | None
    tool: str | None
    resource: str | None
    status: str | None
    error_category: str | None
    completed_summary: str | None
    pending_summary: str | None
    next_action: str | None
    task_label: str | None
    occurred_at: dt.datetime
    sequence: int
    previous_digest: str | None

    def __post_init__(self) -> None:
        _bounded(self.delivery_id, label="delivery_id")
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
        for label, value in (
            ("completed_summary", self.completed_summary),
            ("pending_summary", self.pending_summary),
            ("next_action", self.next_action),
            ("task_label", self.task_label),
        ):
            _safe_summary(value, label=label)
        if self.occurred_at.tzinfo is None:
            raise ValidationFailed("OpenCode lifecycle zamani timezone ister")
        if self.sequence < 1 or (self.sequence == 1) != (self.previous_digest is None):
            raise ValidationFailed("OpenCode lifecycle sequence/previous zinciri gecersiz")

    def body(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "event_id": self.event_id,
            "delivery_id": self.delivery_id,
            "event_type": self.event_type,
            "session_id": self.session_id,
            "parent_session_id": self.parent_session_id,
            "agent": self.agent,
            "model_ref": self.model_ref,
            "tool": self.tool,
            "resource": self.resource,
            "status": self.status,
            "error_category": self.error_category,
            "completed_summary": self.completed_summary,
            "pending_summary": self.pending_summary,
            "next_action": self.next_action,
            "task_label": self.task_label,
            "occurred_at": self.occurred_at.astimezone(dt.UTC).isoformat(),
            "sequence": self.sequence,
            "previous_digest": self.previous_digest,
            "contains_prompt": False,
            "contains_response": False,
            "grants_authority": False,
        }

    def document(self) -> dict[str, Any]:
        body = self.body()
        return body | {"event_digest": digest(body)}


@dataclass(frozen=True, slots=True)
class OpenCodeForwardEvent:
    """Immutable event-level evidence used by canonical forwarding admission."""

    canonical_document: str
    event_digest: str
    event_type: str
    session_id: str
    sequence: int
    previous_digest: str | None

    @classmethod
    def capture(cls, document: Mapping[str, Any]) -> OpenCodeForwardEvent:
        row = dict(document)
        event_digest = str(row.get("event_digest", ""))
        parse_digest(event_digest)
        body = {key: value for key, value in row.items() if key != "event_digest"}
        if digest(body) != event_digest:
            raise ValidationFailed("OpenCode forward event digest drift")
        if body.get("schema") != SCHEMA:
            raise ValidationFailed("OpenCode forward yalniz v2 event kabul eder")
        if (
            body.get("contains_prompt") is not False
            or body.get("contains_response") is not False
            or body.get("grants_authority") is not False
        ):
            raise ValidationFailed("OpenCode forward prompt/response/authority tasiyamaz")
        event_type = str(body.get("event_type", ""))
        if event_type not in _EVENTS:
            raise ValidationFailed("OpenCode forward event type desteklenmiyor")
        session_id = _bounded(str(body.get("session_id", "")), label="session_id")
        raw_sequence = body.get("sequence")
        if isinstance(raw_sequence, bool) or not isinstance(raw_sequence, int):
            raise ValidationFailed("OpenCode forward sequence integer olmali")
        previous = body.get("previous_digest")
        if previous is not None:
            previous = str(previous)
            parse_digest(previous)
        if raw_sequence < 1 or (raw_sequence == 1) != (previous is None):
            raise ValidationFailed("OpenCode forward sequence/previous zinciri gecersiz")
        assert session_id is not None
        return cls(
            canonical_document=canonical_json(row),
            event_digest=event_digest,
            event_type=event_type,
            session_id=session_id,
            sequence=raw_sequence,
            previous_digest=previous,
        )

    @property
    def exact_first_session_created(self) -> bool:
        return (
            self.event_type == "session.created"
            and self.sequence == 1
            and self.previous_digest is None
        )

    def document(self) -> dict[str, Any]:
        value = json.loads(self.canonical_document)
        if not isinstance(value, dict):  # defensive; constructor emits an object
            raise ValidationFailed("OpenCode forward immutable event object olmali")
        return value


@dataclass(frozen=True, slots=True)
class OpenCodeForwardBatch:
    """Bounded immutable batch; each event is admitted and committed separately."""

    events: tuple[OpenCodeForwardEvent, ...]
    batch_digest: str

    @classmethod
    def capture(cls, documents: Iterable[Mapping[str, Any]]) -> OpenCodeForwardBatch:
        events = tuple(OpenCodeForwardEvent.capture(item) for item in documents)
        if len(events) > 500:
            raise ValidationFailed("OpenCode forward batch bounded limiti asti")
        event_digests = tuple(item.event_digest for item in events)
        if len(set(event_digests)) != len(event_digests):
            raise ValidationFailed("OpenCode forward batch duplicate event tasiyor")
        return cls(
            events=events,
            batch_digest=digest(
                {
                    "schema": "zekam-opencode-forward-batch/v1",
                    "event_digests": event_digests,
                    "grants_authority": False,
                }
            ),
        )


def record_event(
    home: Path,
    *,
    event_type: str,
    session_id: str,
    delivery_id: str | None = None,
    parent_session_id: str | None = None,
    agent: str | None = None,
    model_ref: str | None = None,
    tool: str | None = None,
    resource: str | None = None,
    status: str | None = None,
    error_category: str | None = None,
    completed_summary: str | None = None,
    pending_summary: str | None = None,
    next_action: str | None = None,
    task_label: str | None = None,
    now: dt.datetime | None = None,
) -> OpenCodeLifecycleEvent:
    normalized_delivery = _bounded(delivery_id, label="delivery_id")
    normalized_resource = _relative_resource(resource)
    normalized_completed = _safe_summary(completed_summary, label="completed_summary")
    normalized_pending = _safe_summary(pending_summary, label="pending_summary")
    normalized_next = _safe_summary(next_action, label="next_action")
    normalized_label = _safe_summary(task_label, label="task_label")
    root = lifecycle_root(home)
    root.mkdir(parents=True, exist_ok=True)
    lock = _acquire_lock(root)
    try:
        existing = _verified_events(root, quarantine_invalid=True)
        if normalized_delivery is not None:
            replay = next(
                (item for item in existing if item.get("delivery_id") == normalized_delivery),
                None,
            )
            if replay is not None:
                expected = {
                    "event_type": event_type,
                    "session_id": session_id,
                    "parent_session_id": parent_session_id,
                    "agent": agent,
                    "model_ref": model_ref,
                    "tool": tool,
                    "resource": normalized_resource,
                    "status": status,
                    "error_category": error_category,
                    "completed_summary": normalized_completed,
                    "pending_summary": normalized_pending,
                    "next_action": normalized_next,
                    "task_label": normalized_label,
                }
                if any(replay.get(key) != value for key, value in expected.items()):
                    raise ValidationFailed("OpenCode lifecycle delivery_id payload drift")
                return _event_from_document(replay)
        stream_events = [
            item
            for item in existing
            if item.get("schema") == SCHEMA and item.get("session_id") == session_id
        ]
        sequence = len(stream_events) + 1
        previous_digest = None if not stream_events else str(stream_events[-1]["event_digest"])
        event = OpenCodeLifecycleEvent(
            event_id=str(new_uuid7()),
            delivery_id=normalized_delivery,
            event_type=event_type,
            session_id=session_id,
            parent_session_id=parent_session_id,
            agent=agent,
            model_ref=model_ref,
            tool=tool,
            resource=normalized_resource,
            status=status,
            error_category=error_category,
            completed_summary=normalized_completed,
            pending_summary=normalized_pending,
            next_action=normalized_next,
            task_label=normalized_label,
            occurred_at=now or dt.datetime.now(dt.UTC),
            sequence=sequence,
            previous_digest=previous_digest,
        )
        content = json.dumps(event.document(), ensure_ascii=False, sort_keys=True) + "\n"
        descriptor, temporary = tempfile.mkstemp(prefix=".event-", dir=root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            Path(temporary).replace(root / f"{event.sequence:020d}-{event.event_id}.json")
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return event
    finally:
        _release_lock(lock)


def _event_from_document(document: dict[str, Any]) -> OpenCodeLifecycleEvent:
    """Verified persisted document'i idempotent replay sonucu olarak dondurur."""

    return OpenCodeLifecycleEvent(
        event_id=str(document["event_id"]),
        delivery_id=(None if document.get("delivery_id") is None else str(document["delivery_id"])),
        event_type=str(document["event_type"]),
        session_id=str(document["session_id"]),
        parent_session_id=document.get("parent_session_id"),
        agent=document.get("agent"),
        model_ref=document.get("model_ref"),
        tool=document.get("tool"),
        resource=document.get("resource"),
        status=document.get("status"),
        error_category=document.get("error_category"),
        completed_summary=document.get("completed_summary"),
        pending_summary=document.get("pending_summary"),
        next_action=document.get("next_action"),
        task_label=document.get("task_label"),
        occurred_at=dt.datetime.fromisoformat(str(document["occurred_at"])),
        sequence=int(document["sequence"]),
        previous_digest=document.get("previous_digest"),
    )


def _try_platform_lock(stream: BinaryIO) -> bool:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            getattr(msvcrt, "locking")(  # noqa: B009
                stream.fileno(),
                getattr(msvcrt, "LK_NBLCK"),  # noqa: B009
                1,
            )
        except OSError:
            return False
        return True
    import fcntl

    try:
        lock_ex = int(getattr(fcntl, "LOCK_EX"))  # noqa: B009
        lock_nb = int(getattr(fcntl, "LOCK_NB"))  # noqa: B009
        getattr(fcntl, "flock")(stream.fileno(), lock_ex | lock_nb)  # noqa: B009
    except OSError:
        return False
    return True


def _acquire_lock(root: Path, *, timeout_seconds: float = 10.0) -> BinaryIO:
    lock = root / ".writer.lock"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            stream = lock.open("a+b")
        except PermissionError:
            time.sleep(0.005)
            continue
        if _try_platform_lock(stream):
            return stream
        stream.close()
        time.sleep(0.005)
    raise ValidationFailed("OpenCode lifecycle writer lock zaman asimi")


def _release_lock(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        getattr(msvcrt, "locking")(  # noqa: B009
            stream.fileno(),
            getattr(msvcrt, "LK_UNLCK"),  # noqa: B009
            1,
        )
    else:
        import fcntl

        lock_un = int(getattr(fcntl, "LOCK_UN"))  # noqa: B009
        getattr(fcntl, "flock")(stream.fileno(), lock_un)  # noqa: B009
    stream.close()


def _quarantine(root: Path, path: Path, reason: str) -> None:
    target = root / "quarantine"
    target.mkdir(exist_ok=True)
    receipt = {"file": path.name, "reason": reason, "observed_at": dt.datetime.now(dt.UTC)}
    receipt_path = target / f"{path.name}.reason.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, default=str, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.replace(target / path.name)


def _verified_events(root: Path, *, quarantine_invalid: bool) -> list[dict[str, Any]]:
    verified: list[dict[str, Any]] = []
    previous_by_session: dict[str, str] = {}
    expected_by_session: dict[str, int] = {}
    for path in sorted(root.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            event_digest = document.pop("event_digest")
        except (OSError, KeyError, UnicodeDecodeError, json.JSONDecodeError):
            if quarantine_invalid:
                _quarantine(root, path, "unreadable")
            continue
        schema = document.get("schema")
        valid = event_digest == digest(document)
        reason = "digest-mismatch"
        if valid and schema == SCHEMA:
            session_id = str(document.get("session_id", ""))
            expected_sequence = expected_by_session.get(session_id, 1)
            previous = previous_by_session.get(session_id)
            valid = (
                document.get("sequence") == expected_sequence
                and document.get("previous_digest") == previous
            )
            reason = "chain-mismatch"
        elif valid and schema != SCHEMA_V1:
            valid = False
            reason = "schema-unsupported"
        if not valid:
            if quarantine_invalid:
                _quarantine(root, path, reason)
            continue
        verified.append(document | {"event_digest": event_digest})
        if schema == SCHEMA:
            session_id = str(document["session_id"])
            previous_by_session[session_id] = str(event_digest)
            expected_by_session[session_id] = expected_by_session.get(session_id, 1) + 1
    return verified


_ACTIVE_TOOL_TTL = dt.timedelta(seconds=45)
_KNOWN_TOOLS = frozenset(
    {
        "bash",
        "edit",
        "glob",
        "grep",
        "lsp",
        "question",
        "read",
        "skill",
        "task",
        "webfetch",
        "websearch",
        "write",
    }
)


def _safe_tool_name(value: Any) -> str:
    normalized = str(value or "tool").strip().lower()
    return normalized if normalized in _KNOWN_TOOLS else "tool"


def resume_projection(
    home: Path,
    *,
    limit: int = 20,
    now: dt.datetime | None = None,
    quarantine_invalid: bool = True,
) -> dict[str, Any]:
    events = list(recent_events(home, limit=5000, quarantine_invalid=quarantine_invalid))
    events.sort(key=lambda item: (item["occurred_at"], item["event_id"]))
    sessions: dict[str, dict[str, Any]] = {}
    pending_tools: dict[str, tuple[str, dt.datetime]] = {}
    observed_at = now or dt.datetime.now(dt.UTC)
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
                "active_tool": None,
                "last_resource": None,
                "status": "running",
                "error_category": None,
                "completed_summary": None,
                "pending_summary": None,
                "next_safe_action": None,
                "task_label": event.get("task_label"),
                "created_at": event["occurred_at"],
                "updated_at": event["occurred_at"],
            },
        )
        for key in ("parent_session_id", "agent", "model_ref", "task_label"):
            current[key] = event.get(key) or current.get(key)
        current["last_event"] = event["event_type"]
        current["updated_at"] = event["occurred_at"]
        if event["event_type"] == "tool.execute.before":
            tool_name = _safe_tool_name(event.get("tool"))
            pending_tools[session_id] = (
                tool_name,
                dt.datetime.fromisoformat(str(event["occurred_at"])),
            )
            current["last_tool"] = tool_name
            current["active_tool"] = tool_name
            current["last_resource"] = event.get("resource")
            current["status"] = "running"
        elif event["event_type"] == "tool.execute.after":
            pending_tools.pop(session_id, None)
            current["active_tool"] = None
            current["last_tool"] = _safe_tool_name(event.get("tool"))
            current["status"] = event.get("status") or "running"
        elif event["event_type"] == "session.error":
            pending_tools.pop(session_id, None)
            current["active_tool"] = None
            current["status"] = "failed"
            current["error_category"] = event.get("error_category") or "session-error"
        elif event["event_type"] == "session.checkpoint":
            pending_tools.pop(session_id, None)
            current["active_tool"] = None
            current["status"] = "checkpointed"
            current["completed_summary"] = event.get("completed_summary")
            current["pending_summary"] = event.get("pending_summary")
            current["next_safe_action"] = event.get("next_action")
        elif event["event_type"] in {"session.idle", "session.compacted", "session.compacting"}:
            pending_tools.pop(session_id, None)
            current["active_tool"] = None
            current["status"] = "checkpointed"
        elif event["event_type"] == "session.deleted":
            pending = pending_tools.pop(session_id, None)
            current["active_tool"] = None
            if current["status"] == "failed":
                continue
            if pending is not None:
                current["status"] = "interrupted"
                current["next_safe_action"] = f"{pending[0]} etkisini dogrula; sessiz retry yapma"
            elif current["status"] == "checkpointed":
                current["status"] = "closed-checkpointed"
            else:
                current["status"] = "closed"
    for session_id, (tool, started_at) in pending_tools.items():
        if dt.timedelta(0) <= observed_at - started_at <= _ACTIVE_TOOL_TTL:
            sessions[session_id]["status"] = "running"
            sessions[session_id]["active_tool"] = tool
        else:
            sessions[session_id]["status"] = "interrupted"
            sessions[session_id]["active_tool"] = None
            sessions[session_id]["next_safe_action"] = (
                f"{tool} etkisini dogrula; sessiz retry yapma"
            )
    selected = sorted(sessions.values(), key=lambda item: item["updated_at"], reverse=True)[:limit]
    return {
        "source": "opencode-lifecycle",
        "sessions": selected,
        "interrupted_count": sum(item["status"] == "interrupted" for item in selected),
        "failed_count": sum(item["status"] == "failed" for item in selected),
    }


def recent_events(
    home: Path, *, limit: int = 80, quarantine_invalid: bool = True
) -> tuple[dict[str, Any], ...]:
    """Return newest verified, content-free OpenCode lifecycle events."""

    if limit < 1 or limit > 5000:
        raise ValidationFailed("OpenCode lifecycle event limiti 1..5000 olmali")
    root = lifecycle_root(home)
    if not root.is_dir():
        return ()
    events = _verified_events(root, quarantine_invalid=quarantine_invalid)
    events.sort(key=lambda item: (item["occurred_at"], item["event_id"]), reverse=True)
    return tuple(events[:limit])


def oldest_unacknowledged_events(home: Path, *, limit: int = 80) -> tuple[dict[str, Any], ...]:
    """Return the oldest local v2 chain prefix that lacks a durable canonical ACK."""

    if limit < 1 or limit > 500:
        raise ValidationFailed("OpenCode lifecycle forward limiti 1..500 olmali")
    root = lifecycle_root(home)
    if not root.is_dir():
        return ()
    acknowledged: set[str] = set()
    ack_root = root / "acked"
    if ack_root.is_dir():
        for path in ack_root.glob("*.json"):
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
                local_digest = str(receipt["local_event_digest"])
                parse_digest(local_digest)
                parse_digest(str(receipt["canonical_digest"]))
            except (OSError, KeyError, TypeError, ValueError, ValidationFailed):
                continue
            acknowledged.add(local_digest)
    events = [
        event
        for event in _verified_events(root, quarantine_invalid=True)
        if event.get("schema") == SCHEMA and str(event["event_digest"]) not in acknowledged
    ]
    # Sequence is the authority for each session chain. Session id provides a stable
    # grouping so a backlog larger than one CLI batch always starts at its oldest head.
    events.sort(key=lambda item: (str(item["session_id"]), int(item["sequence"])))
    return tuple(events[:limit])
