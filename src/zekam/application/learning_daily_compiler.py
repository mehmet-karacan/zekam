"""Receipt-backed daily learning projection into immutable Markdown knowledge."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from zekam.application.knowledge_file_plane import (
    KnowledgeClassification,
    KnowledgeNoteManifest,
    generated_note_bytes,
    note_content_digest,
    validate_generated_note,
)
from zekam.application.knowledge_plane_service import KnowledgePlaneService
from zekam.application.local_runtime_service import LocalEffectRequest, LocalEffectResult
from zekam.application.operational_store import KnowledgeNoteRecord, OperationalStore
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ZekamError
from zekam.infrastructure.knowledge_files import KnowledgeFileStore

GENERATOR_VERSION = "learning-daily-compiler/v1"
LEARNING_DAILY_OPERATION = "report.learning-daily/v1"
_LOCAL_REALM_ID = str(uuid5(NAMESPACE_URL, "zekam://realm/yerel"))
DAILY_TIMEZONE = "Europe/Istanbul"
DAILY_LOCAL_HOUR = 21


class LearningSnapshotPort(Protocol):
    def daily_snapshot(
        self,
        day: dt.date,
        *,
        start_day: dt.date | None = None,
        timezone_name: str = "UTC",
    ) -> dict[str, object]: ...


def latest_due_learning_day(now: dt.datetime) -> dt.date:
    """Return the last complete local day whose 21:00 compile window is due."""

    if type(now) is not dt.datetime or now.tzinfo is None or now.utcoffset() is None:
        raise PolicyViolation("Learning daily scheduler timezone-aware time ister")
    local = now.astimezone(ZoneInfo(DAILY_TIMEZONE))
    days_back = 1 if local.hour >= DAILY_LOCAL_HOUR else 2
    return local.date() - dt.timedelta(days=days_back)


def learning_daily_scheduled_for(day: dt.date) -> dt.datetime:
    """Canonical audit instant: 21:00 local on the day after the completed window."""

    if type(day) is not dt.date:
        raise PolicyViolation("Learning daily schedule exact date ister")
    local = dt.datetime.combine(
        day + dt.timedelta(days=1),
        dt.time(DAILY_LOCAL_HOUR),
        tzinfo=ZoneInfo(DAILY_TIMEZONE),
    )
    return local.astimezone(dt.UTC)


def latest_materialized_daily_day(
    operational: OperationalStore, files: KnowledgeFileStore
) -> dt.date | None:
    """Read the newest exact generated daily note through the physical file plane."""

    with operational.unit_of_work() as uow:
        notes = uow.list_knowledge_notes(
            owner_scope="global-user", note_kind="daylog", state=None, limit=1000
        )
        uow.commit()
    if len(notes) == 1000:
        raise PolicyViolation("Learning daily watermark note scan bound exceeded")
    days: list[dt.date] = []
    for note in notes:
        if note.authorship != "generated" or not note.materialized:
            continue
        try:
            payload = files.read_note(
                _manifest(note),
                relative_ref=note.archived_ref if note.state == "archived" else None,
            )
            metadata = validate_generated_note(payload)
        except ZekamError:
            continue
        refs = metadata.get("source_refs")
        if metadata.get("generator_version") != GENERATOR_VERSION or not isinstance(refs, list):
            continue
        if len(refs) != 1 or not isinstance(refs[0], str):
            continue
        prefix = "learning/daily/"
        if not refs[0].startswith(prefix) or ".." not in refs[0]:
            continue
        start_text, end_text = refs[0][len(prefix) :].split("..", 1)
        try:
            start, end = dt.date.fromisoformat(start_text), dt.date.fromisoformat(end_text)
        except ValueError:
            continue
        if start > end or not note.portable_ref.startswith(
            f"global/generated/daylog/{end.isoformat()}-"
        ):
            continue
        days.append(end)
    return max(days) if days else None


class LearningDailyEffectExecutor:
    """Typed runtime handler; no model, provider, network or free shell payload."""

    def __init__(
        self,
        learning: LearningSnapshotPort,
        operational: OperationalStore,
        files: KnowledgeFileStore,
    ) -> None:
        self._learning = learning
        self._operational = operational
        self._files = files

    def __call__(self, request: LocalEffectRequest) -> LocalEffectResult:
        payload = request.payload
        if (
            request.operation != LEARNING_DAILY_OPERATION
            or frozenset(payload)
            != {"start_day", "day", "scheduled_for", "schedule_digest", "source", "timezone"}
            or payload.get("source") != "os-supervisor"
            or payload.get("timezone") != DAILY_TIMEZONE
            or not all(
                isinstance(payload.get(key), str)
                for key in ("start_day", "day", "scheduled_for", "schedule_digest")
            )
        ):
            raise PolicyViolation("Learning daily exact typed payload ister")
        try:
            start_day = dt.date.fromisoformat(str(payload["start_day"]))
            day = dt.date.fromisoformat(str(payload["day"]))
            scheduled_for = dt.datetime.fromisoformat(
                str(payload["scheduled_for"]).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise PolicyViolation("Learning daily canonical UTC schedule ister") from exc
        if (
            scheduled_for.tzinfo is None
            or scheduled_for.utcoffset() != dt.timedelta(0)
            or start_day > day
            or scheduled_for != learning_daily_scheduled_for(day)
            or str(payload["scheduled_for"])
            != scheduled_for.isoformat().replace("+00:00", "Z")
        ):
            raise PolicyViolation("Learning daily day/schedule binding drift")
        schedule_body = {
            "schema": "zekam-learning-daily-schedule/v1",
            "start_day": start_day.isoformat(),
            "day": day.isoformat(),
            "scheduled_for": payload["scheduled_for"],
            "operation": LEARNING_DAILY_OPERATION,
            "misfire": "run-once",
            "timezone": DAILY_TIMEZONE,
        }
        if digest(schedule_body) != payload["schedule_digest"]:
            raise PolicyViolation("Learning daily schedule digest drift")
        result = compile_learning_daily(
            self._learning,
            self._operational,
            self._files,
            day=day,
            start_day=start_day,
            timezone_name=DAILY_TIMEZONE,
            compiled_at=scheduled_for,
        )
        return LocalEffectResult("completed", str(result.as_dict()["result_digest"]))


@dataclass(frozen=True, slots=True)
class LearningDailyCompileResult:
    state: str
    note_id: str
    portable_ref: str
    content_digest: str
    snapshot_digest: str
    predecessor_note_id: str | None
    relation_id: str | None

    def as_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "schema": "zekam-learning-daily-compile-result/v1",
            "state": self.state,
            "note_id": self.note_id,
            "portable_ref": self.portable_ref,
            "content_digest": self.content_digest,
            "snapshot_digest": self.snapshot_digest,
            "predecessor_note_id": self.predecessor_note_id,
            "relation_id": self.relation_id,
            "grants_authority": False,
        }
        return body | {"result_digest": digest(body)}


def compile_learning_daily(
    learning: LearningSnapshotPort,
    operational: OperationalStore,
    files: KnowledgeFileStore,
    *,
    day: dt.date,
    start_day: dt.date | None = None,
    timezone_name: str = "UTC",
    compiled_at: dt.datetime,
) -> LearningDailyCompileResult:
    """Create one immutable revision; preserve and mark edited generated predecessors."""

    if type(day) is not dt.date or type(compiled_at) is not dt.datetime:
        raise PolicyViolation("Learning daily compiler exact day/time ister")
    if compiled_at.tzinfo is None or compiled_at.utcoffset() is None:
        raise PolicyViolation("Learning daily compiler timezone-aware time ister")
    start = day if start_day is None else start_day
    if type(start) is not dt.date or start > day or timezone_name not in {
        "UTC",
        DAILY_TIMEZONE,
    }:
        raise PolicyViolation("Learning daily compiler bounded day range ister")
    generated_at = (
        compiled_at.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    snapshot = learning.daily_snapshot(
        day, start_day=start, timezone_name=timezone_name
    )
    snapshot_digest = snapshot.get("snapshot_digest")
    if not isinstance(snapshot_digest, str):
        raise PolicyViolation("Learning daily snapshot digest eksik")
    body = (
        f"# Learning daily {start.isoformat()}..{day.isoformat()}\n\n"
        "This report is a content-free index; original evidence remains authoritative.\n\n"
        f"```json\n{canonical_json(snapshot)}\n```\n"
    )
    payload = generated_note_bytes(
        owner_scope="global-user",
        note_kind="daylog",
        classification=KnowledgeClassification.LOCAL_PRIVATE,
        source_refs=(f"learning/daily/{start.isoformat()}..{day.isoformat()}",),
        source_digests=(snapshot_digest,),
        generated_at=generated_at,
        generator_version=GENERATOR_VERSION,
        body=body,
    )
    content_digest = note_content_digest(payload)
    prefix = f"global/generated/daylog/{day.isoformat()}-"
    portable_ref = f"{prefix}{content_digest[7:19]}.md"
    manifest = KnowledgeNoteManifest(
        owner_scope="global-user",
        note_kind="daylog",
        authorship="generated",
        classification=KnowledgeClassification.LOCAL_PRIVATE,
        portable_ref=portable_ref,
        content_digest=content_digest,
    )
    with operational.unit_of_work() as uow:
        candidates = uow.list_knowledge_notes(
            owner_scope="global-user", note_kind="daylog", state="active", limit=1000
        )
        active = tuple(
            note
            for note in candidates
            if note.authorship == "generated" and note.portable_ref.startswith(prefix)
        )
        uow.commit()
    if len(candidates) == 1000 and not active:
        raise PolicyViolation("Learning daily predecessor lookup bound exceeded")
    if len(active) > 2:
        raise PolicyViolation("Learning daily active revision conflict bound exceeded")
    service = KnowledgePlaneService(operational, files)
    snapshot_matches: list[KnowledgeNoteRecord] = []
    for current in active:
        try:
            metadata = validate_generated_note(files.read_note(_manifest(current)))
        except ZekamError:
            continue
        if (
            metadata.get("source_digests") == [snapshot_digest]
            and metadata.get("generator_version") == GENERATOR_VERSION
        ):
            snapshot_matches.append(current)
    if len(snapshot_matches) > 1:
        raise PolicyViolation("Learning daily duplicate snapshot revisions")
    if snapshot_matches and len(active) == 1:
        current = service.reconcile_materialized_note(
            record=snapshot_matches[0], manifest=_manifest(snapshot_matches[0])
        )
        return LearningDailyCompileResult(
            "current",
            current.id,
            current.portable_ref,
            current.content_digest,
            snapshot_digest,
            None,
            None,
        )
    if len(active) > 1 and not snapshot_matches:
        raise PolicyViolation("Learning daily unresolved active revision conflict")
    exact = snapshot_matches[0] if snapshot_matches else next(
        (note for note in active if note.content_digest == content_digest), None
    )
    predecessor = next((note for note in active if note.id != getattr(exact, "id", None)), None)
    if exact is None:
        materialized = service.materialize_note(
            realm_id=_LOCAL_REALM_ID,
            project_id=None,
            manifest=manifest,
            payload=payload,
        )
        exact = materialized.record
    else:
        if exact.content_digest != content_digest:
            raise PolicyViolation("Learning daily snapshot/content binding drift")
        exact = service.reconcile_materialized_note(record=exact, manifest=_manifest(exact))
    if predecessor is None:
        return LearningDailyCompileResult(
            "current",
            exact.id,
            exact.portable_ref,
            exact.content_digest,
            snapshot_digest,
            None,
            None,
        )
    relation_kind = "supersedes"
    archived_ref: str | None = None
    try:
        archived_ref = files.archive_note(_manifest(predecessor))
    except ZekamError:
        relation_kind = "conflicts-with"
    relation_source = digest(
        {
            "operation": "learning.daily.compile",
            "day": day.isoformat(),
            "new_note_id": exact.id,
            "old_note_id": predecessor.id,
            "snapshot_digest": snapshot_digest,
            "relation_kind": relation_kind,
        }
    )
    with operational.unit_of_work() as uow:
        relation = uow.relate_knowledge_notes(
            from_note_id=exact.id,
            to_note_id=predecessor.id,
            relation_kind=relation_kind,
            source_digest=relation_source,
            verified=True,
        )
        if archived_ref is not None:
            uow.archive_knowledge_note(
                note_id=predecessor.id,
                expected_content_digest=predecessor.content_digest,
                archived_ref=archived_ref,
            )
        uow.commit()
    return LearningDailyCompileResult(
        "conflict" if relation_kind == "conflicts-with" else "revised",
        exact.id,
        exact.portable_ref,
        exact.content_digest,
        snapshot_digest,
        predecessor.id,
        relation.id,
    )


def _manifest(record: KnowledgeNoteRecord) -> KnowledgeNoteManifest:
    return KnowledgeNoteManifest(
        owner_scope=record.owner_scope,
        project_slug=record.project_slug,
        note_kind=record.note_kind,
        authorship=record.authorship,
        classification=KnowledgeClassification(record.classification),
        portable_ref=record.portable_ref,
        content_digest=record.content_digest,
        state=record.state,
    )
