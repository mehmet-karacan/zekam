"""Dormant atomic SQLite writer for reviewed Codex 0.151 PreCompact."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import stat
from contextlib import closing, suppress
from pathlib import Path
from typing import TypeAlias, cast, final
from uuid import UUID, uuid5

from zekam.application.client_lifecycle_spool import ClientLifecycleSpool, LifecycleSpoolEntry
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_source_authority import (
    FileIdentity,
    LocalBindingRevision,
    PortableSourcePlanRecord,
)
from zekam.application.local_continuity_source_plan import MAX_SOURCE_BYTES, ContinuitySourcePlan
from zekam.application.local_continuity_v4_compaction import (
    PRECOMPACT_FINAL_RESERVE_MS,
    PreCompactionFailure,
    PreCompactionResult,
    PreparedPreCompactionPlan,
    ResolvedPreCompactionBinding,
    SealedPreCompactionDeadline,
    VerifiedAckDecision,
    _checkpoint_ready,
    _issue_ack_decision,
    _issue_deadline,
    _issue_plan,
    recovery_required,
    rejected,
)
from zekam.application.local_continuity_v4_ingress import (
    ManagedInvocationSnapshot,
    ManagedProcessSnapshot,
)
from zekam.application.local_continuity_v4_writer import (
    CanonicalManifestProvenance,
    CurrentSourceSnapshot,
    ResolvedManifestFragment,
    internal_receipt_digest,
    revision_digest,
    verify_persisted_context_manifest,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    PolicyViolation,
    ValidationFailed,
)
from zekam.infrastructure.clients.codex_macos_0151_lifecycle import (
    CODEX_MACOS_0151_CONTRACT_SCHEMA,
    CODEX_MACOS_0151_VERSION,
    CodexMacOS0151Event,
    LiveProcessVerificationError,
    TrustedCodex0151ProcessManager,
)
from zekam.infrastructure.local_continuity_source_plan import (
    BoundedContinuitySource,
    _source_authority_held_identity,
    _source_authority_identity,
    _source_authority_parent_chain,
    read_portable_source_plan,
)
from zekam.infrastructure.macos_precompaction_supervisor import (
    _DarwinGenerationOwner,
    _generation_digest_if_current,
)
from zekam.infrastructure.sqlite import operational_schema
from zekam.infrastructure.sqlite.continuity_native_verifier import verify_reviewed_hook_commands
from zekam.infrastructure.sqlite.local_continuity_source_authority import (
    _validate as _validate_source_authority,
)
from zekam.infrastructure.sqlite.local_continuity_source_authority import (
    _validated_candidate,
    local_source_authority_path,
)
from zekam.infrastructure.sqlite.local_continuity_v4_writer import SQLiteDormantV4CloseWriter

_NS = UUID("018f0000-0000-7000-8000-000000000152")
_TOPOLOGY = "native-fork-shell-exec-launcher-exec-runtime/v1"
_SUCCESS_DIGEST = "sha256:83b0c2d644685886e897a47420a509055cd62bdc37be550ee96b839cdb1028be"
Db: TypeAlias = sqlite3.Connection  # noqa: UP040 -- runtime also supports Python 3.11


def _digest_text(value: str) -> str:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(char not in "0123456789abcdef" for char in value[7:])
    ):
        raise ValidationFailed("PreCompact digest required")
    return value


def _resolve_existing_binding(db: Db, event: CodexMacOS0151Event) -> ResolvedPreCompactionBinding:
    """Resolve exactly one open Codex binding and its current hydrated/replay head."""
    if type(event) is not CodexMacOS0151Event or event.event_type != "PreCompact":
        raise ValidationFailed("PreCompact exact raw selector event required")
    event.__post_init__()
    rows = db.execute(
        "select b.session_id,b.external_session_id,b.project_id,b.realm_id,b.work_item_id,"
        "b.run_id,b.client_id,b.device_id,b.source_snapshot_id,b.task_digest,b.plan_digest,"
        "b.policy_digest,b.binding_digest,a.attachment_id,a.attachment_digest,"
        "r.revision_digest,r.revision_number,r.previous_revision_digest,r.state,"
        "r.active_manifest_digest,r.active_hydration_receipt_digest,n.delivery_id "
        "from continuity_session_binding b join session s on s.id=b.session_id "
        "join continuity_hook_attachment a on a.session_id=b.session_id "
        "join continuity_hook_attachment_revision r on r.attachment_id=a.attachment_id "
        "and r.revision_number=(select max(x.revision_number) from "
        "continuity_hook_attachment_revision x where x.attachment_id=a.attachment_id) "
        "join source_snapshot ss on ss.id=b.source_snapshot_id "
        "join source_binding sb on sb.id=ss.source_binding_id and sb.project_id=b.project_id "
        "join project p on p.id=b.project_id "
        "join context_manifest m on m.manifest_digest=r.active_manifest_digest "
        "and m.session_id=b.session_id join hydration_receipt h "
        "on h.receipt_digest=r.active_hydration_receipt_digest "
        "and h.session_id=b.session_id and h.manifest_digest=m.manifest_digest "
        "left join continuity_native_event_receipt n "
        "on n.event_digest=r.pre_compaction_event_digest "
        "where b.client_id='codex' and b.external_session_id=? and s.status='open' "
        "and sb.active=1 and sb.source_kind='git' and p.status='active' limit 2",
        (event.external_session_id,),
    ).fetchall()
    if len(rows) != 1:
        raise PolicyViolation("PreCompact exact-one active binding required")
    row = rows[0]
    binding = ContinuityBinding(
        str(row["session_id"]),
        str(row["external_session_id"]),
        str(row["project_id"]),
        str(row["realm_id"]),
        str(row["client_id"]),
        str(row["device_id"]),
        str(row["source_snapshot_id"]),
        str(row["task_digest"]),
        str(row["plan_digest"]),
        str(row["policy_digest"]),
        None if row["work_item_id"] is None else str(row["work_item_id"]),
        None if row["run_id"] is None else str(row["run_id"]),
    )
    SQLiteDormantV4CloseWriter._binding(db, binding)
    attachment = SQLiteDormantV4CloseWriter._attachment(db, binding)
    current = SQLiteDormantV4CloseWriter._current_revision(db, str(row["attachment_id"]))
    if (
        attachment["attachment_id"] != row["attachment_id"]
        or current["revision_digest"] != row["revision_digest"]
    ):
        raise PolicyViolation("PreCompact resolved attachment head drift")
    state = str(current["state"])
    anchor = current
    if state == "pre-compact-committed":
        predecessor_digest = current["previous_revision_digest"]
        predecessor = db.execute(
            "select * from continuity_hook_attachment_revision where revision_digest=?",
            (predecessor_digest,),
        ).fetchone()
        anchor = SQLiteDormantV4CloseWriter._verified_revision(predecessor)
    delivery = digest(
        {
            "schema": "zekam-codex-0151-delivery/v1",
            "session_id": event.external_session_id,
            "external_event_type": "PreCompact",
            "turn_id": event.turn_id,
            "trigger": event.trigger,
            "wire_digest": event.wire_digest,
        }
    )
    if (
        binding.binding_digest != row["binding_digest"]
        or state not in {"hydrated", "pre-compact-committed"}
        or anchor["state"] != "hydrated"
        or current["active_manifest_digest"] != anchor["active_manifest_digest"]
        or current["active_hydration_receipt_digest"] != anchor["active_hydration_receipt_digest"]
        or anchor["active_manifest_digest"] is None
        or anchor["active_hydration_receipt_digest"] is None
        or (state == "pre-compact-committed" and row["delivery_id"] != delivery)
        or (state == "hydrated" and row["delivery_id"] is not None)
    ):
        raise PolicyViolation("PreCompact active hydrated binding evidence drift")
    body = {
        "schema": "zekam-precompact-existing-binding-resolution/v1",
        "binding_digest": binding.binding_digest,
        "attachment_id": str(row["attachment_id"]),
        "head_revision_digest": str(row["revision_digest"]),
        "head_state": state,
        "active_manifest_digest": str(anchor["active_manifest_digest"]),
        "active_hydration_receipt_digest": str(anchor["active_hydration_receipt_digest"]),
    }
    return ResolvedPreCompactionBinding(
        binding,
        str(row["attachment_id"]),
        str(row["revision_digest"]),
        state,
        str(anchor["active_manifest_digest"]),
        str(anchor["active_hydration_receipt_digest"]),
        digest(body),
    )


def resolve_existing_precompaction_binding(
    path: Path, event: CodexMacOS0151Event, *, cwd: Path
) -> ResolvedPreCompactionBinding:
    if (
        type(path) is not type(Path())
        or not path.is_absolute()
        or path.is_symlink()
        or type(cwd) is not type(Path())
        or not cwd.is_absolute()
    ):
        raise ValidationFailed("PreCompact exact operational database path required")
    before = path.stat(follow_symlinks=False)
    with closing(
        sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.0)
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("pragma query_only=on")
        db.execute("pragma foreign_keys=on")
        db.execute("pragma busy_timeout=0")
        db.execute("begin")
        if operational_schema._validate_connection(db) != 4:
            raise ConfigurationError("PreCompact corrected explicit V4 required")
        resolved = _resolve_existing_binding(db, event)
        source_row = db.execute(
            "select ss.source_binding_id from source_snapshot ss where ss.id=?",
            (resolved.binding.source_snapshot_id,),
        ).fetchone()
        if source_row is None:
            raise PolicyViolation("PreCompact resolved source binding unavailable")
        source_binding_id = str(source_row[0])
        db.rollback()
    after = path.stat(follow_symlinks=False)
    if (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
    ):
        raise PolicyViolation("PreCompact operational database identity drift")
    home = path.parent.parent
    side_path = local_source_authority_path(home)
    _GenerationSource._file_ok(side_path)
    with closing(
        sqlite3.connect(f"{side_path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.0)
    ) as side:
        side.row_factory = sqlite3.Row
        side.execute("pragma foreign_keys=on")
        side.execute("pragma query_only=on")
        side.execute("pragma busy_timeout=0")
        side.execute("begin")
        _validate_source_authority(side, physical=False)
        candidates = side.execute(
            "select r.*,cast(r.body_json as blob) as body_blob,h.previous_generation,"
            "h.previous_revision_digest as head_previous from local_source_binding_revision r "
            "join local_source_binding_head h on h.device_id=r.device_id and "
            "h.source_binding_id=r.source_binding_id and h.generation=r.generation and "
            "h.revision_digest=r.revision_digest where r.device_id=? and "
            "r.source_binding_id=? and r.project_id=? order by r.generation desc limit 1",
            (resolved.binding.device_id, source_binding_id, resolved.binding.project_id),
        ).fetchall()
        if len(candidates) != 1:
            raise PolicyViolation("PreCompact exact-one local source authority required")
        candidate = _validated_candidate(candidates[0])
        side.rollback()
    try:
        expected_root = Path(candidate.root_path).resolve(strict=True)
        actual_root = cwd.resolve(strict=True)
    except OSError as exc:
        raise PolicyViolation("PreCompact resolved source root unavailable") from exc
    if expected_root != actual_root:
        raise PolicyViolation("PreCompact raw cwd/resolved source mismatch")
    return resolved


def rollover_existing_precompaction_process(
    path: Path,
    event: CodexMacOS0151Event,
    cwd: Path,
    resolved: ResolvedPreCompactionBinding,
    manager: TrustedCodex0151ProcessManager,
) -> ResolvedPreCompactionBinding:
    """Append one orderly native generation before a resumed-process PreCompact."""
    from zekam.infrastructure.sqlite.local_continuity_v4_ingress import (
        _insert_revision as insert_revision,
    )
    from zekam.infrastructure.sqlite.local_continuity_v4_ingress import (
        _revision_body as attachment_revision_body,
    )

    if type(resolved) is not ResolvedPreCompactionBinding or resolved.head_state not in {
        "hydrated",
        "pre-compact-committed",
    }:
        raise ValidationFailed("PreCompact exact resolved rollover anchor required")
    process = manager.capture_process(resolved.binding)
    if type(process) is not ManagedProcessSnapshot:
        raise ValidationFailed("PreCompact exact rollover process snapshot required")
    process.__post_init__()
    if process.attachment_id != resolved.attachment_id:
        raise PolicyViolation("PreCompact rollover attachment mismatch")
    manager.assert_process(process)
    committed = False
    expected_generation: str | None = None
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True, timeout=0.0)
    db.row_factory = sqlite3.Row
    try:
        db.execute("pragma foreign_keys=on")
        db.execute("pragma busy_timeout=0")
        db.execute("begin immediate")
        if operational_schema._validate_connection(db) != 4:
            raise PolicyViolation("PreCompact rollover corrected v4 required")
        current = _resolve_existing_binding(db, event)
        if current != resolved:
            raise PolicyViolation("PreCompact rollover binding/head drift")
        generations = db.execute(
            "select * from continuity_hook_process_generation where attachment_id=? "
            "order by generation desc limit 2",
            (resolved.attachment_id,),
        ).fetchall()
        if not generations:
            raise PolicyViolation("PreCompact rollover predecessor missing")
        latest = generations[0]
        generation_count = int(
            db.execute(
                "select count(*) from continuity_hook_process_generation where attachment_id=?",
                (resolved.attachment_id,),
            ).fetchone()[0]
        )
        if (
            int(latest["generation"]) != generation_count
            or generation_count > 64
            or (
                len(generations) == 2
                and latest["previous_process_generation_digest"]
                != generations[1]["process_generation_digest"]
            )
        ):
            raise PolicyViolation("PreCompact rollover generation chain drift")
        same_process = (
            latest["native_pid"],
            latest["native_uid"],
            latest["native_start_token"],
            latest["native_artifact_digest"],
            latest["hook_set_digest"],
            latest["ancestry_policy_digest"],
        ) == (
            process.native_pid,
            process.native_uid,
            process.native_start_token,
            process.native_artifact_digest,
            process.hook_set_digest,
            process.ancestry_policy_digest,
        )
        if same_process:
            db.rollback()
            return current
        if current.head_state != "hydrated":
            raise PolicyViolation("PreCompact rollover after durable event rejected")
        SQLiteDormantV4CloseWriter._no_pending(db, resolved.binding)
        generation_number = int(latest["generation"]) + 1
        if generation_number > 64:
            raise PolicyViolation("PreCompact rollover generation cap reached")
        previous_generation = str(latest["process_generation_digest"])
        managed_body = {
            "ancestry_policy_digest": process.ancestry_policy_digest,
            "attachment_id": resolved.attachment_id,
            "created_at": process.captured_at,
            "hook_set_digest": process.hook_set_digest,
            "native_artifact_digest": process.native_artifact_digest,
            "native_pid": process.native_pid,
            "native_start_token": process.native_start_token,
            "native_uid": process.native_uid,
            "predecessor_process_generation_digest": previous_generation,
            "transition_kind": "orderly-reattach",
        }
        managed_digest = digest(managed_body)
        generation_body = {
            "ancestry_policy_digest": process.ancestry_policy_digest,
            "attachment_id": resolved.attachment_id,
            "created_at": process.captured_at,
            "generation": generation_number,
            "hook_set_digest": process.hook_set_digest,
            "managed_launch_receipt_digest": managed_digest,
            "native_artifact_digest": process.native_artifact_digest,
            "native_pid": process.native_pid,
            "native_start_token": process.native_start_token,
            "native_uid": process.native_uid,
            "previous_process_generation_digest": previous_generation,
        }
        expected_generation = digest(generation_body)
        db.execute(
            "insert into continuity_managed_process_receipt values(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                managed_digest,
                resolved.attachment_id,
                previous_generation,
                process.native_pid,
                process.native_uid,
                process.native_start_token,
                process.native_artifact_digest,
                process.hook_set_digest,
                process.ancestry_policy_digest,
                "orderly-reattach",
                canonical_json(managed_body),
                process.captured_at,
            ),
        )
        db.execute(
            "insert into continuity_hook_process_generation values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                expected_generation,
                resolved.attachment_id,
                generation_number,
                process.native_pid,
                process.native_uid,
                process.native_start_token,
                process.native_artifact_digest,
                process.hook_set_digest,
                process.ancestry_policy_digest,
                previous_generation,
                managed_digest,
                canonical_json(generation_body),
                process.captured_at,
            ),
        )
        attached = insert_revision(
            db,
            attachment_revision_body(
                attachment_id=resolved.attachment_id,
                revision_number=int(
                    db.execute(
                        "select max(revision_number) from continuity_hook_attachment_revision "
                        "where attachment_id=?",
                        (resolved.attachment_id,),
                    ).fetchone()[0]
                )
                + 1,
                previous_revision_digest=resolved.head_revision_digest,
                operation_key=f"codex0151-orderly-reattach-{expected_generation}",
                state="attached",
                process_generation_digest=expected_generation,
                created_at=process.captured_at,
            ),
        )
        next_number = (
            int(
                db.execute(
                    "select max(revision_number) from continuity_hook_attachment_revision "
                    "where attachment_id=?",
                    (resolved.attachment_id,),
                ).fetchone()[0]
            )
            + 1
        )
        insert_revision(
            db,
            attachment_revision_body(
                attachment_id=resolved.attachment_id,
                revision_number=next_number,
                previous_revision_digest=attached,
                operation_key=f"codex0151-orderly-rehydrate-{expected_generation}",
                state="hydrated",
                process_generation_digest=expected_generation,
                active_manifest_digest=resolved.active_manifest_digest,
                active_hydration_receipt_digest=resolved.active_hydration_receipt_digest,
                created_at=process.captured_at,
            ),
        )
        manager.assert_process(process)
        committed = True
        db.commit()
    except sqlite3.Error as exc:
        with suppress(sqlite3.Error):
            db.rollback()
        if not committed or expected_generation is None:
            raise ConcurrencyConflict("PreCompact rollover transaction conflict") from exc
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    verified = resolve_existing_precompaction_binding(path, event, cwd=cwd)
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as check:
        row = check.execute(
            "select process_generation_digest from continuity_hook_attachment_revision "
            "where revision_digest=?",
            (verified.head_revision_digest,),
        ).fetchone()
    if row is None or row[0] != expected_generation:
        raise PolicyViolation("PreCompact rollover durable census mismatch")
    return verified


class _CompactionGateError(PolicyViolation):
    def __init__(self, category: PreCompactionFailure) -> None:
        self.category = category
        super().__init__("PreCompact fixed gate failure")


@final
class _GenerationSource:
    """Read-only Gate-A authority held for one exact PreCompact request."""

    __slots__ = (
        "_candidate",
        "_captured",
        "_db",
        "_generation",
        "_home",
        "_operational_fd",
        "_operational_identity",
        "_operational_parent",
        "_path",
        "_record",
        "_resolver",
        "_side_fd",
        "_side_identity",
        "_snapshot",
        "_source",
    )

    def __init__(self, generation: _DarwinGenerationOwner, path: Path | None = None) -> None:
        _generation_digest_if_current(generation)
        self._generation = generation
        self._path = Path("/nonexistent/operational.db") if path is None else path
        self._home = self._path.parent.parent
        self._db: Db | None = None
        self._side_fd: int | None = None
        self._operational_fd: int | None = None
        self._candidate: LocalBindingRevision | None = None
        self._record: PortableSourcePlanRecord | None = None
        self._resolver: object | None = None
        self._source: BoundedContinuitySource | None = None
        self._captured: ContinuitySourcePlan | None = None
        self._snapshot: CurrentSourceSnapshot | None = None
        self._side_identity: FileIdentity | None = None
        self._operational_identity: FileIdentity | None = None
        self._operational_parent: str | None = None

    def close(self) -> None:
        failure: BaseException | None = None
        if self._db is not None:
            try:
                if self._db.in_transaction:
                    self._db.rollback()
                self._db.close()
            except BaseException as exc:
                failure = exc
            self._db = None
        for name in ("_operational_fd", "_side_fd"):
            descriptor = getattr(self, name)
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    failure = failure or exc
                setattr(self, name, None)
        self._candidate = self._record = self._source = self._captured = self._snapshot = None
        self._resolver = None
        if failure is not None:
            raise failure

    @staticmethod
    def _file_ok(path: Path) -> None:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise PolicyViolation("PreCompact source authority file identity rejected")

    def _unchanged(self, binding: ContinuityBinding) -> None:
        if any(
            value is None
            for value in (
                self._db,
                self._side_fd,
                self._operational_fd,
                self._candidate,
                self._record,
                self._source,
                self._captured,
                self._snapshot,
            )
        ):
            raise PolicyViolation("PreCompact source authority is not held")
        assert self._db is not None and self._side_fd is not None
        assert self._operational_fd is not None and self._candidate is not None
        assert self._record is not None and self._source is not None
        side_path = local_source_authority_path(self._home)
        self._file_ok(side_path)
        for suffix in ("-journal", "-wal", "-shm"):
            try:
                Path(str(side_path) + suffix).lstat()
            except FileNotFoundError:
                continue
            raise PolicyViolation("PreCompact source authority side file rejected")
        if (
            _source_authority_held_identity(self._side_fd) != self._side_identity
            or _source_authority_identity(side_path, regular=True) != self._side_identity
            or _source_authority_held_identity(self._operational_fd) != self._operational_identity
            or _source_authority_identity(self._path, regular=True) != self._operational_identity
            or _source_authority_parent_chain(self._path, self._home) != self._operational_parent
            or self._candidate.device_id != binding.device_id
            or self._candidate.project_id != binding.project_id
        ):
            raise PolicyViolation("PreCompact source authority identity drift")
        row = self._db.execute(
            "select r.*,cast(r.body_json as blob) as body_blob,h.previous_generation,"
            "h.previous_revision_digest as head_previous from local_source_binding_revision r "
            "join local_source_binding_head h on h.device_id=r.device_id and "
            "h.source_binding_id=r.source_binding_id and h.generation=r.generation and "
            "h.revision_digest=r.revision_digest where r.revision_digest=?",
            (self._candidate.revision_digest,),
        ).fetchone()
        if row is None or _validated_candidate(row) != self._candidate:
            raise PolicyViolation("PreCompact source authority revision drift")
        if (
            read_portable_source_plan(
                self._home, binding.project_id, self._record.plan.content_digest
            )
            != self._record
            or self._source.capture() != self._captured
        ):
            raise PolicyViolation("PreCompact source authority evidence drift")

    def snapshot(
        self, binding: ContinuityBinding, deadline: SealedPreCompactionDeadline
    ) -> CurrentSourceSnapshot:
        deadline.assert_generation(self._generation)
        binding.__post_init__()
        if self._path.name != "operational.db" or self._path.parent.name != "state":
            raise ConfigurationError("PreCompact fixed operational layout required")
        self.close()
        side_path = local_source_authority_path(self._home)
        self._file_ok(side_path)
        self._side_identity = _source_authority_identity(side_path, regular=True)
        self._operational_identity = _source_authority_identity(self._path, regular=True)
        self._operational_parent = _source_authority_parent_chain(self._path, self._home)
        self._side_fd = os.open(side_path, os.O_RDONLY | os.O_NOFOLLOW)
        self._operational_fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if (
                _source_authority_held_identity(self._side_fd) != self._side_identity
                or _source_authority_held_identity(self._operational_fd)
                != self._operational_identity
            ):
                raise PolicyViolation("PreCompact source authority open identity drift")
            self._db = sqlite3.connect(
                f"file:/dev/fd/{self._side_fd}?mode=ro", uri=True, timeout=0.0
            )
            self._db.row_factory = sqlite3.Row
            self._db.execute("pragma foreign_keys=on")
            self._db.execute("pragma query_only=on")
            self._db.execute("pragma busy_timeout=0")
            self._db.execute("begin")
            _validate_source_authority(self._db, physical=False)
            with closing(
                sqlite3.connect(f"{self._path.resolve().as_uri()}?mode=ro", uri=True, timeout=0.0)
            ) as operational:
                operational.row_factory = sqlite3.Row
                operational.execute("pragma query_only=on")
                operational.execute("begin")
                if operational_schema._validate_connection(operational) != 4:
                    raise PolicyViolation("PreCompact corrected V4 source required")
                source_rows = operational.execute(
                    "select s.id,s.source_binding_id,s.revision_ref,s.tree_digest,"
                    "s.content_digest,s.config_digest from source_snapshot s join source_binding b "
                    "on b.id=s.source_binding_id join project p on p.id=b.project_id where s.id=? "
                    "and b.project_id=? and b.active=1 and b.source_kind='git' "
                    "and p.status='active'",
                    (binding.source_snapshot_id, binding.project_id),
                ).fetchall()
                latest = operational.execute(
                    "select id from source_snapshot where source_binding_id="
                    "(select source_binding_id "
                    "from source_snapshot where id=?) order by captured_at desc,id desc limit 2",
                    (binding.source_snapshot_id,),
                ).fetchall()
            if (
                len(source_rows) != 1
                or len(latest) != 1
                or latest[0][0] != binding.source_snapshot_id
            ):
                raise PolicyViolation("PreCompact operational source authority drift")
            source_row = source_rows[0]
            source_binding_id, content = str(source_row[1]), str(source_row[4])
            candidate_row = self._db.execute(
                "select r.*,cast(r.body_json as blob) as body_blob,h.previous_generation,"
                "h.previous_revision_digest as head_previous from local_source_binding_revision r "
                "join local_source_binding_head h on h.device_id=r.device_id and "
                "h.source_binding_id=r.source_binding_id and h.generation=r.generation and "
                "h.revision_digest=r.revision_digest where r.device_id=? and r.source_binding_id=? "
                "order by r.generation desc limit 1",
                (binding.device_id, source_binding_id),
            ).fetchone()
            if candidate_row is None:
                raise PolicyViolation("PreCompact local source authority missing")
            self._candidate = _validated_candidate(candidate_row)
            self._record = read_portable_source_plan(self._home, binding.project_id, content)
            plan = self._record.plan
            if (
                self._record.source_snapshot_id != binding.source_snapshot_id
                or self._candidate.portable_plan_digest != content
                or self._candidate.operational_identity != self._operational_identity
                or self._candidate.parent_chain_digest != self._operational_parent
                or self._candidate.source_binding_id != source_binding_id
                or _source_authority_identity(Path(self._candidate.root_path), regular=False)
                != self._candidate.root_identity
                or (plan.revision_ref, plan.tree_digest, plan.content_digest, plan.config_digest)
                != tuple(source_row[2:6])
                or (plan.recipe.realm_id, plan.recipe.task_digest, plan.recipe.policy_digest)
                != (binding.realm_id, binding.task_digest, binding.policy_digest)
            ):
                raise PolicyViolation("PreCompact source authority relation drift")
            self._source = BoundedContinuitySource(Path(self._candidate.root_path), plan.recipe)
            self._captured = self._source.capture()
            if self._captured != plan:
                raise PolicyViolation("PreCompact source authority capture drift")
            self._snapshot = CurrentSourceSnapshot(
                binding.source_snapshot_id,
                plan.revision_ref,
                digest(
                    {
                        "schema": "zekam-current-source-snapshot/v1",
                        "source_snapshot_id": binding.source_snapshot_id,
                        "revision_ref": plan.revision_ref,
                        "tree_digest": plan.tree_digest,
                        "content_digest": plan.content_digest,
                        "config_digest": plan.config_digest,
                    }
                ),
            )
            from zekam.infrastructure.local_continuity_v4_composition import (
                _DormantV4ContinuityRead,
            )
            from zekam.infrastructure.local_startup_composition import _BoundedProjectSource
            from zekam.infrastructure.sqlite.local_continuity_startup import (
                SQLiteStartupSourceResolver,
            )

            self._resolver = SQLiteStartupSourceResolver(
                _DormantV4ContinuityRead(self._path),
                _BoundedProjectSource(self._source, self._captured, binding),
            )
            self._unchanged(binding)
            return self._snapshot
        except BaseException:
            self.close()
            raise

    def assert_current(
        self,
        binding: ContinuityBinding,
        snapshot: CurrentSourceSnapshot,
        deadline: SealedPreCompactionDeadline,
    ) -> None:
        deadline.assert_generation(self._generation)
        if type(snapshot) is not CurrentSourceSnapshot or snapshot != self._snapshot:
            raise PolicyViolation("PreCompact source snapshot drift")
        self._unchanged(binding)
        deadline.require_current()

    def resolve_fragment(
        self,
        binding: ContinuityBinding,
        snapshot: CurrentSourceSnapshot,
        provenance: CanonicalManifestProvenance,
        deadline: SealedPreCompactionDeadline,
    ) -> ResolvedManifestFragment:
        self.assert_current(binding, snapshot, deadline)
        if (
            type(provenance) is not CanonicalManifestProvenance
            or self._source is None
            or self._resolver is None
        ):
            raise ValidationFailed("PreCompact exact source provenance required")
        provenance.__post_init__()
        body = json.loads(provenance.body_json)
        source_ref = body.get("source_ref")
        if "kind" not in body:
            if source_ref not in self._source.recipe.allowed_paths:
                raise PolicyViolation("PreCompact source reference outside portable plan")
            payload = self._source._read(str(source_ref), MAX_SOURCE_BYTES)
            if payload is None:
                raise PolicyViolation("PreCompact source fragment unavailable")
            try:
                text = payload.decode("utf-8")
            except UnicodeError as exc:
                raise PolicyViolation("PreCompact source fragment encoding drift") from exc
            if body.get("revision") != snapshot.revision_ref or body.get("digest") != digest(text):
                raise PolicyViolation("PreCompact source fragment provenance drift")
            return ResolvedManifestFragment(provenance.candidate_id, text)
        from zekam.infrastructure.sqlite.local_continuity_startup import (
            SQLiteStartupSourceResolver,
        )

        if type(self._resolver) is not SQLiteStartupSourceResolver:
            raise PolicyViolation("PreCompact exact startup resolver unavailable")
        text = self._resolver(binding, body)
        return ResolvedManifestFragment(provenance.candidate_id, text)


@final
class _GenerationDurability:
    __slots__ = ("_generation",)

    def __init__(self, generation: _DarwinGenerationOwner) -> None:
        _generation_digest_if_current(generation)
        self._generation = generation

    def verify(self, db: Db, deadline: SealedPreCompactionDeadline, *, read_only: bool) -> None:
        deadline.assert_generation(self._generation)
        if type(read_only) is not bool:
            raise PolicyViolation("PreCompact durability boundary state invalid")
        if int(db.execute("pragma foreign_keys").fetchone()[0]) != 1:
            raise ConfigurationError("PreCompact foreign keys unavailable")
        if read_only != bool(db.execute("pragma query_only").fetchone()[0]):
            raise ConfigurationError("PreCompact query-only state drift")


def _uuid(binding: ContinuityBinding, delivery: str, role: str) -> str:
    return str(uuid5(_NS, f"{binding.binding_digest}|{delivery}|{role}"))


def _key(delivery: str, suffix: str) -> str:
    value = f"precompact:{delivery}:{suffix}"
    if len(value.encode("utf-8")) > 512:
        raise ValidationFailed("PreCompact operation key outside bound")
    return value


def _event_envelope(
    binding: ContinuityBinding,
    *,
    sequence: int,
    previous: str,
    kind: str,
    key: str,
    occurred_at: str,
    evidence: list[str],
    spool_digest: str | None,
) -> dict[str, object]:
    event = {
        "kind": kind,
        "idempotency_key": key,
        "occurred_at": occurred_at,
        "source_refs": [],
        "evidence_digests": evidence,
        "spool_digest": spool_digest,
    }
    return {
        "session_id": binding.session_id,
        "binding_digest": binding.binding_digest,
        "sequence": sequence,
        "previous_digest": previous,
        "event": event,
    }


def _revision_body(predecessor: sqlite3.Row, plan: dict[str, object]) -> dict[str, object]:
    return {
        "attachment_id": predecessor["attachment_id"],
        "revision_number": int(predecessor["revision_number"]) + 1,
        "previous_revision_digest": predecessor["revision_digest"],
        "operation_key": plan["revision_key"],
        "state": "pre-compact-committed",
        "process_generation_digest": predecessor["process_generation_digest"],
        "active_manifest_digest": predecessor["active_manifest_digest"],
        "active_hydration_receipt_digest": predecessor["active_hydration_receipt_digest"],
        "checkpoint_digest": plan["checkpoint_digest"],
        "pre_compaction_event_digest": plan["pre_digest"],
        "post_compaction_event_digest": None,
        "close_request_digest": None,
        "pre_close_event_digest": None,
        "close_receipt_digest": None,
        "session_closed_event_digest": None,
        "hook_recovery_case_id": predecessor["hook_recovery_case_id"],
        "hook_recovery_resolution_id": predecessor["hook_recovery_resolution_id"],
        "local_recovery_case_id": predecessor["local_recovery_case_id"],
        "local_recovery_resolution_id": predecessor["local_recovery_resolution_id"],
        "crash_recovered_event_digest": predecessor["crash_recovered_event_digest"],
        "crash_recovered_receipt_digest": predecessor["crash_recovered_receipt_digest"],
        "created_at": plan["observed_at"],
    }


class _OwnedImmediateTransaction:
    __slots__ = ("__db", "plan_digest", "state")

    def __init__(self, db: Db) -> None:
        self.__db = db
        self.state = "open-clean"
        self.plan_digest: str | None = None
        db.execute("begin immediate")

    def __getattr__(self, name: str) -> object:
        if name == "db":
            self.abort()
            raise PolicyViolation("PreCompact raw transaction connection is private")
        raise AttributeError(name)

    def planned(self, value: str) -> None:
        if self.state != "open-clean":
            raise PolicyViolation("PreCompact transaction state drift")
        self.state, self.plan_digest = "planned", value

    def applying(self) -> None:
        if self.state != "planned":
            raise PolicyViolation("PreCompact transaction not planned")
        self.state = "applying"

    def apply_precompaction_graph(self, plan: PreparedPreCompactionPlan) -> None:
        """The sole fixed mutation operation; callers never receive the connection."""
        if self.state != "applying" or self.plan_digest != plan.ack_decision_digest:
            raise PolicyViolation("PreCompact fixed transaction operation unavailable")
        ancestry = json.loads(plan.ancestry_body_json)
        stored = {
            "receipt_digest": plan.ancestry_receipt_digest,
            **{key: value for key, value in ancestry.items() if key != "schema"},
        }
        columns = tuple(stored)
        self.__db.execute(
            "insert into continuity_hook_invocation_ancestry_receipt("
            + ",".join(columns)
            + ",body_json) values("
            + ",".join("?" for _ in range(len(columns) + 1))
            + ")",
            (*stored.values(), plan.ancestry_body_json),
        )
        native = json.loads(plan.native_body_json)
        stored = {"receipt_digest": plan.native_receipt_digest, **native}
        columns = tuple(stored)
        self.__db.execute(
            "insert into continuity_native_event_receipt("
            + ",".join(columns)
            + ",body_json) values("
            + ",".join("?" for _ in range(len(columns) + 1))
            + ")",
            (*stored.values(), plan.native_body_json),
        )
        self.__db.execute(
            "insert into continuity_internal_event_receipt("
            "receipt_digest,event_digest,session_id,binding_digest,event_kind,operation_key,"
            "expected_previous_event_digest,native_event_receipt_digest,attachment_revision_digest,"
            "body_json,created_at) values(?,?,?,?,?,?,?,?,?,?,?)",
            (
                plan.checkpoint_receipt_digest,
                plan.checkpoint_event_digest,
                plan.binding.session_id,
                plan.binding.binding_digest,
                "CHECKPOINT_REQUESTED",
                _key(plan.delivery_id, "checkpoint-requested"),
                plan.old_event_digest,
                plan.native_receipt_digest,
                plan.predecessor_revision_digest,
                plan.checkpoint_receipt_body_json,
                plan.observed_at,
            ),
        )
        for event_id, kind, event_digest_value, body_json, spool in (
            (
                plan.checkpoint_event_id,
                "CHECKPOINT_REQUESTED",
                plan.checkpoint_event_digest,
                plan.checkpoint_event_body_json,
                None,
            ),
            (
                plan.precompact_event_id,
                "PRE_COMPACTION",
                plan.precompact_event_digest,
                plan.precompact_event_body_json,
                plan.spool_entry_digest,
            ),
        ):
            body = json.loads(body_json)
            self.__db.execute(
                "insert into session_event values(?,?,?,?,?)",
                (event_id, plan.binding.session_id, kind, event_digest_value, plan.observed_at),
            )
            self.__db.execute(
                "insert into session_event_detail values(?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    plan.binding.session_id,
                    body["sequence"],
                    body["previous_digest"],
                    body["event"]["idempotency_key"],
                    event_digest_value,
                    spool,
                    body_json,
                ),
            )
        checkpoint = json.loads(plan.checkpoint_body_json)
        self.__db.execute(
            "insert into continuity_checkpoint values(?,?,?,?,?,?,?,?,?,?)",
            (
                plan.checkpoint_digest,
                plan.binding.session_id,
                checkpoint["idempotency_key"],
                checkpoint["covered_sequence"],
                plan.precompact_event_digest,
                plan.binding.source_snapshot_id,
                plan.active_manifest_digest,
                checkpoint["spool_digest"],
                plan.checkpoint_body_json,
                plan.observed_at,
            ),
        )
        revision = json.loads(plan.revision_body_json)
        stored = {"revision_digest": plan.revision_digest, **revision}
        columns = tuple(stored)
        self.__db.execute(
            "insert into continuity_hook_attachment_revision("
            + ",".join(columns)
            + ",body_json) values("
            + ",".join("?" for _ in range(len(columns) + 1))
            + ")",
            (*stored.values(), canonical_json(stored)),
        )

    def verified(self, value: str) -> None:
        if self.state not in {"planned", "applying"} or self.plan_digest != value:
            raise PolicyViolation("PreCompact transaction verification drift")
        self.state = "verified"

    def commit_verified(self, value: str, deadline: SealedPreCompactionDeadline) -> None:
        if type(deadline) is not SealedPreCompactionDeadline:
            raise ValidationFailed("PreCompact exact deadline required")
        deadline.require_current(reserve_ms=PRECOMPACT_FINAL_RESERVE_MS)
        if self.state != "verified" or self.plan_digest != value:
            self.__db.rollback()
            self.state = "rolled-back"
            raise PolicyViolation("PreCompact unverified commit rejected")
        self.__db.commit()
        deadline.require_current()
        self.state = "committed"

    def abort(self) -> None:
        if self.__db.in_transaction:
            self.__db.rollback()
        self.state = "rolled-back"


@final
class _SQLiteDormantV4PreCompactionWriter:
    """Explicit V4-only writer; construction and use do not activate hooks."""

    def __init__(
        self,
        path: Path,
        binding: ContinuityBinding,
        *,
        spool: ClientLifecycleSpool,
        generation: _DarwinGenerationOwner,
    ) -> None:
        if type(path) is not type(Path()) or not path.is_absolute():
            raise ValidationFailed("PreCompact exact absolute database path required")
        if type(binding) is not ContinuityBinding or type(spool) is not ClientLifecycleSpool:
            raise ValidationFailed("PreCompact exact binding/spool required")
        binding.__post_init__()
        _generation_digest_if_current(generation)
        self.path, self.binding = path, binding
        self.resolved: ResolvedPreCompactionBinding | None = None
        self.generation = generation
        self._last_decision: VerifiedAckDecision | None = None
        self.process_manager = TrustedCodex0151ProcessManager(
            getattr(generation, "_artifacts", None)
        )
        self.source = _GenerationSource(generation, path)
        self.spool = spool
        self.durability = _GenerationDurability(generation)

    def _connect(self, deadline: SealedPreCompactionDeadline, *, read_only: bool = False) -> Db:
        deadline.assert_generation(self.generation)
        if self.path.is_symlink() or not self.path.is_file():
            raise ConfigurationError("PreCompact existing regular database required")
        remaining = deadline.remaining_seconds(reserve_ms=PRECOMPACT_FINAL_RESERVE_MS)
        mode = "ro" if read_only else "rw"
        db = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode={mode}", uri=True, timeout=min(5.0, remaining)
        )
        db.row_factory = sqlite3.Row
        db.execute("pragma foreign_keys=on")
        db.execute(f"pragma busy_timeout={max(1, int(min(5.0, remaining) * 1000))}")
        if db.execute("pragma foreign_keys").fetchone()[0] != 1:
            db.close()
            raise ConfigurationError("PreCompact SQLite foreign keys unavailable")
        if read_only:
            db.execute("pragma query_only=on")
        try:
            self.durability.verify(db, deadline, read_only=read_only)
        except BaseException:
            db.close()
            raise
        return db

    @staticmethod
    def _schema(db: Db) -> None:
        if operational_schema._validate_connection(db) != 4:
            raise ConfigurationError("PreCompact corrected explicit V4 required")

    @staticmethod
    def _current(db: Db, attachment_id: str) -> sqlite3.Row:
        return SQLiteDormantV4CloseWriter._current_revision(db, attachment_id)

    @staticmethod
    def _selected_census(
        db: Db,
        binding: ContinuityBinding,
        delivery_id: str,
        deadline: SealedPreCompactionDeadline,
    ) -> str:
        """Bounded operation census; unrelated database rows are never materialized."""
        _digest_text(delivery_id)
        attachment = SQLiteDormantV4CloseWriter._attachment(db, binding)
        attachment_id = str(attachment["attachment_id"])
        keys = tuple(
            _key(delivery_id, suffix)
            for suffix in (
                "ancestry",
                "native",
                "checkpoint-requested",
                "checkpoint",
                "pre-compact",
                "pre-compact-revision",
            )
        )
        event_ids = (
            _uuid(binding, delivery_id, "checkpoint-requested"),
            _uuid(binding, delivery_id, "pre-compact"),
        )
        queries = (
            (
                "revision",
                "select revision_digest,revision_number,state,previous_revision_digest,"
                "checkpoint_digest,pre_compaction_event_digest,active_manifest_digest,"
                "active_hydration_receipt_digest,process_generation_digest,operation_key "
                "from continuity_hook_attachment_revision where attachment_id=? "
                "order by revision_number limit 66",
                (attachment_id,),
                65,
            ),
            (
                "events",
                "select id,event_kind,event_digest,created_at from session_event "
                "where session_id=? order by created_at,id limit 4097",
                (binding.session_id,),
                4096,
            ),
            (
                "details",
                "select event_id,sequence,previous_digest,idempotency_key,event_digest,"
                "spool_digest from session_event_detail where session_id=? "
                "order by sequence limit 4097",
                (binding.session_id,),
                4096,
            ),
            (
                "operation-events",
                "select id,event_kind,event_digest from session_event where id in (?,?) "
                "order by id limit 3",
                event_ids,
                2,
            ),
            (
                "ancestry",
                "select receipt_digest,delivery_id,observation_digest from "
                "continuity_hook_invocation_ancestry_receipt where delivery_id=? limit 2",
                (delivery_id,),
                1,
            ),
            (
                "native",
                "select receipt_digest,delivery_id,event_digest from "
                "continuity_native_event_receipt where delivery_id=? limit 2",
                (delivery_id,),
                1,
            ),
            (
                "internal",
                "select receipt_digest,operation_key,event_digest from "
                "continuity_internal_event_receipt where operation_key=? limit 2",
                (keys[2],),
                1,
            ),
            (
                "checkpoint",
                "select checkpoint_digest,idempotency_key,covered_event_digest from "
                "continuity_checkpoint where idempotency_key=? limit 2",
                (keys[3],),
                1,
            ),
        )
        result: list[tuple[str, tuple[tuple[object, ...], ...]]] = []
        total_bytes = 0
        for label, sql, args, cap in queries:
            deadline.require_current(reserve_ms=PRECOMPACT_FINAL_RESERVE_MS)
            bounded_sql = sql.rsplit(" limit ", maxsplit=1)[0]
            count = int(db.execute(f"select count(*) from ({bounded_sql})", args).fetchone()[0])
            if count > cap:
                raise PolicyViolation("PreCompact selected census cardinality exceeded")
            rows = tuple(tuple(row) for row in db.execute(sql, args))
            encoded = canonical_json(rows).encode("utf-8")
            total_bytes += len(encoded)
            if len(encoded) > 1_048_576 or total_bytes > 2_097_152:
                raise PolicyViolation("PreCompact selected census byte bound exceeded")
            result.append((label, rows))
        return digest(result)

    def _manifest(
        self,
        db: Db,
        predecessor: sqlite3.Row,
        source: CurrentSourceSnapshot,
        deadline: SealedPreCompactionDeadline,
    ) -> None:
        digest_value = str(predecessor["active_manifest_digest"])
        row = db.execute(
            "select manifest_digest,session_id,checkpoint_digest,token_budget,token_count,"
            "typeof(body_json) as body_type,length(cast(body_json as blob)) as body_bytes "
            "from context_manifest where manifest_digest=? and session_id=?",
            (digest_value, self.binding.session_id),
        ).fetchone()
        hydration = db.execute(
            "select * from hydration_receipt where receipt_digest=? and session_id=?",
            (predecessor["active_hydration_receipt_digest"], self.binding.session_id),
        ).fetchone()
        source_row = db.execute(
            "select revision_ref,tree_digest,content_digest,config_digest "
            "from source_snapshot where id=?",
            (self.binding.source_snapshot_id,),
        ).fetchone()
        if (
            row is None
            or hydration is None
            or source_row is None
            or source.source_snapshot_id != self.binding.source_snapshot_id
            or source.revision_ref != str(source_row["revision_ref"])
            or source.snapshot_digest
            != digest(
                {
                    "schema": "zekam-current-source-snapshot/v1",
                    "source_snapshot_id": self.binding.source_snapshot_id,
                    "revision_ref": str(source_row["revision_ref"]),
                    "tree_digest": str(source_row["tree_digest"]),
                    "content_digest": str(source_row["content_digest"]),
                    "config_digest": str(source_row["config_digest"]),
                }
            )
            or row["body_type"] != "text"
            or not 1 <= int(row["body_bytes"]) <= 1_048_576
        ):
            raise _CompactionGateError(PreCompactionFailure.SOURCE_DRIFT)
        body_json = db.execute(
            "select body_json from context_manifest where manifest_digest=?", (digest_value,)
        ).fetchone()[0]
        verified = verify_persisted_context_manifest(
            binding=self.binding,
            manifest_digest=digest_value,
            row_columns=dict(row),
            body_json=body_json,
            active_hydration_receipt=dict(hydration),
            db_source_revision=str(source_row[0]),
            port_source_revision=source.revision_ref,
        )
        fragments = dict(verified.fragments)
        for selected in verified.selected:
            deadline.require_current()
            try:
                resolved = self.source.resolve_fragment(
                    self.binding, source, selected.provenance, deadline
                )
            except (PolicyViolation, ValidationFailed) as exc:
                raise _CompactionGateError(PreCompactionFailure.SOURCE_DRIFT) from exc
            if (
                type(resolved) is not ResolvedManifestFragment
                or resolved.candidate_id != selected.candidate_id
                or resolved.text != fragments[selected.candidate_id]
            ):
                raise _CompactionGateError(PreCompactionFailure.SOURCE_DRIFT)

    def _plan(
        self,
        predecessor: sqlite3.Row,
        rows: list[sqlite3.Row],
        entry: LifecycleSpoolEntry,
        entries: tuple[LifecycleSpoolEntry, ...],
        source: CurrentSourceSnapshot,
        invocation: ManagedInvocationSnapshot,
    ) -> PreparedPreCompactionPlan:
        old = rows[-1]
        observed = invocation.observed_at
        checkpoint_key = _key(entry.delivery_id, "checkpoint-requested")
        pre_key = _key(entry.delivery_id, "pre-compaction")
        first = _event_envelope(
            self.binding,
            sequence=int(old["sequence"]) + 1,
            previous=str(old["event_digest"]),
            kind="CHECKPOINT_REQUESTED",
            key=checkpoint_key,
            occurred_at=observed,
            evidence=[str(predecessor["active_manifest_digest"])],
            spool_digest=None,
        )
        first_digest = digest(first)
        pre = _event_envelope(
            self.binding,
            sequence=int(old["sequence"]) + 2,
            previous=first_digest,
            kind="PRE_COMPACTION",
            key=pre_key,
            occurred_at=observed,
            evidence=[str(predecessor["active_manifest_digest"]), entry.observation_digest],
            spool_digest=entry.entry_digest,
        )
        pre_digest = digest(pre)
        ancestry = {
            "schema": "zekam-hook-invocation-ancestry-receipt/v1",
            "ancestry_policy_digest": invocation.ancestry_policy_digest,
            "approval_inherited": 0,
            "delivery_id": invocation.delivery_id,
            "external_event_type": "PreCompact",
            "grants_authority": 0,
            "hook_parent_pid": invocation.native_pid,
            "hook_parent_start_token": invocation.native_start_token,
            "hook_parent_uid": invocation.native_uid,
            "hook_pid": invocation.hook_pid,
            "hook_start_token": invocation.hook_start_token,
            "hook_uid": invocation.hook_uid,
            "launch_command_digest": invocation.launch_command_digest,
            "native_artifact_digest": invocation.native_artifact_digest,
            "native_pid": invocation.native_pid,
            "native_start_token": invocation.native_start_token,
            "native_uid": invocation.native_uid,
            "observation_digest": invocation.observation_digest,
            "observed_at": observed,
            "process_generation_digest": invocation.process_generation_digest,
            "python_launcher_artifact_digest": invocation.python_launcher_artifact_digest,
            "python_runtime_artifact_digest": invocation.python_runtime_artifact_digest,
            "shell_artifact_digest": invocation.shell_artifact_digest,
            "shell_parent_pid": invocation.native_pid,
            "shell_parent_start_token": invocation.native_start_token,
            "shell_parent_uid": invocation.native_uid,
            "shell_pid": invocation.hook_pid,
            "shell_start_token": invocation.hook_start_token,
            "shell_uid": invocation.hook_uid,
            "topology": _TOPOLOGY,
        }
        ancestry_digest = digest(ancestry)
        native = {
            "ancestry_receipt_digest": ancestry_digest,
            "approval_inherited": 0,
            "attachment_revision_digest": predecessor["revision_digest"],
            "created_at": observed,
            "delivery_id": invocation.delivery_id,
            "event_digest": pre_digest,
            "external_event_type": "PreCompact",
            "external_trigger_id": entry.observation["trigger"],
            "external_turn_id": entry.observation["turn_id"],
            "grants_authority": 0,
            "hook_pid": invocation.hook_pid,
            "hook_start_token": invocation.hook_start_token,
            "hook_uid": invocation.hook_uid,
            "hydration_receipt_digest": None,
            "internal_event_type": "PRE_COMPACTION",
            "observation_digest": invocation.observation_digest,
            "previous_spool_digest": entry.previous_entry_digest,
            "process_generation_digest": invocation.process_generation_digest,
            "python_launcher_artifact_digest": invocation.python_launcher_artifact_digest,
            "python_runtime_artifact_digest": invocation.python_runtime_artifact_digest,
            "shell_artifact_digest": invocation.shell_artifact_digest,
            "shell_pid": invocation.hook_pid,
            "shell_start_token": invocation.hook_start_token,
            "shell_uid": invocation.hook_uid,
            "spool_digest": entry.entry_digest,
            "spool_sequence": entry.sequence,
        }
        native_digest = digest(native)
        receipt = {
            "attachment_revision_digest": predecessor["revision_digest"],
            "binding_digest": self.binding.binding_digest,
            "created_at": observed,
            "event_digest": first_digest,
            "event_kind": "CHECKPOINT_REQUESTED",
            "expected_previous_event_digest": old["event_digest"],
            "operation_key": checkpoint_key,
            "session_id": self.binding.session_id,
        }
        receipt_digest = internal_receipt_digest(
            receipt, producer_kind="native_event_receipt_digest", producer_ref=native_digest
        )
        checkpoint = {
            "session_id": self.binding.session_id,
            "binding_digest": self.binding.binding_digest,
            "covered_sequence": int(old["sequence"]) + 2,
            "covered_event_digest": pre_digest,
            "source_snapshot_id": self.binding.source_snapshot_id,
            "context_digest": predecessor["active_manifest_digest"],
            "spool_digest": digest(tuple(item.entry_digest for item in entries)),
            "idempotency_key": _key(entry.delivery_id, "checkpoint"),
            "grants_authority": False,
            "approval_inherited": False,
        }
        checkpoint_digest = digest(checkpoint)
        draft: dict[str, object] = {
            "checkpoint_digest": checkpoint_digest,
            "pre_digest": pre_digest,
            "revision_key": _key(entry.delivery_id, "pre-compact-revision"),
            "observed_at": observed,
        }
        revision = _revision_body(predecessor, draft)
        revision_value = revision_digest(revision)
        ack = {
            "schema": "zekam-precompaction-ack-decision/v1",
            "session_id": self.binding.session_id,
            "external_session_id": self.binding.external_session_id,
            "client_id": self.binding.client_id,
            "device_id": self.binding.device_id,
            "binding_digest": self.binding.binding_digest,
            "attachment_id": predecessor["attachment_id"],
            "process_generation_digest": predecessor["process_generation_digest"],
            "hydrated_predecessor_revision_digest": predecessor["revision_digest"],
            "delivery_id": entry.delivery_id,
            "spool_entry_digest": entry.entry_digest,
            "full_spool_tuple_digest": checkpoint["spool_digest"],
            "ancestry_receipt_digest": ancestry_digest,
            "native_receipt_digest": native_digest,
            "internal_receipt_digest": receipt_digest,
            "checkpoint_requested_event_digest": first_digest,
            "pre_compaction_event_digest": pre_digest,
            "checkpoint_digest": checkpoint_digest,
            "pre_compact_committed_revision_digest": revision_value,
            "source_snapshot_id": source.source_snapshot_id,
            "source_revision": source.revision_ref,
            "source_snapshot_digest": source.snapshot_digest,
            "active_manifest_digest": predecessor["active_manifest_digest"],
            "active_hydration_receipt_digest": predecessor["active_hydration_receipt_digest"],
            "success_stdout_digest": _SUCCESS_DIGEST,
            "durable_reopen_verified": True,
            "native_ack_observed": False,
            "grants_authority": False,
            "approval_inherited": False,
        }
        values: dict[str, object] = {
            "binding": self.binding,
            "observed_at": observed,
            "delivery_id": entry.delivery_id,
            "spool_entry_digest": entry.entry_digest,
            "spool_entry_digests": tuple(item.entry_digest for item in entries),
            "source_snapshot": source,
            "predecessor_revision_digest": predecessor["revision_digest"],
            "process_generation_digest": predecessor["process_generation_digest"],
            "active_manifest_digest": predecessor["active_manifest_digest"],
            "active_hydration_receipt_digest": predecessor["active_hydration_receipt_digest"],
            "old_sequence": old["sequence"],
            "old_event_digest": old["event_digest"],
            "ancestry_body_json": canonical_json(ancestry),
            "ancestry_receipt_digest": ancestry_digest,
            "native_body_json": canonical_json(native),
            "native_receipt_digest": native_digest,
            "checkpoint_event_id": _uuid(
                self.binding, entry.delivery_id, "checkpoint-requested-event"
            ),
            "checkpoint_event_body_json": canonical_json(first),
            "checkpoint_event_digest": first_digest,
            "checkpoint_receipt_body_json": canonical_json(receipt),
            "checkpoint_receipt_digest": receipt_digest,
            "precompact_event_id": _uuid(self.binding, entry.delivery_id, "pre-compaction-event"),
            "precompact_event_body_json": canonical_json(pre),
            "precompact_event_digest": pre_digest,
            "checkpoint_body_json": canonical_json(checkpoint),
            "checkpoint_digest": checkpoint_digest,
            "revision_body_json": canonical_json(revision),
            "revision_digest": revision_value,
            "ack_body_json": canonical_json(ack),
            "ack_decision_digest": digest(ack),
            "rows": (
                ("ancestry", (ancestry_digest, entry.delivery_id)),
                ("native", (native_digest, pre_digest, entry.delivery_id)),
                ("internal", (receipt_digest, first_digest, checkpoint_key)),
                ("checkpoint", (checkpoint_digest, checkpoint["idempotency_key"])),
                (
                    "revision",
                    (revision_value, revision["revision_number"], revision["operation_key"]),
                ),
            ),
        }
        return _issue_plan(self.generation, values)

    @staticmethod
    def _collisions(db: Db, plan: PreparedPreCompactionPlan) -> tuple[int, ...]:
        checks = (
            (
                "continuity_hook_invocation_ancestry_receipt",
                "receipt_digest=? or delivery_id=?",
                (plan.ancestry_receipt_digest, plan.delivery_id),
            ),
            (
                "continuity_native_event_receipt",
                "receipt_digest=? or event_digest=? or delivery_id=? or "
                "ancestry_receipt_digest=? or "
                "(process_generation_digest=? and spool_sequence=?)",
                (
                    plan.native_receipt_digest,
                    plan.precompact_event_digest,
                    plan.delivery_id,
                    plan.ancestry_receipt_digest,
                    plan.process_generation_digest,
                    json.loads(plan.native_body_json)["spool_sequence"],
                ),
            ),
            (
                "continuity_internal_event_receipt",
                "receipt_digest=? or event_digest=? or "
                "(session_id=? and event_kind='CHECKPOINT_REQUESTED' "
                "and operation_key=?)",
                (
                    plan.checkpoint_receipt_digest,
                    plan.checkpoint_event_digest,
                    plan.binding.session_id,
                    _key(plan.delivery_id, "checkpoint-requested"),
                ),
            ),
            (
                "session_event",
                "id in (?,?) or event_digest in (?,?)",
                (
                    plan.checkpoint_event_id,
                    plan.precompact_event_id,
                    plan.checkpoint_event_digest,
                    plan.precompact_event_digest,
                ),
            ),
            (
                "session_event_detail",
                "event_id in (?,?) or event_digest in (?,?) or "
                "(session_id=? and sequence in (?,?)) or "
                "(session_id=? and idempotency_key in (?,?))",
                (
                    plan.checkpoint_event_id,
                    plan.precompact_event_id,
                    plan.checkpoint_event_digest,
                    plan.precompact_event_digest,
                    plan.binding.session_id,
                    plan.old_sequence + 1,
                    plan.old_sequence + 2,
                    plan.binding.session_id,
                    _key(plan.delivery_id, "checkpoint-requested"),
                    _key(plan.delivery_id, "pre-compaction"),
                ),
            ),
            (
                "continuity_checkpoint",
                "checkpoint_digest=? or (session_id=? and idempotency_key=?)",
                (
                    plan.checkpoint_digest,
                    plan.binding.session_id,
                    _key(plan.delivery_id, "checkpoint"),
                ),
            ),
            (
                "continuity_hook_attachment_revision",
                "revision_digest=? or "
                "(attachment_id=? and revision_number=?) or "
                "(attachment_id=? and operation_key=?)",
                (
                    plan.revision_digest,
                    json.loads(plan.revision_body_json)["attachment_id"],
                    json.loads(plan.revision_body_json)["revision_number"],
                    json.loads(plan.revision_body_json)["attachment_id"],
                    _key(plan.delivery_id, "pre-compact-revision"),
                ),
            ),
        )
        return tuple(
            int(db.execute(f"select count(*) from {table} where {where}", args).fetchone()[0])
            for table, where, args in checks
        )

    @staticmethod
    def _exact_plan_rows(db: Db, plan: PreparedPreCompactionPlan) -> None:
        expected = (
            (
                "continuity_hook_invocation_ancestry_receipt",
                "receipt_digest",
                plan.ancestry_receipt_digest,
                plan.ancestry_body_json,
            ),
            (
                "continuity_native_event_receipt",
                "receipt_digest",
                plan.native_receipt_digest,
                plan.native_body_json,
            ),
            (
                "continuity_internal_event_receipt",
                "receipt_digest",
                plan.checkpoint_receipt_digest,
                plan.checkpoint_receipt_body_json,
            ),
            (
                "continuity_checkpoint",
                "checkpoint_digest",
                plan.checkpoint_digest,
                plan.checkpoint_body_json,
            ),
        )
        for table, key, value, body in expected:
            row = db.execute(f"select body_json from {table} where {key}=?", (value,)).fetchone()
            if row is None or row[0] != body:
                raise PolicyViolation("PreCompact exact planned row drift")
        for event_digest, body in (
            (plan.checkpoint_event_digest, plan.checkpoint_event_body_json),
            (plan.precompact_event_digest, plan.precompact_event_body_json),
        ):
            row = db.execute(
                "select body_json from session_event_detail where event_digest=?", (event_digest,)
            ).fetchone()
            if row is None or row[0] != body:
                raise PolicyViolation("PreCompact exact event detail drift")
        revision_body = {
            "revision_digest": plan.revision_digest,
            **json.loads(plan.revision_body_json),
        }
        row = db.execute(
            "select body_json from continuity_hook_attachment_revision where revision_digest=?",
            (plan.revision_digest,),
        ).fetchone()
        if row is None or row[0] != canonical_json(revision_body):
            raise PolicyViolation("PreCompact exact revision row drift")

    def _verify_plan(
        self,
        db: Db,
        plan: PreparedPreCompactionPlan,
        entries: tuple[LifecycleSpoolEntry, ...],
        invocation: ManagedInvocationSnapshot,
        deadline: SealedPreCompactionDeadline,
        *,
        issue_decision: bool,
    ) -> VerifiedAckDecision | None:
        self._schema(db)
        session = SQLiteDormantV4CloseWriter._binding(db, self.binding)
        if session["status"] != "open":
            raise PolicyViolation("PreCompact open session required")
        rows = SQLiteDormantV4CloseWriter._events(db, self.binding)
        SQLiteDormantV4CloseWriter._no_pending(db, self.binding)
        if tuple(
            str(row["spool_digest"]) for row in rows if row["spool_digest"] is not None
        ) != tuple(item.entry_digest for item in entries):
            raise PolicyViolation("PreCompact durable spool prefix drift")
        attachment = SQLiteDormantV4CloseWriter._attachment(db, self.binding)
        current = self._current(db, str(attachment["attachment_id"]))
        if (
            current["revision_digest"] != plan.revision_digest
            or current["state"] != "pre-compact-committed"
        ):
            raise PolicyViolation("PreCompact committed revision drift")
        checkpoint = db.execute(
            "select body_json from continuity_checkpoint where checkpoint_digest=?",
            (plan.checkpoint_digest,),
        ).fetchone()
        if checkpoint is None or checkpoint[0] != plan.checkpoint_body_json:
            raise PolicyViolation("PreCompact checkpoint replay drift")
        self._exact_plan_rows(db, plan)
        predecessor = self._current_by_digest(db, plan.predecessor_revision_digest)
        # This shared census delegates each bounded resolve_fragment parity check.
        self._manifest(db, predecessor, plan.source_snapshot, deadline)
        try:
            self.source.assert_current(self.binding, plan.source_snapshot, deadline)
        except (PolicyViolation, ValidationFailed) as exc:
            raise _CompactionGateError(PreCompactionFailure.SOURCE_DRIFT) from exc
        self.process_manager.assert_invocation_bounded(invocation, deadline)
        query_only = bool(db.execute("pragma query_only").fetchone()[0])
        self.durability.verify(db, deadline, read_only=query_only)
        deadline.require_current()
        if not issue_decision:
            return None
        if not bool(db.execute("pragma query_only").fetchone()[0]):
            raise PolicyViolation("PreCompact decision requires reopened read-only verifier")
        return _issue_ack_decision(self.generation, json.loads(plan.ack_body_json))

    def _commit_unknown(
        self,
        plan: PreparedPreCompactionPlan,
        baseline: str,
        expected_post: str,
        entries: tuple[LifecycleSpoolEntry, ...],
        invocation: ManagedInvocationSnapshot,
        deadline: SealedPreCompactionDeadline,
    ) -> PreCompactionResult:
        """Perform exactly one bounded read-only, no-repair classification."""
        try:
            with closing(self._connect(deadline, read_only=True)) as verify:
                verify.execute("begin")
                observed = self._selected_census(verify, self.binding, plan.delivery_id, deadline)
                if observed == baseline:
                    verify.rollback()
                    return recovery_required(PreCompactionFailure.RECOVERY_REQUIRED)
                if observed != expected_post:
                    verify.rollback()
                    return recovery_required(PreCompactionFailure.RECOVERY_REQUIRED)
                try:
                    decision = self._verify_plan(
                        verify, plan, entries, invocation, deadline, issue_decision=True
                    )
                except Exception:
                    verify.rollback()
                    return recovery_required(PreCompactionFailure.RECOVERY_REQUIRED)
                verify.rollback()
            deadline.require_current()
            if decision is None:
                return recovery_required(PreCompactionFailure.RECOVERY_REQUIRED)
            self._last_decision = decision
            return _checkpoint_ready(self.generation, decision, replay=True)
        except Exception:
            return recovery_required(PreCompactionFailure.RECOVERY_REQUIRED)

    def _run(
        self, event: CodexMacOS0151Event, deadline: SealedPreCompactionDeadline
    ) -> PreCompactionResult:
        self.generation._recheck("accept")
        if type(event) is not CodexMacOS0151Event:
            raise ValidationFailed("PreCompact exact parsed event required")
        event.__post_init__()
        if (
            event.event_type != "PreCompact"
            or event.external_session_id != self.binding.external_session_id
        ):
            raise PolicyViolation("PreCompact event/binding mismatch")
        observed = self.process_manager.recovery_time()
        occurred = dt.datetime.fromisoformat(observed)
        delivery = digest(
            {
                "schema": "zekam-codex-0151-delivery/v1",
                "session_id": self.binding.external_session_id,
                "external_event_type": "PreCompact",
                "turn_id": event.turn_id,
                "trigger": event.trigger,
                "wire_digest": event.wire_digest,
            }
        )
        with self.spool.stage_frozen(
            event.observation_body(), delivery_id=delivery, occurred_at=occurred, deadline=deadline
        ) as held:
            entry, _created, entries = held
            source = self.source.snapshot(self.binding, deadline)
            db = self._connect(deadline)
            try:
                owner = _OwnedImmediateTransaction(db)
                baseline = self._selected_census(db, self.binding, delivery, deadline)
            except BaseException:
                db.close()
                raise
            expected_post = baseline
            plan: PreparedPreCompactionPlan | None = None
            try:
                self._schema(db)
                if (
                    self.resolved is not None
                    and _resolve_existing_binding(db, event) != self.resolved
                ):
                    raise PolicyViolation("PreCompact resolved authority changed before mutation")
                session = SQLiteDormantV4CloseWriter._binding(db, self.binding)
                if session["status"] != "open":
                    raise PolicyViolation("PreCompact open session required")
                attachment = SQLiteDormantV4CloseWriter._attachment(db, self.binding)
                current = self._current(db, str(attachment["attachment_id"]))
                rows = SQLiteDormantV4CloseWriter._events(db, self.binding)
                predecessor = current
                base_rows = rows
                replay = current["state"] == "pre-compact-committed"
                if replay:
                    predecessor = self._current_by_digest(
                        db, str(current["previous_revision_digest"])
                    )
                    base_rows = rows[:-2]
                if predecessor["state"] != "hydrated" or not base_rows:
                    raise PolicyViolation("PreCompact hydrated predecessor required")
                if (
                    entries[-1] != entry
                    or entry.sequence < 2
                    or entry.external_event_type != "PreCompact"
                    or entry.internal_event_type != "PRE_COMPACTION"
                    or entry.observation["schema"] != CODEX_MACOS_0151_CONTRACT_SCHEMA
                    or entry.client_version != CODEX_MACOS_0151_VERSION
                ):
                    raise _CompactionGateError(PreCompactionFailure.UNPERSISTED_DELTA)
                persisted = tuple(
                    str(row["spool_digest"]) for row in base_rows if row["spool_digest"] is not None
                )
                if persisted != tuple(item.entry_digest for item in entries[:-1]):
                    raise _CompactionGateError(PreCompactionFailure.UNPERSISTED_DELTA)
                try:
                    SQLiteDormantV4CloseWriter._no_pending(db, self.binding)
                except PolicyViolation as exc:
                    raise _CompactionGateError(PreCompactionFailure.PENDING_WORK) from exc
                commands = {
                    c.external_event_type: c
                    for c in verify_reviewed_hook_commands(db, str(attachment["attachment_id"]))
                }
                generation = db.execute(
                    "select * from continuity_hook_process_generation "
                    "where process_generation_digest=?",
                    (predecessor["process_generation_digest"],),
                ).fetchone()
                if generation is None:
                    raise PolicyViolation("PreCompact process generation missing")
                invocation_args = (
                    self.binding,
                    event.observation_body(),
                    entry.entry_digest,
                    observed,
                    str(generation["process_generation_digest"]),
                    str(generation["created_at"]),
                    str(generation["managed_launch_receipt_digest"]),
                    commands["PreCompact"],
                    str(generation["ancestry_policy_digest"]),
                    deadline,
                )
                if int(generation["generation"]) == 1:
                    invocation = self.process_manager.capture_precompaction_invocation(
                        *invocation_args
                    )
                else:
                    receipt = db.execute(
                        "select transition_kind from continuity_managed_process_receipt "
                        "where receipt_digest=?",
                        (generation["managed_launch_receipt_digest"],),
                    ).fetchone()
                    if receipt is None:
                        raise PolicyViolation("PreCompact managed generation receipt missing")
                    invocation = self.process_manager.capture_precompaction_invocation(
                        *invocation_args,
                        expected_generation_number=int(generation["generation"]),
                        expected_previous_generation_digest=str(
                            generation["previous_process_generation_digest"]
                        ),
                        expected_transition_kind=str(receipt["transition_kind"]),
                    )
                if (
                    invocation.delivery_id != entry.delivery_id
                    or invocation.spool_digest != entry.entry_digest
                ):
                    raise PolicyViolation("PreCompact invocation/spool drift")
                self.process_manager.assert_invocation_bounded(invocation, deadline)
                self._manifest(db, predecessor, source, deadline)
                plan = self._plan(predecessor, base_rows, entry, entries, source, invocation)
                owner.planned(plan.ack_decision_digest)
                collisions = self._collisions(db, plan)
                if replay:
                    if collisions != (1, 1, 1, 2, 2, 1, 1):
                        raise PolicyViolation("PreCompact partial replay graph")
                elif any(collisions):
                    raise ConcurrencyConflict("PreCompact deterministic identity collision")
                if not replay:
                    self.generation._recheck("first-mutation")
                    owner.applying()
                    owner.apply_precompaction_graph(plan)
                    expected_post = self._selected_census(db, self.binding, delivery, deadline)
                else:
                    expected_post = baseline
                self._manifest(db, predecessor, source, deadline)
                self._verify_plan(db, plan, entries, invocation, deadline, issue_decision=False)
                if self._selected_census(db, self.binding, delivery, deadline) != expected_post:
                    raise PolicyViolation("PreCompact C2 selected census drift")
                deadline.require_current(reserve_ms=PRECOMPACT_FINAL_RESERVE_MS)
                self.generation._recheck("precommit")
                owner.verified(plan.ack_decision_digest)
                try:
                    owner.commit_verified(plan.ack_decision_digest, deadline)
                except Exception:
                    db.close()
                    return self._commit_unknown(
                        plan, baseline, expected_post, entries, invocation, deadline
                    )
            except BaseException:
                owner.abort()
                raise
            finally:
                db.close()
            assert plan is not None
            with closing(self._connect(deadline, read_only=True)) as verify:
                self.generation._recheck("read-only-verification")
                verify.execute("begin")
                if self._selected_census(verify, self.binding, delivery, deadline) != expected_post:
                    raise PolicyViolation("PreCompact C3 selected census drift")
                decision = self._verify_plan(
                    verify, plan, entries, invocation, deadline, issue_decision=True
                )
                verify.rollback()
            deadline.require_current()
            if decision is None:
                raise PolicyViolation("PreCompact reopened decision missing")
            self.generation._recheck("response")
            self._last_decision = decision
            return _checkpoint_ready(self.generation, decision, replay=replay)

    @staticmethod
    def _current_by_digest(db: Db, value: str) -> sqlite3.Row:
        row = db.execute(
            "select * from continuity_hook_attachment_revision where revision_digest=?", (value,)
        ).fetchone()
        if row is None:
            raise PolicyViolation("PreCompact predecessor missing")
        return cast(sqlite3.Row, row)

    def pre_compaction(self, event: CodexMacOS0151Event) -> PreCompactionResult:
        import time

        self._last_decision = None
        deadline = _issue_deadline(self.generation, time.monotonic_ns)
        result: PreCompactionResult
        try:
            result = self._run(event, deadline)
        except TimeoutError:
            result = rejected(PreCompactionFailure.DEADLINE)
        except LiveProcessVerificationError:
            result = rejected(PreCompactionFailure.PROCESS_DRIFT)
        except ConcurrencyConflict:
            result = rejected(PreCompactionFailure.UNPERSISTED_DELTA)
        except _CompactionGateError as exc:
            result = recovery_required(exc.category)
        except (ConfigurationError, sqlite3.Error, OSError):
            result = recovery_required(PreCompactionFailure.STORAGE_UNAVAILABLE)
        except PolicyViolation:
            result = recovery_required(PreCompactionFailure.RECOVERY_REQUIRED)
        except ValidationFailed:
            result = rejected(PreCompactionFailure.VALIDATION)
        except Exception:
            result = recovery_required(PreCompactionFailure.RECOVERY_REQUIRED)
        finally:
            close_source = getattr(self.source, "close", None)
            if callable(close_source):
                close_source()
        return result

    def pre_compaction_with_decision(
        self, event: CodexMacOS0151Event
    ) -> tuple[PreCompactionResult, VerifiedAckDecision | None]:
        result = self.pre_compaction(event)
        decision = self._last_decision
        if result.status == "checkpoint-ready":
            if decision is None or decision.decision_digest != result.ack_decision_digest:
                raise PolicyViolation("PreCompact successful writer decision unavailable")
            decision.__post_init__()
        elif decision is not None:
            raise PolicyViolation("PreCompact failure carried decision authority")
        return result, decision


SQLiteDormantV4PreCompactionWriter = _SQLiteDormantV4PreCompactionWriter


def resolved_precompaction_writer(
    path: Path,
    resolved: ResolvedPreCompactionBinding,
    *,
    spool: ClientLifecycleSpool,
    generation: _DarwinGenerationOwner,
) -> _SQLiteDormantV4PreCompactionWriter:
    _generation_digest_if_current(generation)
    if type(resolved) is not ResolvedPreCompactionBinding:
        raise ValidationFailed("PreCompact exact resolved authority required")
    resolved.__post_init__()
    writer = _SQLiteDormantV4PreCompactionWriter(
        path, resolved.binding, spool=spool, generation=generation
    )
    writer.resolved = resolved
    return writer
