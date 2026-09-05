"""Dormant SQLite writer for exact Codex 0.151 SessionStart ingress."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid5

from zekam.application import client_lifecycle_spool as spool_module
from zekam.application.client_lifecycle_spool import ClientLifecycleSpool, LifecycleSpoolEntry
from zekam.application.local_continuity import ContinuityBinding
from zekam.application.local_continuity_v4_ingress import (
    CurrentSessionStartContextPort,
    FrozenCurrentStartupContext,
    ManagedInvocationSnapshot,
    ManagedProcessSnapshot,
    SessionStartIngressResult,
    TrustedProcessManagerPort,
)
from zekam.application.local_continuity_v4_writer import (
    revision_digest,
    verify_persisted_context_manifest,
)
from zekam.application.local_hook_command_contract import ReviewedHookCommand
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
    _trusted_process_owner,
    handled_failure_output,
)
from zekam.infrastructure.sqlite import operational_schema
from zekam.infrastructure.sqlite.continuity_native_verifier import verify_reviewed_hook_commands
from zekam.infrastructure.sqlite.local_continuity_v4_writer import SQLiteDormantV4CloseWriter

_NAMESPACE = UUID("018f0000-0000-7000-8000-000000000151")
_TOPOLOGY = "native-fork-shell-exec-launcher-exec-runtime/v1"


def _event_uuid(binding: ContinuityBinding, spool_digest: str) -> str:
    return str(uuid5(_NAMESPACE, f"event|{binding.session_id}|{spool_digest}"))


def _attachment_uuid(binding: ContinuityBinding) -> str:
    return str(uuid5(_NAMESPACE, f"attachment|{binding.session_id}|{binding.binding_digest}"))


def _operation_key(event_id: str) -> str:
    return f"codex0151-session-start-{event_id}"


def _revision_body(
    *,
    attachment_id: str,
    revision_number: int,
    previous_revision_digest: str | None,
    operation_key: str,
    state: str,
    process_generation_digest: str,
    active_manifest_digest: str | None = None,
    active_hydration_receipt_digest: str | None = None,
    hook_recovery_case_id: str | None = None,
    created_at: str,
) -> dict[str, Any]:
    return {
        "attachment_id": attachment_id,
        "revision_number": revision_number,
        "previous_revision_digest": previous_revision_digest,
        "operation_key": operation_key,
        "state": state,
        "process_generation_digest": process_generation_digest,
        "active_manifest_digest": active_manifest_digest,
        "active_hydration_receipt_digest": active_hydration_receipt_digest,
        "checkpoint_digest": None,
        "pre_compaction_event_digest": None,
        "post_compaction_event_digest": None,
        "close_request_digest": None,
        "pre_close_event_digest": None,
        "close_receipt_digest": None,
        "session_closed_event_digest": None,
        "hook_recovery_case_id": hook_recovery_case_id,
        "hook_recovery_resolution_id": None,
        "local_recovery_case_id": None,
        "local_recovery_resolution_id": None,
        "crash_recovered_event_digest": None,
        "crash_recovered_receipt_digest": None,
        "created_at": created_at,
    }


def _insert_revision(db: sqlite3.Connection, body: dict[str, Any]) -> str:
    value = revision_digest(body)
    stored = {"revision_digest": value, **body}
    columns = tuple(stored)
    db.execute(
        "insert into continuity_hook_attachment_revision("
        + ",".join(columns)
        + ",body_json) values("
        + ",".join("?" for _ in range(len(columns) + 1))
        + ")",
        (*stored.values(), canonical_json(stored)),
    )
    return value


class SQLiteCodexV4Ingress:
    """Explicit v4-only ingress; never composed by a default-v3 entrypoint."""

    def __init__(
        self,
        path: Path,
        binding: ContinuityBinding,
        *,
        process_manager: TrustedProcessManagerPort,
        context_port: CurrentSessionStartContextPort,
        spool: ClientLifecycleSpool,
    ) -> None:
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValidationFailed("V4 ingress exact absolute database path required")
        if type(binding) is not ContinuityBinding:
            raise ValidationFailed("V4 ingress exact binding required")
        binding.__post_init__()
        from zekam.infrastructure.local_continuity_v4_composition import (
            _trusted_context_owner,
        )

        if not _trusted_process_owner(process_manager):
            raise ValidationFailed("V4 ingress sealed concrete process manager required")
        if not _trusted_context_owner(context_port):
            raise ValidationFailed("V4 ingress sealed concrete context owner required")
        if type(spool) is not ClientLifecycleSpool:
            raise ValidationFailed("V4 ingress exact lifecycle spool required")
        self.path = path
        self.binding = binding
        self.process_manager = process_manager
        self.context_port = context_port
        self.spool = spool

    def _schema(self) -> None:
        current = operational_schema.status(self.path)
        if not current.exists or not current.integrity_ok or not current.schema_ok:
            raise ConfigurationError("V4 ingress operational integrity gate failed")
        if current.schema_version != 4:
            raise PolicyViolation("V4 ingress requires explicit dormant schema v4")

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        self._schema()
        uri = self.path.resolve().as_uri() + ("?mode=ro" if read_only else "?mode=rw")
        db = sqlite3.connect(uri, uri=True, timeout=5.0)
        db.row_factory = sqlite3.Row
        db.execute("pragma foreign_keys=on")
        db.execute("pragma busy_timeout=5000")
        if read_only:
            db.execute("pragma query_only=on")
        return db

    def _assert_binding(self, db: sqlite3.Connection) -> sqlite3.Row:
        b = self.binding
        row = db.execute(
            "select b.*,s.status from continuity_session_binding b "
            "join session s on s.id=b.session_id where b.session_id=?",
            (b.session_id,),
        ).fetchone()
        if row is None:
            raise PolicyViolation("V4 ingress existing binding required")
        expected = {
            "session_id": b.session_id,
            "external_session_id": b.external_session_id,
            "project_id": b.project_id,
            "realm_id": b.realm_id,
            "work_item_id": b.work_item_id,
            "run_id": b.run_id,
            "client_id": b.client_id,
            "device_id": b.device_id,
            "source_snapshot_id": b.source_snapshot_id,
            "task_digest": b.task_digest,
            "plan_digest": b.plan_digest,
            "policy_digest": b.policy_digest,
            "binding_digest": b.binding_digest,
        }
        if any(row[key] != value for key, value in expected.items()) or row["status"] != "open":
            raise PolicyViolation("V4 ingress binding/session authority drift")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _current_revision(db: sqlite3.Connection, attachment_id: str) -> sqlite3.Row:
        return SQLiteDormantV4CloseWriter._current_revision(db, attachment_id)

    @staticmethod
    def _verify_invocation_generation(
        db: sqlite3.Connection, invocation: ManagedInvocationSnapshot
    ) -> None:
        generation = db.execute(
            "select * from continuity_hook_process_generation where process_generation_digest=?",
            (invocation.process_generation_digest,),
        ).fetchone()
        if generation is None or (
            generation["native_pid"],
            generation["native_uid"],
            generation["native_start_token"],
            generation["native_artifact_digest"],
            generation["ancestry_policy_digest"],
        ) != (
            invocation.native_pid,
            invocation.native_uid,
            invocation.native_start_token,
            invocation.native_artifact_digest,
            invocation.ancestry_policy_digest,
        ):
            raise PolicyViolation("V4 ingress invocation/live generation tuple drift")

    def attach_process(self) -> str:
        self._schema()
        process = self.process_manager.capture_process(self.binding)
        if type(process) is not ManagedProcessSnapshot:
            raise ValidationFailed("V4 ingress exact manager process snapshot required")
        process.__post_init__()
        if process.attachment_id != _attachment_uuid(self.binding):
            raise PolicyViolation("V4 ingress deterministic attachment identity drift")
        self.process_manager.assert_process(process)
        db = self._connect()
        try:
            db.execute("begin immediate")
            self._assert_binding(db)
            existing = db.execute(
                "select attachment_digest from continuity_hook_attachment where session_id=?",
                (self.binding.session_id,),
            ).fetchone()
            if existing is not None:
                verified = self._verify_attachment(db, process)
                preview = self.context_port.build(
                    self.binding,
                    hydration_key=f"codex0151-attach-preflight-{process.attachment_id}",
                    observed_at=process.captured_at,
                )
                self.context_port.assert_current(self.binding, preview)
                db.rollback()
                return verified
            SQLiteDormantV4CloseWriter._no_pending(db, self.binding)
            attachment_body = {
                "attachment_id": process.attachment_id,
                "client_contract_digest": process.client_contract_digest,
                "created_at": process.captured_at,
                "hook_set_digest": process.hook_set_digest,
                "native_artifact_digest": process.native_artifact_digest,
                "session_id": self.binding.session_id,
            }
            attachment_digest = digest(attachment_body)
            db.execute(
                "insert into continuity_hook_attachment values(?,?,?,?,?,?,?,?)",
                (
                    process.attachment_id,
                    self.binding.session_id,
                    process.client_contract_digest,
                    process.native_artifact_digest,
                    process.hook_set_digest,
                    attachment_digest,
                    canonical_json(attachment_body),
                    process.captured_at,
                ),
            )
            managed_body = {
                "ancestry_policy_digest": process.ancestry_policy_digest,
                "attachment_id": process.attachment_id,
                "created_at": process.captured_at,
                "hook_set_digest": process.hook_set_digest,
                "native_artifact_digest": process.native_artifact_digest,
                "native_pid": process.native_pid,
                "native_start_token": process.native_start_token,
                "native_uid": process.native_uid,
                "predecessor_process_generation_digest": None,
                "transition_kind": "initial-attach",
            }
            managed_digest = digest(managed_body)
            db.execute(
                "insert into continuity_managed_process_receipt values(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    managed_digest,
                    process.attachment_id,
                    None,
                    process.native_pid,
                    process.native_uid,
                    process.native_start_token,
                    process.native_artifact_digest,
                    process.hook_set_digest,
                    process.ancestry_policy_digest,
                    "initial-attach",
                    canonical_json(managed_body),
                    process.captured_at,
                ),
            )
            generation_body = {
                "ancestry_policy_digest": process.ancestry_policy_digest,
                "attachment_id": process.attachment_id,
                "created_at": process.captured_at,
                "generation": 1,
                "hook_set_digest": process.hook_set_digest,
                "managed_launch_receipt_digest": managed_digest,
                "native_artifact_digest": process.native_artifact_digest,
                "native_pid": process.native_pid,
                "native_start_token": process.native_start_token,
                "native_uid": process.native_uid,
                "previous_process_generation_digest": None,
            }
            generation_digest = digest(generation_body)
            db.execute(
                "insert into continuity_hook_process_generation values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    generation_digest,
                    process.attachment_id,
                    1,
                    process.native_pid,
                    process.native_uid,
                    process.native_start_token,
                    process.native_artifact_digest,
                    process.hook_set_digest,
                    process.ancestry_policy_digest,
                    None,
                    managed_digest,
                    canonical_json(generation_body),
                    process.captured_at,
                ),
            )
            attached = _insert_revision(
                db,
                _revision_body(
                    attachment_id=process.attachment_id,
                    revision_number=1,
                    previous_revision_digest=None,
                    operation_key=f"codex0151-attach-{process.attachment_id}",
                    state="attached",
                    process_generation_digest=generation_digest,
                    created_at=process.captured_at,
                ),
            )
            for command in process.reviewed_commands:
                self._insert_command(db, command)
            if (
                verify_reviewed_hook_commands(db, process.attachment_id)
                != process.reviewed_commands
            ):
                raise PolicyViolation("V4 ingress reviewed command readback drift")
            self.process_manager.assert_process(process)
            self._assert_binding(db)
            if self._current_revision(db, process.attachment_id)["revision_digest"] != attached:
                raise PolicyViolation("V4 ingress attached revision readback drift")
            preview = self.context_port.build(
                self.binding,
                hydration_key=f"codex0151-attach-preflight-{process.attachment_id}",
                observed_at=process.captured_at,
            )
            self.context_port.assert_current(self.binding, preview)
            self.process_manager.assert_process(process)
            db.commit()
        except sqlite3.IntegrityError as exc:
            db.rollback()
            raise ConcurrencyConflict("V4 ingress concurrent attach conflict") from exc
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        with closing(self._connect(read_only=True)) as verify:
            verify.execute("begin")
            return self._verify_attachment(verify, process)

    @staticmethod
    def _insert_command(db: sqlite3.Connection, command: ReviewedHookCommand) -> None:
        db.execute(
            "insert into continuity_reviewed_hook_command values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                command.command_digest,
                command.attachment_id,
                command.external_event_type,
                command.topology,
                command.client_contract_digest,
                command.hook_set_digest,
                command.shell_artifact_digest,
                command.python_launcher_artifact_digest,
                command.python_runtime_artifact_digest,
                command.argv_recipe_digest,
                command.sandbox_profile_digest,
                canonical_json(command.body()),
                command.created_at,
                0,
                0,
            ),
        )

    def _verify_attachment(self, db: sqlite3.Connection, process: ManagedProcessSnapshot) -> str:
        self._assert_binding(db)
        attachment = db.execute(
            "select * from continuity_hook_attachment where session_id=?",
            (self.binding.session_id,),
        ).fetchall()
        if len(attachment) != 1:
            raise PolicyViolation("V4 ingress exact attachment row required")
        row = attachment[0]
        body = json.loads(row["body_json"])
        expected_attachment = {
            "attachment_id": row["attachment_id"],
            "client_contract_digest": row["client_contract_digest"],
            "created_at": row["created_at"],
            "hook_set_digest": row["hook_set_digest"],
            "native_artifact_digest": row["native_artifact_digest"],
            "session_id": row["session_id"],
        }
        if (
            body != expected_attachment
            or canonical_json(body) != row["body_json"]
            or digest(body) != row["attachment_digest"]
            or row["attachment_id"] != process.attachment_id
            or row["session_id"] != self.binding.session_id
            or row["client_contract_digest"] != process.client_contract_digest
            or row["hook_set_digest"] != process.hook_set_digest
            or row["native_artifact_digest"] != process.native_artifact_digest
        ):
            raise PolicyViolation("V4 ingress attachment body/digest drift")
        managed_rows = db.execute(
            "select * from continuity_managed_process_receipt where attachment_id=?",
            (process.attachment_id,),
        ).fetchall()
        generation_rows = db.execute(
            "select * from continuity_hook_process_generation where attachment_id=?",
            (process.attachment_id,),
        ).fetchall()
        attached_rows = db.execute(
            "select * from continuity_hook_attachment_revision "
            "where attachment_id=? and revision_number=1",
            (process.attachment_id,),
        ).fetchall()
        if len(managed_rows) != 1 or len(generation_rows) != 1 or len(attached_rows) != 1:
            raise PolicyViolation("V4 ingress exact durable attach graph required")
        managed = managed_rows[0]
        managed_body = {
            "ancestry_policy_digest": managed["ancestry_policy_digest"],
            "attachment_id": managed["attachment_id"],
            "created_at": managed["created_at"],
            "hook_set_digest": managed["hook_set_digest"],
            "native_artifact_digest": managed["native_artifact_digest"],
            "native_pid": managed["native_pid"],
            "native_start_token": managed["native_start_token"],
            "native_uid": managed["native_uid"],
            "predecessor_process_generation_digest": managed[
                "predecessor_process_generation_digest"
            ],
            "transition_kind": managed["transition_kind"],
        }
        generation = generation_rows[0]
        generation_body = {
            "ancestry_policy_digest": generation["ancestry_policy_digest"],
            "attachment_id": generation["attachment_id"],
            "created_at": generation["created_at"],
            "generation": generation["generation"],
            "hook_set_digest": generation["hook_set_digest"],
            "managed_launch_receipt_digest": generation["managed_launch_receipt_digest"],
            "native_artifact_digest": generation["native_artifact_digest"],
            "native_pid": generation["native_pid"],
            "native_start_token": generation["native_start_token"],
            "native_uid": generation["native_uid"],
            "previous_process_generation_digest": generation["previous_process_generation_digest"],
        }
        attached = attached_rows[0]
        attached_without_digest = _revision_body(
            attachment_id=process.attachment_id,
            revision_number=1,
            previous_revision_digest=None,
            operation_key=f"codex0151-attach-{process.attachment_id}",
            state="attached",
            process_generation_digest=str(generation["process_generation_digest"]),
            created_at=str(attached["created_at"]),
        )
        attached_body = {
            "revision_digest": revision_digest(attached_without_digest),
            **attached_without_digest,
        }
        try:
            stored_managed_body = json.loads(managed["body_json"])
            stored_generation_body = json.loads(generation["body_json"])
            stored_attached_body = json.loads(attached["body_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PolicyViolation("V4 ingress durable attach JSON malformed") from exc
        live_tuple = (
            process.native_pid,
            process.native_uid,
            process.native_start_token,
            process.native_artifact_digest,
            process.hook_set_digest,
            process.ancestry_policy_digest,
        )
        if (
            stored_managed_body != managed_body
            or canonical_json(managed_body) != managed["body_json"]
            or digest(managed_body) != managed["receipt_digest"]
            or stored_generation_body != generation_body
            or canonical_json(generation_body) != generation["body_json"]
            or digest(generation_body) != generation["process_generation_digest"]
            or generation["managed_launch_receipt_digest"] != managed["receipt_digest"]
            or stored_attached_body != attached_body
            or canonical_json(attached_body) != attached["body_json"]
            or attached["revision_digest"] != attached_body["revision_digest"]
            or attached["process_generation_digest"] != generation["process_generation_digest"]
            or live_tuple
            != (
                managed["native_pid"],
                managed["native_uid"],
                managed["native_start_token"],
                managed["native_artifact_digest"],
                managed["hook_set_digest"],
                managed["ancestry_policy_digest"],
            )
            or live_tuple
            != (
                generation["native_pid"],
                generation["native_uid"],
                generation["native_start_token"],
                generation["native_artifact_digest"],
                generation["hook_set_digest"],
                generation["ancestry_policy_digest"],
            )
        ):
            raise PolicyViolation("V4 ingress durable attach graph parity drift")
        commands = verify_reviewed_hook_commands(db, process.attachment_id)

        def stable(command: ReviewedHookCommand) -> tuple[str, ...]:
            return (
                command.external_event_type,
                command.topology,
                command.client_contract_digest,
                command.hook_set_digest,
                command.shell_artifact_digest,
                command.python_launcher_artifact_digest,
                command.python_runtime_artifact_digest,
                command.argv_recipe_digest,
                command.sandbox_profile_digest,
            )

        if tuple(stable(command) for command in commands) != tuple(
            stable(command) for command in process.reviewed_commands
        ):
            raise PolicyViolation("V4 ingress reviewed command replay drift")
        self._current_revision(db, process.attachment_id)
        return str(row["attachment_digest"])

    def session_start(self, event: CodexMacOS0151Event) -> SessionStartIngressResult:
        if type(event) is not CodexMacOS0151Event:
            raise ValidationFailed("V4 ingress exact parsed Codex event required")
        event.__post_init__()
        if event.event_type != "SessionStart" or event.source != "startup":
            raise PolicyViolation("V4 ingress SessionStart startup only")
        if event.external_session_id != self.binding.external_session_id:
            raise PolicyViolation("V4 ingress external session binding mismatch")
        observed_at = self.process_manager.recovery_time()
        try:
            occurred_at = dt.datetime.fromisoformat(observed_at)
        except ValueError as exc:
            raise ValidationFailed("V4 ingress manager recovery time invalid") from exc
        with self._stage_current_invocation(event, occurred_at=occurred_at) as (
            entry,
            created,
        ):
            self._verify_spool(event, entry)
            with closing(self._connect(read_only=True)) as scope_db:
                scope_db.execute("begin")
                attachment = scope_db.execute(
                    "select attachment_id from continuity_hook_attachment where session_id=?",
                    (self.binding.session_id,),
                ).fetchone()
                if attachment is None:
                    raise PolicyViolation("V4 ingress process attachment required")
                current = self._current_revision(scope_db, str(attachment[0]))
                if current["state"] == "attached" and not created:
                    raise PolicyViolation(
                        "V4 ingress pre-existing staged event is ambiguous-unacknowledged"
                    )
                commands = verify_reviewed_hook_commands(scope_db, str(attachment[0]))
                generation = scope_db.execute(
                    "select ancestry_policy_digest,created_at,managed_launch_receipt_digest "
                    "from continuity_hook_process_generation "
                    "where process_generation_digest=? and attachment_id=?",
                    (current["process_generation_digest"], attachment[0]),
                ).fetchone()
                if generation is None:
                    raise PolicyViolation("V4 ingress process generation missing")
                live_process = self.process_manager.capture_process(self.binding)
                if type(live_process) is not ManagedProcessSnapshot:
                    raise ValidationFailed("V4 ingress exact live process snapshot required")
                live_process.__post_init__()
                self._verify_attachment(scope_db, live_process)
                self.process_manager.assert_process(live_process)
            invocation = self.process_manager.capture_invocation(
                self.binding,
                event.observation_body(),
                entry.entry_digest,
                entry.occurred_at.isoformat(timespec="seconds"),
                str(current["process_generation_digest"]),
                str(generation["created_at"]),
                str(generation["managed_launch_receipt_digest"]),
                commands[0],
                str(generation["ancestry_policy_digest"]),
            )
            if type(invocation) is not ManagedInvocationSnapshot:
                raise ValidationFailed("V4 ingress exact invocation snapshot required")
            invocation.__post_init__()
            if (
                invocation.delivery_id != entry.delivery_id
                or invocation.observation_digest != entry.observation_digest
                or invocation.spool_digest != entry.entry_digest
            ):
                raise PolicyViolation("V4 ingress invocation/spool identity drift")
            try:
                self.process_manager.assert_invocation(invocation)
                return self._write_session_start(event, entry, invocation)
            except LiveProcessVerificationError:
                return self._recover_process_drift(event, entry, invocation)

    @contextmanager
    def _stage_current_invocation(
        self, event: CodexMacOS0151Event, *, occurred_at: dt.datetime
    ) -> Iterator[tuple[LifecycleSpoolEntry, bool]]:
        """Create and classify one spool entry while retaining the producer lock.

        A byte-identical entry which existed before this lock acquisition is not
        evidence that this invocation created it.  The lock is retained through
        the complete database decision, closing the former precheck/stage race.
        """

        safe = spool_module._validate_observation(event.observation_body())
        spool_module._timestamp(occurred_at, label="occurred_at")
        delivery_id = digest(
            {
                "schema": "zekam-codex-0151-delivery/v1",
                "session_id": event.external_session_id,
                "external_event_type": "SessionStart",
                "wire_digest": event.wire_digest,
            }
        )
        self.spool._ensure_write_directories()
        with spool_module._exclusive_lock(self.spool.lock_path):
            queue_sequence, queue_previous = self.spool._load_queue_tail(recover=True)
            previous = self.spool._load_session_tail(
                client_id=str(safe["client_id"]), session_id=str(safe["session_id"])
            )
            existing = self.spool._entry_for_delivery(delivery_id)
            if existing is not None:
                if existing.observation_digest != digest(safe) or existing.observation != safe:
                    raise PolicyViolation("Lifecycle delivery replay payload drift")
                yield existing, False
                return
            if previous is not None:
                raise PolicyViolation("V4 ingress exact single SessionStart spool required")
            draft = LifecycleSpoolEntry(
                entry_digest="",
                delivery_id=delivery_id,
                client_id=safe["client_id"],
                client_kind=safe["client_kind"],
                client_version=safe["client_version"],
                session_id=safe["session_id"],
                sequence=1 if previous is None else previous.sequence + 1,
                previous_entry_digest=None if previous is None else previous.entry_digest,
                external_event_type=safe["external_event_type"],
                internal_event_type=safe["internal_event_type"],
                observation_digest=digest(safe),
                observation=safe,
                occurred_at=occurred_at,
            )
            entry = replace(draft, entry_digest=digest(draft.body()))
            entry.assert_integrity()
            next_queue_sequence = queue_sequence + 1
            spool_module._write_atomic_json(
                self.spool.queue_state_path,
                self.spool._queue_state_document(
                    entry,
                    queue_sequence=next_queue_sequence,
                    previous_queue_entry_digest=queue_previous,
                    state="pending",
                ),
            )
            spool_module._write_atomic_json(
                self.spool._session_path(entry.client_id, entry.session_id),
                self.spool._checkpoint_document(entry, state="pending"),
            )
            spool_module._write_immutable_json(
                self.spool._entry_path(entry.entry_digest), entry.as_dict()
            )
            spool_module._write_immutable_json(
                self.spool._delivery_path(entry.delivery_id),
                self.spool._delivery_document(entry, queue_sequence=next_queue_sequence),
            )
            spool_module._write_immutable_json(
                self.spool._queue_path(next_queue_sequence),
                self.spool._queue_ref_document(
                    entry,
                    queue_sequence=next_queue_sequence,
                    previous_queue_entry_digest=queue_previous,
                ),
            )
            spool_module._write_atomic_json(
                self.spool._session_path(entry.client_id, entry.session_id),
                self.spool._checkpoint_document(entry, state="committed"),
            )
            spool_module._write_atomic_json(
                self.spool.queue_state_path,
                self.spool._queue_state_document(
                    entry,
                    queue_sequence=next_queue_sequence,
                    previous_queue_entry_digest=queue_previous,
                    state="committed",
                ),
            )
            yield entry, True

    @staticmethod
    def _verify_spool(event: CodexMacOS0151Event, entry: LifecycleSpoolEntry) -> None:
        entry.assert_integrity()
        if (
            entry.sequence != 1
            or entry.previous_entry_digest is not None
            or entry.client_id != "codex"
            or entry.client_kind != "codex"
            or entry.client_version != CODEX_MACOS_0151_VERSION
            or entry.session_id != event.external_session_id
            or entry.external_event_type != "SessionStart"
            or entry.internal_event_type != "SESSION_START"
            or entry.observation != event.observation_body()
            or entry.observation["schema"] != CODEX_MACOS_0151_CONTRACT_SCHEMA
        ):
            raise PolicyViolation("V4 ingress staged spool/event parity drift")

    def _write_session_start(
        self,
        event: CodexMacOS0151Event,
        entry: LifecycleSpoolEntry,
        invocation: ManagedInvocationSnapshot,
    ) -> SessionStartIngressResult:
        event_id = _event_uuid(self.binding, entry.entry_digest)
        key = _operation_key(event_id)
        db = self._connect()
        try:
            db.execute("begin immediate")
            self._assert_binding(db)
            attachment = db.execute(
                "select attachment_id from continuity_hook_attachment where session_id=?",
                (self.binding.session_id,),
            ).fetchone()
            if attachment is None:
                raise PolicyViolation("V4 ingress process attachment required")
            attachment_id = str(attachment[0])
            current = self._current_revision(db, attachment_id)
            if current["state"] == "hydrated" and current["operation_key"] == key:
                db.rollback()
                return self._replay(event, entry, invocation, event_id, key)
            if current["state"] == "recovery-required":
                self._verify_recovery_head(
                    db,
                    event=event,
                    entry=entry,
                    invocation=invocation,
                    current=current,
                )
                db.rollback()
                return SessionStartIngressResult(
                    handled_failure_output(recovery_required=True),
                    None,
                    None,
                    None,
                    str(current["revision_digest"]),
                    True,
                    True,
                )
            if current["state"] != "attached":
                raise PolicyViolation("V4 ingress current attached revision required")
            if current["process_generation_digest"] != invocation.process_generation_digest:
                raise PolicyViolation("V4 ingress invocation generation drift")
            self._verify_invocation_generation(db, invocation)
            SQLiteDormantV4CloseWriter._no_pending(db, self.binding)
            commands = verify_reviewed_hook_commands(db, attachment_id)
            if commands[0].command_digest != invocation.launch_command_digest:
                raise PolicyViolation("V4 ingress SessionStart command drift")
            self.process_manager.assert_invocation(invocation)
            frozen = self.context_port.build(
                self.binding, hydration_key=key, observed_at=invocation.observed_at
            )
            if type(frozen) is not FrozenCurrentStartupContext:
                raise ValidationFailed("V4 ingress exact frozen current context required")
            frozen.__post_init__()
            self.context_port.assert_current(self.binding, frozen)
            if invocation.observed_at != frozen.observed_at:
                raise PolicyViolation("V4 ingress context/invocation timestamp drift")
            self._insert_context(db, frozen)
            ancestry_digest = self._insert_ancestry(db, invocation)
            event_body = {
                "kind": "SESSION_START",
                "idempotency_key": key,
                "occurred_at": invocation.observed_at,
                "source_refs": sorted(
                    {item.source_ref for item in frozen.context.manifest.selected}
                ),
                "evidence_digests": [
                    frozen.manifest_digest,
                    frozen.hydration_receipt_digest,
                    ancestry_digest,
                    frozen.output_digest,
                ],
                "spool_digest": entry.entry_digest,
            }
            envelope = {
                "session_id": self.binding.session_id,
                "binding_digest": self.binding.binding_digest,
                "sequence": 1,
                "previous_digest": None,
                "event": event_body,
            }
            event_digest = digest(envelope)
            native_digest = self._insert_native(
                db,
                current_revision=str(current["revision_digest"]),
                invocation=invocation,
                ancestry_digest=ancestry_digest,
                hydration_digest=frozen.hydration_receipt_digest,
                event_digest=event_digest,
            )
            db.execute(
                "insert into session_event values(?,?,?,?,?)",
                (
                    event_id,
                    self.binding.session_id,
                    "SESSION_START",
                    event_digest,
                    invocation.observed_at,
                ),
            )
            db.execute(
                "insert into session_event_detail values(?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    self.binding.session_id,
                    1,
                    None,
                    key,
                    event_digest,
                    entry.entry_digest,
                    canonical_json(envelope),
                ),
            )
            hydrated = _insert_revision(
                db,
                _revision_body(
                    attachment_id=attachment_id,
                    revision_number=int(current["revision_number"]) + 1,
                    previous_revision_digest=str(current["revision_digest"]),
                    operation_key=key,
                    state="hydrated",
                    process_generation_digest=invocation.process_generation_digest,
                    active_manifest_digest=frozen.manifest_digest,
                    active_hydration_receipt_digest=frozen.hydration_receipt_digest,
                    created_at=invocation.observed_at,
                ),
            )
            self.context_port.assert_current(self.binding, frozen)
            self.process_manager.assert_invocation(invocation)
            self._verify_spool(event, entry)
            if digest(frozen.additional_context) != event_body["evidence_digests"][3]:
                raise PolicyViolation("V4 ingress precommit output digest drift")
            try:
                self._commit(db)
            except Exception:
                if db.in_transaction:
                    db.rollback()
                db.close()
                return self._recover_commit_unknown(
                    event=event,
                    entry=entry,
                    invocation=invocation,
                    event_id=event_id,
                    operation_key=key,
                    expected_manifest=frozen.manifest_digest,
                    expected_hydration=frozen.hydration_receipt_digest,
                    expected_ancestry=ancestry_digest,
                    expected_event=event_digest,
                    expected_native=native_digest,
                    expected_hydrated=hydrated,
                )
        except LiveProcessVerificationError:
            if db.in_transaction:
                db.rollback()
            return self._recover_process_drift(event, entry, invocation)
        except sqlite3.IntegrityError as exc:
            db.rollback()
            raise ConcurrencyConflict("V4 ingress concurrent SessionStart conflict") from exc
        except Exception:
            try:
                if db.in_transaction:
                    db.rollback()
            except sqlite3.ProgrammingError:
                # The commit-unknown branch deliberately closes the primary
                # handle before independent read-only classification.
                pass
            raise
        finally:
            db.close()
        result = self._replay(event, entry, invocation, event_id, key)
        if result.attachment_revision_digest != hydrated or result.event_digest != event_digest:
            raise PolicyViolation("V4 ingress postcommit graph identity drift")
        del native_digest
        return SessionStartIngressResult(
            result.stdout,
            result.manifest_digest,
            result.hydration_receipt_digest,
            result.event_digest,
            result.attachment_revision_digest,
            False,
            False,
        )

    @staticmethod
    def _commit(db: sqlite3.Connection) -> None:
        db.commit()

    def _same_operation_census(
        self,
        db: sqlite3.Connection,
        *,
        attachment_id: str,
        event_id: str,
        operation_key: str,
        entry: LifecycleSpoolEntry,
        allowed_case_id: str | None = None,
        allowed_revision_digest: str | None = None,
    ) -> int:
        return sum(
            int(db.execute(query, arguments).fetchone()[0])
            for query, arguments in (
                (
                    "select count(*) from context_manifest where session_id=?",
                    (self.binding.session_id,),
                ),
                (
                    "select count(*) from hydration_receipt "
                    "where session_id=? or idempotency_key=?",
                    (self.binding.session_id, operation_key),
                ),
                (
                    "select count(*) from continuity_hook_invocation_ancestry_receipt "
                    "where delivery_id=? or observation_digest=?",
                    (entry.delivery_id, entry.observation_digest),
                ),
                (
                    "select count(*) from session_event where session_id=? or id=?",
                    (self.binding.session_id, event_id),
                ),
                (
                    "select count(*) from session_event_detail "
                    "where session_id=? or idempotency_key=? or spool_digest=?",
                    (self.binding.session_id, operation_key, entry.entry_digest),
                ),
                (
                    "select count(*) from continuity_native_event_receipt "
                    "where delivery_id=? or spool_digest=? or observation_digest=?",
                    (entry.delivery_id, entry.entry_digest, entry.observation_digest),
                ),
                (
                    "select count(*) from continuity_hook_attachment_revision "
                    "where attachment_id=? and operation_key in (?,?,?) "
                    "and (? is null or revision_digest<>?)",
                    (
                        attachment_id,
                        operation_key,
                        f"{operation_key}:process-drift",
                        f"{operation_key}:transaction-unknown",
                        allowed_revision_digest,
                        allowed_revision_digest,
                    ),
                ),
                (
                    "select count(*) from continuity_hook_recovery_case "
                    "where attachment_id=? and session_id=? "
                    "and (? is null or recovery_case_id<>?)",
                    (
                        attachment_id,
                        self.binding.session_id,
                        allowed_case_id,
                        allowed_case_id,
                    ),
                ),
                (
                    "select count(*) from continuity_hook_recovery_resolution r "
                    "join continuity_hook_recovery_case c "
                    "on c.recovery_case_id=r.recovery_case_id "
                    "where c.attachment_id=? and c.session_id=?",
                    (attachment_id, self.binding.session_id),
                ),
            )
        )

    def _verify_hydrated_graph(
        self,
        db: sqlite3.Connection,
        *,
        event: CodexMacOS0151Event,
        entry: LifecycleSpoolEntry,
        revision: sqlite3.Row,
    ) -> FrozenCurrentStartupContext:
        event_id = _event_uuid(self.binding, entry.entry_digest)
        key = _operation_key(event_id)
        if revision["state"] != "hydrated" or revision["operation_key"] != key:
            raise PolicyViolation("V4 ingress exact hydrated evidence required")
        predecessor = db.execute(
            "select * from continuity_hook_attachment_revision where revision_digest=?",
            (revision["previous_revision_digest"],),
        ).fetchone()
        if predecessor is None or predecessor["state"] != "attached":
            raise PolicyViolation("V4 ingress hydrated predecessor missing")
        frozen = self.context_port.build(
            self.binding, hydration_key=key, observed_at=str(revision["created_at"])
        )
        if type(frozen) is not FrozenCurrentStartupContext:
            raise ValidationFailed("V4 ingress exact historical context required")
        frozen.__post_init__()
        self.context_port.assert_current(self.binding, frozen)
        manifest = db.execute(
            "select * from context_manifest where manifest_digest=? and session_id=?",
            (revision["active_manifest_digest"], self.binding.session_id),
        ).fetchone()
        hydration = db.execute(
            "select * from hydration_receipt where receipt_digest=? and session_id=?",
            (revision["active_hydration_receipt_digest"], self.binding.session_id),
        ).fetchone()
        latest = db.execute(
            "select receipt_digest from hydration_receipt where session_id=? "
            "order by created_at desc,receipt_digest desc limit 1",
            (self.binding.session_id,),
        ).fetchone()
        source = db.execute(
            "select id,revision_ref from source_snapshot where id=?",
            (self.binding.source_snapshot_id,),
        ).fetchone()
        if (
            manifest is None
            or hydration is None
            or latest is None
            or latest[0] != hydration["receipt_digest"]
            or source is None
            or frozen.manifest_digest != revision["active_manifest_digest"]
            or frozen.hydration_receipt_digest != revision["active_hydration_receipt_digest"]
            or db.execute(
                "select count(*) from context_manifest where session_id=?",
                (self.binding.session_id,),
            ).fetchone()[0]
            != 1
            or db.execute(
                "select count(*) from hydration_receipt where session_id=?",
                (self.binding.session_id,),
            ).fetchone()[0]
            != 1
        ):
            raise PolicyViolation("V4 ingress historical context graph missing")
        verify_persisted_context_manifest(
            binding=self.binding,
            manifest_digest=frozen.manifest_digest,
            row_columns=dict(manifest),
            body_json=str(manifest["body_json"]),
            active_hydration_receipt=dict(hydration),
            db_source_revision=str(source["revision_ref"]),
            port_source_revision=frozen.source_snapshot.revision_ref,
        )
        event_row = db.execute(
            "select * from session_event where id=? and session_id=?",
            (event_id, self.binding.session_id),
        ).fetchone()
        detail = db.execute(
            "select * from session_event_detail where event_id=? and session_id=?",
            (event_id, self.binding.session_id),
        ).fetchone()
        native_rows = db.execute(
            "select * from continuity_native_event_receipt where event_digest=?",
            (None if detail is None else detail["event_digest"],),
        ).fetchall()
        if event_row is None or detail is None or len(native_rows) != 1:
            raise PolicyViolation("V4 ingress historical event/native graph missing")
        native = native_rows[0]
        ancestry_rows = db.execute(
            "select * from continuity_hook_invocation_ancestry_receipt where receipt_digest=?",
            (native["ancestry_receipt_digest"],),
        ).fetchall()
        if len(ancestry_rows) != 1:
            raise PolicyViolation("V4 ingress historical ancestry missing")
        ancestry = ancestry_rows[0]
        commands = verify_reviewed_hook_commands(db, str(revision["attachment_id"]))
        generation = db.execute(
            "select * from continuity_hook_process_generation "
            "where process_generation_digest=? and attachment_id=?",
            (revision["process_generation_digest"], revision["attachment_id"]),
        ).fetchone()
        if generation is None:
            raise PolicyViolation("V4 ingress historical generation missing")
        verified_events = SQLiteDormantV4CloseWriter._events(db, self.binding)
        native_event_count = sum(
            row["event_kind"] in {"SESSION_START", "PRE_COMPACTION", "POST_COMPACTION"}
            for row in verified_events
        )
        cardinality = db.execute(
            "select "
            "(select count(*) from session_event e where e.session_id=?),"
            "(select count(*) from session_event_detail d where d.session_id=?),"
            "(select count(*) from continuity_internal_event_receipt i "
            " where i.session_id=?),"
            "(select count(*) from continuity_native_event_receipt n "
            " join continuity_hook_process_generation g "
            " on g.process_generation_digest=n.process_generation_digest "
            " join continuity_hook_attachment a on a.attachment_id=g.attachment_id "
            " where a.attachment_id=? and a.session_id=?),"
            "(select count(*) from continuity_hook_invocation_ancestry_receipt ar "
            " join continuity_hook_process_generation g "
            " on g.process_generation_digest=ar.process_generation_digest "
            " join continuity_hook_attachment a on a.attachment_id=g.attachment_id "
            " where a.attachment_id=? and a.session_id=?)",
            (
                self.binding.session_id,
                self.binding.session_id,
                self.binding.session_id,
                revision["attachment_id"],
                self.binding.session_id,
                revision["attachment_id"],
                self.binding.session_id,
            ),
        ).fetchone()
        expected_cardinality = (
            len(verified_events),
            len(verified_events),
            len(verified_events) - native_event_count,
            native_event_count,
            native_event_count,
        )
        # The shared event verifier admits later, fully-produced Slice-B event
        # chain entries without treating them as Slice-A authority.  Cardinality
        # then proves that no event, detail, internal/native producer, or ancestry
        # exists outside that verified chain.  Slice A itself contributes exactly
        # one native SessionStart at the head.
        if (
            cardinality is None
            or tuple(cardinality) != expected_cardinality
            or not verified_events
            or verified_events[0]["id"] != event_id
            or verified_events[0]["event_kind"] != "SESSION_START"
            or sum(row["event_kind"] == "SESSION_START" for row in verified_events) != 1
        ):
            raise PolicyViolation("V4 ingress Slice A event graph cardinality drift")
        ancestry_columns = {
            name: ancestry[name]
            for name in tuple(ancestry.keys())
            if name not in {"receipt_digest", "body_json"}
        }
        native_columns = {
            name: native[name]
            for name in tuple(native.keys())
            if name not in {"receipt_digest", "body_json"}
        }
        try:
            ancestry_body = json.loads(ancestry["body_json"])
            native_body = json.loads(native["body_json"])
            envelope = json.loads(detail["body_json"])
        except (TypeError, ValueError, RecursionError) as exc:
            raise PolicyViolation("V4 ingress historical graph JSON invalid") from exc
        expected_event = {
            "kind": "SESSION_START",
            "idempotency_key": key,
            "occurred_at": str(revision["created_at"]),
            "source_refs": sorted({item.source_ref for item in frozen.context.manifest.selected}),
            "evidence_digests": [
                frozen.manifest_digest,
                frozen.hydration_receipt_digest,
                ancestry["receipt_digest"],
                frozen.output_digest,
            ],
            "spool_digest": entry.entry_digest,
        }
        expected_envelope = {
            "session_id": self.binding.session_id,
            "binding_digest": self.binding.binding_digest,
            "sequence": 1,
            "previous_digest": None,
            "event": expected_event,
        }
        expected_revision = _revision_body(
            attachment_id=str(revision["attachment_id"]),
            revision_number=int(predecessor["revision_number"]) + 1,
            previous_revision_digest=str(predecessor["revision_digest"]),
            operation_key=key,
            state="hydrated",
            process_generation_digest=str(generation["process_generation_digest"]),
            active_manifest_digest=frozen.manifest_digest,
            active_hydration_receipt_digest=frozen.hydration_receipt_digest,
            created_at=str(revision["created_at"]),
        )
        revision_columns = {
            name: revision[name]
            for name in tuple(revision.keys())
            if name not in {"revision_digest", "body_json"}
        }
        if (
            ancestry_body
            != {"schema": "zekam-hook-invocation-ancestry-receipt/v1", **ancestry_columns}
            or canonical_json(ancestry_body) != ancestry["body_json"]
            or digest(ancestry_body) != ancestry["receipt_digest"]
            or native_body != native_columns
            or canonical_json(native_body) != native["body_json"]
            or digest(native_body) != native["receipt_digest"]
            or envelope != expected_envelope
            or canonical_json(envelope) != detail["body_json"]
            or digest(envelope) != detail["event_digest"]
            or event_row["event_digest"] != detail["event_digest"]
            or event_row["event_kind"] != "SESSION_START"
            or event_row["created_at"] != revision["created_at"]
            or detail["sequence"] != 1
            or detail["previous_digest"] is not None
            or detail["idempotency_key"] != key
            or detail["spool_digest"] != entry.entry_digest
            or native["attachment_revision_digest"] != predecessor["revision_digest"]
            or native["approval_inherited"] != 0
            or native["grants_authority"] != 0
            or native["hydration_receipt_digest"] != frozen.hydration_receipt_digest
            or native["created_at"] != revision["created_at"]
            or native["delivery_id"] != entry.delivery_id
            or native["external_event_type"] != "SessionStart"
            or native["internal_event_type"] != "SESSION_START"
            or native["external_turn_id"] is not None
            or native["external_trigger_id"] is not None
            or native["spool_sequence"] != 1
            or native["previous_spool_digest"] is not None
            or native["event_digest"] != detail["event_digest"]
            or native["spool_digest"] != entry.entry_digest
            or native["observation_digest"] != entry.observation_digest
            or native["process_generation_digest"] != generation["process_generation_digest"]
            or native["ancestry_receipt_digest"] != ancestry["receipt_digest"]
            or ancestry["delivery_id"] != entry.delivery_id
            or ancestry["observation_digest"] != entry.observation_digest
            or ancestry["external_event_type"] != "SessionStart"
            or ancestry["approval_inherited"] != 0
            or ancestry["grants_authority"] != 0
            or ancestry["observed_at"] != revision["created_at"]
            or ancestry["topology"] != _TOPOLOGY
            or ancestry["launch_command_digest"] != commands[0].command_digest
            or ancestry["process_generation_digest"] != generation["process_generation_digest"]
            or ancestry["native_pid"] != generation["native_pid"]
            or ancestry["native_uid"] != generation["native_uid"]
            or ancestry["native_start_token"] != generation["native_start_token"]
            or ancestry["native_artifact_digest"] != generation["native_artifact_digest"]
            or ancestry["ancestry_policy_digest"] != generation["ancestry_policy_digest"]
            or ancestry["shell_pid"] != ancestry["hook_pid"]
            or ancestry["shell_uid"] != ancestry["hook_uid"]
            or ancestry["shell_start_token"] != ancestry["hook_start_token"]
            or ancestry["hook_parent_pid"] != ancestry["native_pid"]
            or ancestry["hook_parent_uid"] != ancestry["native_uid"]
            or ancestry["hook_parent_start_token"] != ancestry["native_start_token"]
            or native["hook_pid"] != ancestry["hook_pid"]
            or native["hook_uid"] != ancestry["hook_uid"]
            or native["hook_start_token"] != ancestry["hook_start_token"]
            or native["shell_artifact_digest"] != ancestry["shell_artifact_digest"]
            or native["python_launcher_artifact_digest"]
            != ancestry["python_launcher_artifact_digest"]
            or native["python_runtime_artifact_digest"]
            != ancestry["python_runtime_artifact_digest"]
            or revision_columns != expected_revision
            or revision["revision_digest"] != revision_digest(expected_revision)
            or json.loads(revision["body_json"])
            != {"revision_digest": revision["revision_digest"], **expected_revision}
            or canonical_json({"revision_digest": revision["revision_digest"], **expected_revision})
            != revision["body_json"]
        ):
            raise PolicyViolation("V4 ingress historical SessionStart graph drift")
        self._verify_spool(event, entry)
        return frozen

    def _verify_recovery_head(
        self,
        db: sqlite3.Connection,
        *,
        event: CodexMacOS0151Event,
        entry: LifecycleSpoolEntry,
        invocation: ManagedInvocationSnapshot,
        current: sqlite3.Row,
    ) -> tuple[str, str]:
        if current["state"] != "recovery-required":
            raise PolicyViolation("V4 ingress exact recovery-required head required")
        case_id = current["hook_recovery_case_id"]
        case = db.execute(
            "select * from continuity_hook_recovery_case where recovery_case_id=? "
            "and attachment_id=? and session_id=?",
            (case_id, current["attachment_id"], self.binding.session_id),
        ).fetchone()
        predecessor = db.execute(
            "select * from continuity_hook_attachment_revision where revision_digest=?",
            (current["previous_revision_digest"],),
        ).fetchone()
        if case is None or predecessor is None:
            raise PolicyViolation("V4 ingress recovery authority graph missing")
        if (
            db.execute(
                "select count(*) from continuity_hook_recovery_case "
                "where attachment_id=? and session_id=? and process_generation_digest=?",
                (
                    current["attachment_id"],
                    self.binding.session_id,
                    current["process_generation_digest"],
                ),
            ).fetchone()[0]
            != 1
            or db.execute(
                "select count(*) from continuity_hook_recovery_resolution r "
                "join continuity_hook_recovery_case c "
                "on c.recovery_case_id=r.recovery_case_id "
                "where c.attachment_id=? and c.session_id=?",
                (current["attachment_id"], self.binding.session_id),
            ).fetchone()[0]
            != 0
        ):
            raise PolicyViolation("V4 ingress recovery graph cardinality drift")
        case_body = {
            "attachment_id": case["attachment_id"],
            "case_kind": case["case_kind"],
            "created_at": case["created_at"],
            "evidence_digest": case["evidence_digest"],
            "process_generation_digest": case["process_generation_digest"],
            "recovery_case_id": case["recovery_case_id"],
            "session_id": case["session_id"],
        }
        key = _operation_key(_event_uuid(self.binding, entry.entry_digest))
        kind = str(case["case_kind"])
        if kind == "process-drift":
            expected_case_id = str(
                uuid5(
                    _NAMESPACE,
                    f"process-drift|{current['attachment_id']}|{entry.entry_digest}",
                )
            )
            command = verify_reviewed_hook_commands(db, str(current["attachment_id"]))[0]
            evidence_body = {
                "schema": "zekam-hook-process-drift-evidence/v2",
                "case_category": "live-manager-verification-failed",
                "attachment_id": current["attachment_id"],
                "session_id": self.binding.session_id,
                "external_session_id": self.binding.external_session_id,
                "process_generation_digest": invocation.process_generation_digest,
                "reviewed_command_digest": command.command_digest,
                "spool_digest": entry.entry_digest,
                "observation_digest": entry.observation_digest,
                "expected_internal_event_type": "SESSION_START",
            }
            expected_operation = f"{key}:process-drift"
            if predecessor["state"] == "hydrated":
                self._verify_hydrated_graph(db, event=event, entry=entry, revision=predecessor)
            elif predecessor["state"] == "attached":
                if (
                    self._same_operation_census(
                        db,
                        attachment_id=str(current["attachment_id"]),
                        event_id=_event_uuid(self.binding, entry.entry_digest),
                        operation_key=key,
                        entry=entry,
                        allowed_case_id=str(case_id),
                        allowed_revision_digest=str(current["revision_digest"]),
                    )
                    != 0
                ):
                    raise PolicyViolation("V4 ingress process drift contains partial effects")
            else:
                raise PolicyViolation("V4 ingress process drift predecessor unsupported")
        elif kind == "transaction-unknown":
            expected_case_id = str(uuid5(_NAMESPACE, f"unknown|{current['attachment_id']}|{key}"))
            frozen = self.context_port.build(
                self.binding,
                hydration_key=key,
                observed_at=entry.occurred_at.isoformat(timespec="seconds"),
            )
            ancestry_body = self._ancestry_body(invocation)
            ancestry_digest = digest(ancestry_body)
            event_body = {
                "kind": "SESSION_START",
                "idempotency_key": key,
                "occurred_at": invocation.observed_at,
                "source_refs": sorted(
                    {item.source_ref for item in frozen.context.manifest.selected}
                ),
                "evidence_digests": [
                    frozen.manifest_digest,
                    frozen.hydration_receipt_digest,
                    ancestry_digest,
                    frozen.output_digest,
                ],
                "spool_digest": entry.entry_digest,
            }
            event_digest = digest(
                {
                    "session_id": self.binding.session_id,
                    "binding_digest": self.binding.binding_digest,
                    "sequence": 1,
                    "previous_digest": None,
                    "event": event_body,
                }
            )
            native_digest = digest(
                self._native_body(
                    current_revision=str(predecessor["revision_digest"]),
                    invocation=invocation,
                    ancestry_digest=ancestry_digest,
                    hydration_digest=frozen.hydration_receipt_digest,
                    event_digest=event_digest,
                )
            )
            hypothetical_hydrated = revision_digest(
                _revision_body(
                    attachment_id=str(current["attachment_id"]),
                    revision_number=int(predecessor["revision_number"]) + 1,
                    previous_revision_digest=str(predecessor["revision_digest"]),
                    operation_key=key,
                    state="hydrated",
                    process_generation_digest=invocation.process_generation_digest,
                    active_manifest_digest=frozen.manifest_digest,
                    active_hydration_receipt_digest=frozen.hydration_receipt_digest,
                    created_at=invocation.observed_at,
                )
            )
            evidence_body = {
                "schema": "zekam-hook-transaction-unknown-evidence/v1",
                "attachment_id": current["attachment_id"],
                "session_id": self.binding.session_id,
                "process_generation_digest": invocation.process_generation_digest,
                "operation_key": key,
                "expected_manifest_digest": frozen.manifest_digest,
                "expected_hydration_receipt_digest": frozen.hydration_receipt_digest,
                "expected_event_digest": event_digest,
                "expected_native_receipt_digest": native_digest,
                "expected_hydrated_revision_digest": hypothetical_hydrated,
                "observed_state": "no-effect",
            }
            expected_operation = f"{key}:transaction-unknown"
            if predecessor["state"] != "attached":
                raise PolicyViolation("V4 ingress transaction recovery predecessor drift")
            if (
                self._same_operation_census(
                    db,
                    attachment_id=str(current["attachment_id"]),
                    event_id=_event_uuid(self.binding, entry.entry_digest),
                    operation_key=key,
                    entry=entry,
                    allowed_case_id=str(case_id),
                    allowed_revision_digest=str(current["revision_digest"]),
                )
                != 0
            ):
                raise PolicyViolation("V4 ingress transaction recovery contains partial effects")
        else:
            raise PolicyViolation("V4 ingress unsupported recovery case kind")
        expected_revision = _revision_body(
            attachment_id=str(current["attachment_id"]),
            revision_number=int(predecessor["revision_number"]) + 1,
            previous_revision_digest=str(predecessor["revision_digest"]),
            operation_key=expected_operation,
            state="recovery-required",
            process_generation_digest=invocation.process_generation_digest,
            active_manifest_digest=predecessor["active_manifest_digest"],
            active_hydration_receipt_digest=predecessor["active_hydration_receipt_digest"],
            hook_recovery_case_id=expected_case_id,
            created_at=str(case["created_at"]),
        )
        stored_revision = {
            key_name: current[key_name]
            for key_name in tuple(current.keys())
            if key_name not in {"revision_digest", "body_json"}
        }
        try:
            case_document = json.loads(case["body_json"])
            revision_document = json.loads(current["body_json"])
        except (TypeError, ValueError, RecursionError) as exc:
            raise PolicyViolation("V4 ingress recovery authority JSON invalid") from exc
        if (
            case_id != expected_case_id
            or case["process_generation_digest"] != invocation.process_generation_digest
            or case["evidence_digest"] != digest(evidence_body)
            or case_document != case_body
            or canonical_json(case_body) != case["body_json"]
            or current["created_at"] != case["created_at"]
            or stored_revision != expected_revision
            or current["revision_digest"] != revision_digest(expected_revision)
            or revision_document
            != {"revision_digest": current["revision_digest"], **expected_revision}
            or canonical_json({"revision_digest": current["revision_digest"], **expected_revision})
            != current["body_json"]
        ):
            raise PolicyViolation("V4 ingress recovery authority parity drift")
        return str(case_id), str(current["revision_digest"])

    def _classify_committed_recovery(
        self,
        *,
        event: CodexMacOS0151Event,
        entry: LifecycleSpoolEntry,
        invocation: ManagedInvocationSnapshot,
        case_id: str,
        recovered_revision_digest: str,
    ) -> SessionStartIngressResult:
        with closing(self._connect(read_only=True)) as db:
            db.execute("begin")
            self._assert_binding(db)
            case = db.execute(
                "select * from continuity_hook_recovery_case where recovery_case_id=? "
                "and session_id=?",
                (case_id, self.binding.session_id),
            ).fetchone()
            revision = db.execute(
                "select * from continuity_hook_attachment_revision where revision_digest=? "
                "and hook_recovery_case_id=? and state='recovery-required'",
                (recovered_revision_digest, case_id),
            ).fetchone()
            if case is None and revision is None:
                attachment = db.execute(
                    "select attachment_id from continuity_hook_attachment where session_id=?",
                    (self.binding.session_id,),
                ).fetchone()
                if attachment is None:
                    raise PolicyViolation("V4 ingress recovery attachment missing")
                current = self._current_revision(db, str(attachment["attachment_id"]))
                operation_key = _operation_key(_event_uuid(self.binding, entry.entry_digest))
                if (
                    current["state"] != "attached"
                    or self._same_operation_census(
                        db,
                        attachment_id=str(attachment["attachment_id"]),
                        event_id=_event_uuid(self.binding, entry.entry_digest),
                        operation_key=operation_key,
                        entry=entry,
                    )
                    != 0
                ):
                    raise PolicyViolation("V4 ingress recovery no-effect census conflict")
                return SessionStartIngressResult(
                    handled_failure_output(recovery_required=True),
                    None,
                    None,
                    None,
                    str(current["revision_digest"]),
                    False,
                    True,
                )
            if case is None or revision is None:
                raise PolicyViolation("V4 ingress recovery graph is partial")
            current = self._current_revision(db, str(revision["attachment_id"]))
            if (
                current["revision_digest"] != recovered_revision_digest
                or current["hook_recovery_case_id"] != case_id
            ):
                raise PolicyViolation("V4 ingress committed recovery is not current head")
            self._verify_recovery_head(
                db,
                event=event,
                entry=entry,
                invocation=invocation,
                current=current,
            )
            return SessionStartIngressResult(
                handled_failure_output(recovery_required=True),
                None,
                None,
                None,
                recovered_revision_digest,
                True,
                True,
            )

    def _recover_commit_unknown(
        self,
        *,
        event: CodexMacOS0151Event,
        entry: LifecycleSpoolEntry,
        invocation: ManagedInvocationSnapshot,
        event_id: str,
        operation_key: str,
        expected_manifest: str,
        expected_hydration: str,
        expected_ancestry: str,
        expected_event: str,
        expected_native: str,
        expected_hydrated: str,
    ) -> SessionStartIngressResult:
        try:
            replay = self._replay(event, entry, invocation, event_id, operation_key)
        except (PolicyViolation, ConcurrencyConflict, ConfigurationError, ValidationFailed):
            replay = None
        if replay is not None:
            return replay
        recovery_at = self.process_manager.recovery_time()
        db = self._connect()
        try:
            db.execute("begin immediate")
            self._assert_binding(db)
            attachment = db.execute(
                "select attachment_id from continuity_hook_attachment where session_id=?",
                (self.binding.session_id,),
            ).fetchone()
            if attachment is None:
                raise PolicyViolation("V4 ingress recovery attachment missing")
            attachment_id = str(attachment[0])
            current = self._current_revision(db, attachment_id)
            rows = sum(
                int(db.execute(query, arguments).fetchone()[0])
                for query, arguments in (
                    (
                        "select count(*) from context_manifest where manifest_digest=?",
                        (expected_manifest,),
                    ),
                    (
                        "select count(*) from hydration_receipt where receipt_digest=?",
                        (expected_hydration,),
                    ),
                    (
                        "select count(*) from continuity_hook_invocation_ancestry_receipt "
                        "where receipt_digest=?",
                        (expected_ancestry,),
                    ),
                    (
                        "select count(*) from session_event where id=? or event_digest=?",
                        (event_id, expected_event),
                    ),
                    (
                        "select count(*) from session_event_detail where event_digest=?",
                        (expected_event,),
                    ),
                    (
                        "select count(*) from continuity_native_event_receipt "
                        "where receipt_digest=?",
                        (expected_native,),
                    ),
                    (
                        "select count(*) from continuity_hook_attachment_revision "
                        "where revision_digest=?",
                        (expected_hydrated,),
                    ),
                )
            )
            conflicting_identity_rows = sum(
                int(db.execute(query, arguments).fetchone()[0])
                for query, arguments in (
                    (
                        "select count(*) from context_manifest where session_id=?",
                        (self.binding.session_id,),
                    ),
                    (
                        "select count(*) from hydration_receipt "
                        "where session_id=? or idempotency_key=?",
                        (self.binding.session_id, operation_key),
                    ),
                    (
                        "select count(*) from continuity_hook_invocation_ancestry_receipt "
                        "where delivery_id=? or receipt_digest=?",
                        (entry.delivery_id, expected_ancestry),
                    ),
                    (
                        "select count(*) from session_event "
                        "where session_id=? or id=? or event_digest=?",
                        (self.binding.session_id, event_id, expected_event),
                    ),
                    (
                        "select count(*) from session_event_detail "
                        "where session_id=? or idempotency_key=? or spool_digest=? "
                        "or event_digest=?",
                        (
                            self.binding.session_id,
                            operation_key,
                            entry.entry_digest,
                            expected_event,
                        ),
                    ),
                    (
                        "select count(*) from continuity_native_event_receipt "
                        "where delivery_id=? or spool_digest=? or observation_digest=? "
                        "or event_digest=? or receipt_digest=?",
                        (
                            entry.delivery_id,
                            entry.entry_digest,
                            entry.observation_digest,
                            expected_event,
                            expected_native,
                        ),
                    ),
                    (
                        "select count(*) from continuity_hook_attachment_revision "
                        "where attachment_id=? and operation_key=?",
                        (attachment_id, operation_key),
                    ),
                )
            )
            conflicting_identity_rows += self._same_operation_census(
                db,
                attachment_id=attachment_id,
                event_id=event_id,
                operation_key=operation_key,
                entry=entry,
            )
            if current["state"] == "recovery-required":
                self._verify_recovery_head(
                    db,
                    event=event,
                    entry=entry,
                    invocation=invocation,
                    current=current,
                )
                db.rollback()
                return SessionStartIngressResult(
                    handled_failure_output(recovery_required=True),
                    None,
                    None,
                    None,
                    str(current["revision_digest"]),
                    True,
                    True,
                )
            if current["state"] != "attached" or rows != 0 or conflicting_identity_rows != 0:
                db.rollback()
                return SessionStartIngressResult(
                    handled_failure_output(recovery_required=True),
                    None,
                    None,
                    None,
                    str(current["revision_digest"]),
                    False,
                    True,
                )
            self.process_manager.assert_invocation(invocation)
            self._verify_spool(event, entry)
            evidence_body = {
                "schema": "zekam-hook-transaction-unknown-evidence/v1",
                "attachment_id": attachment_id,
                "session_id": self.binding.session_id,
                "process_generation_digest": invocation.process_generation_digest,
                "operation_key": operation_key,
                "expected_manifest_digest": expected_manifest,
                "expected_hydration_receipt_digest": expected_hydration,
                "expected_event_digest": expected_event,
                "expected_native_receipt_digest": expected_native,
                "expected_hydrated_revision_digest": expected_hydrated,
                "observed_state": "no-effect",
            }
            evidence_digest = digest(evidence_body)
            case_id = str(uuid5(_NAMESPACE, f"unknown|{attachment_id}|{operation_key}"))
            case_body = {
                "attachment_id": attachment_id,
                "case_kind": "transaction-unknown",
                "created_at": recovery_at,
                "evidence_digest": evidence_digest,
                "process_generation_digest": invocation.process_generation_digest,
                "recovery_case_id": case_id,
                "session_id": self.binding.session_id,
            }
            db.execute(
                "insert into continuity_hook_recovery_case values(?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    attachment_id,
                    self.binding.session_id,
                    invocation.process_generation_digest,
                    "transaction-unknown",
                    evidence_digest,
                    canonical_json(case_body),
                    recovery_at,
                ),
            )
            recovered = _insert_revision(
                db,
                _revision_body(
                    attachment_id=attachment_id,
                    revision_number=int(current["revision_number"]) + 1,
                    previous_revision_digest=str(current["revision_digest"]),
                    operation_key=f"{operation_key}:transaction-unknown",
                    state="recovery-required",
                    process_generation_digest=invocation.process_generation_digest,
                    hook_recovery_case_id=case_id,
                    created_at=recovery_at,
                ),
            )
            self.process_manager.assert_invocation(invocation)
            self._verify_spool(event, entry)
            try:
                self._commit(db)
            except Exception:
                if db.in_transaction:
                    db.rollback()
                db.close()
                return self._classify_committed_recovery(
                    event=event,
                    entry=entry,
                    invocation=invocation,
                    case_id=case_id,
                    recovered_revision_digest=recovered,
                )
            return SessionStartIngressResult(
                handled_failure_output(recovery_required=True),
                None,
                None,
                None,
                recovered,
                False,
                True,
            )
        except sqlite3.IntegrityError as exc:
            if db.in_transaction:
                db.rollback()
            raise ConcurrencyConflict("V4 ingress recovery concurrency conflict") from exc
        finally:
            db.close()

    def _recover_process_drift(
        self,
        event: CodexMacOS0151Event,
        entry: LifecycleSpoolEntry,
        invocation: ManagedInvocationSnapshot,
    ) -> SessionStartIngressResult:
        recovery_at = self.process_manager.recovery_time()
        event_id = _event_uuid(self.binding, entry.entry_digest)
        key = _operation_key(event_id)
        db = self._connect()
        try:
            db.execute("begin immediate")
            self._assert_binding(db)
            attachment = db.execute(
                "select attachment_id from continuity_hook_attachment where session_id=?",
                (self.binding.session_id,),
            ).fetchone()
            if attachment is None:
                raise PolicyViolation("V4 ingress process drift attachment missing")
            attachment_id = str(attachment[0])
            current = self._current_revision(db, attachment_id)
            if current["state"] == "recovery-required":
                self._verify_recovery_head(
                    db,
                    event=event,
                    entry=entry,
                    invocation=invocation,
                    current=current,
                )
                db.rollback()
                return SessionStartIngressResult(
                    handled_failure_output(recovery_required=True),
                    None,
                    None,
                    None,
                    str(current["revision_digest"]),
                    True,
                    True,
                )
            if current["state"] not in {"attached", "hydrated"}:
                raise PolicyViolation("V4 ingress process drift predecessor is unsupported")
            if current["state"] == "hydrated":
                self._verify_hydrated_graph(db, event=event, entry=entry, revision=current)
            command = verify_reviewed_hook_commands(db, attachment_id)[0]
            generation = db.execute(
                "select process_generation_digest from continuity_hook_process_generation "
                "where process_generation_digest=? and attachment_id=?",
                (invocation.process_generation_digest, attachment_id),
            ).fetchone()
            if (
                generation is None
                or command.command_digest != invocation.launch_command_digest
                or invocation.observation_digest != entry.observation_digest
                or invocation.spool_digest != entry.entry_digest
            ):
                raise PolicyViolation("V4 ingress process drift durable scope mismatch")
            self._verify_spool(event, entry)
            evidence_body = {
                "schema": "zekam-hook-process-drift-evidence/v2",
                "case_category": "live-manager-verification-failed",
                "attachment_id": attachment_id,
                "session_id": self.binding.session_id,
                "external_session_id": self.binding.external_session_id,
                "process_generation_digest": invocation.process_generation_digest,
                "reviewed_command_digest": command.command_digest,
                "spool_digest": entry.entry_digest,
                "observation_digest": entry.observation_digest,
                "expected_internal_event_type": "SESSION_START",
            }
            evidence_digest = digest(evidence_body)
            case_id = str(uuid5(_NAMESPACE, f"process-drift|{attachment_id}|{entry.entry_digest}"))
            case_body = {
                "attachment_id": attachment_id,
                "case_kind": "process-drift",
                "created_at": recovery_at,
                "evidence_digest": evidence_digest,
                "process_generation_digest": invocation.process_generation_digest,
                "recovery_case_id": case_id,
                "session_id": self.binding.session_id,
            }
            db.execute(
                "insert into continuity_hook_recovery_case values(?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    attachment_id,
                    self.binding.session_id,
                    invocation.process_generation_digest,
                    "process-drift",
                    evidence_digest,
                    canonical_json(case_body),
                    recovery_at,
                ),
            )
            recovered = _insert_revision(
                db,
                _revision_body(
                    attachment_id=attachment_id,
                    revision_number=int(current["revision_number"]) + 1,
                    previous_revision_digest=str(current["revision_digest"]),
                    operation_key=f"{key}:process-drift",
                    state="recovery-required",
                    process_generation_digest=invocation.process_generation_digest,
                    active_manifest_digest=current["active_manifest_digest"],
                    active_hydration_receipt_digest=current["active_hydration_receipt_digest"],
                    hook_recovery_case_id=case_id,
                    created_at=recovery_at,
                ),
            )
            self._assert_binding(db)
            self._verify_spool(event, entry)
            try:
                self._commit(db)
            except Exception:
                if db.in_transaction:
                    db.rollback()
                db.close()
                return self._classify_committed_recovery(
                    event=event,
                    entry=entry,
                    invocation=invocation,
                    case_id=case_id,
                    recovered_revision_digest=recovered,
                )
            return SessionStartIngressResult(
                handled_failure_output(recovery_required=True),
                None,
                None,
                None,
                recovered,
                False,
                True,
            )
        except sqlite3.IntegrityError as exc:
            if db.in_transaction:
                db.rollback()
            raise ConcurrencyConflict("V4 ingress process drift concurrency conflict") from exc
        finally:
            db.close()

    def _insert_context(self, db: sqlite3.Connection, frozen: FrozenCurrentStartupContext) -> None:
        db.execute(
            "insert into context_manifest values(?,?,?,?,?,?,?)",
            (
                frozen.manifest_digest,
                self.binding.session_id,
                None,
                frozen.context.manifest.token_budget,
                sum(item.token_count for item in frozen.context.manifest.selected),
                frozen.manifest_body_json,
                frozen.observed_at,
            ),
        )
        db.execute(
            "insert into hydration_receipt values(?,?,?,?,?)",
            (
                frozen.hydration_receipt_digest,
                self.binding.session_id,
                frozen.manifest_digest,
                frozen.hydration_key,
                frozen.observed_at,
            ),
        )

    @staticmethod
    def _ancestry_body(invocation: ManagedInvocationSnapshot) -> dict[str, Any]:
        return {
            "schema": "zekam-hook-invocation-ancestry-receipt/v1",
            "ancestry_policy_digest": invocation.ancestry_policy_digest,
            "approval_inherited": 0,
            "delivery_id": invocation.delivery_id,
            "external_event_type": "SessionStart",
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
            "observed_at": invocation.observed_at,
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

    @classmethod
    def _insert_ancestry(cls, db: sqlite3.Connection, invocation: ManagedInvocationSnapshot) -> str:
        body = cls._ancestry_body(invocation)
        value = digest(body)
        stored = {
            "receipt_digest": value,
            **{key: val for key, val in body.items() if key != "schema"},
        }
        columns = tuple(stored)
        db.execute(
            "insert into continuity_hook_invocation_ancestry_receipt("
            + ",".join(columns)
            + ",body_json) values("
            + ",".join("?" for _ in range(len(columns) + 1))
            + ")",
            (*stored.values(), canonical_json(body)),
        )
        return value

    @staticmethod
    def _native_body(
        *,
        current_revision: str,
        invocation: ManagedInvocationSnapshot,
        ancestry_digest: str,
        hydration_digest: str,
        event_digest: str,
    ) -> dict[str, Any]:
        return {
            "ancestry_receipt_digest": ancestry_digest,
            "approval_inherited": 0,
            "attachment_revision_digest": current_revision,
            "created_at": invocation.observed_at,
            "delivery_id": invocation.delivery_id,
            "event_digest": event_digest,
            "external_event_type": "SessionStart",
            "external_trigger_id": None,
            "external_turn_id": None,
            "grants_authority": 0,
            "hook_pid": invocation.hook_pid,
            "hook_start_token": invocation.hook_start_token,
            "hook_uid": invocation.hook_uid,
            "hydration_receipt_digest": hydration_digest,
            "internal_event_type": "SESSION_START",
            "observation_digest": invocation.observation_digest,
            "previous_spool_digest": None,
            "process_generation_digest": invocation.process_generation_digest,
            "python_launcher_artifact_digest": invocation.python_launcher_artifact_digest,
            "python_runtime_artifact_digest": invocation.python_runtime_artifact_digest,
            "shell_artifact_digest": invocation.shell_artifact_digest,
            "shell_pid": invocation.hook_pid,
            "shell_start_token": invocation.hook_start_token,
            "shell_uid": invocation.hook_uid,
            "spool_digest": invocation.spool_digest,
            "spool_sequence": 1,
        }

    def _insert_native(
        self,
        db: sqlite3.Connection,
        *,
        current_revision: str,
        invocation: ManagedInvocationSnapshot,
        ancestry_digest: str,
        hydration_digest: str,
        event_digest: str,
    ) -> str:
        body = self._native_body(
            current_revision=current_revision,
            invocation=invocation,
            ancestry_digest=ancestry_digest,
            hydration_digest=hydration_digest,
            event_digest=event_digest,
        )
        value = digest(body)
        stored = {"receipt_digest": value, **body}
        columns = tuple(stored)
        db.execute(
            "insert into continuity_native_event_receipt("
            + ",".join(columns)
            + ",body_json) values("
            + ",".join("?" for _ in range(len(columns) + 1))
            + ")",
            (*stored.values(), canonical_json(body)),
        )
        return value

    def _replay(
        self,
        event: CodexMacOS0151Event,
        entry: LifecycleSpoolEntry,
        invocation: ManagedInvocationSnapshot,
        event_id: str,
        key: str,
    ) -> SessionStartIngressResult:
        self.process_manager.assert_invocation(invocation)
        with closing(self._connect(read_only=True)) as db:
            db.execute("begin")
            self._assert_binding(db)
            attachment = db.execute(
                "select attachment_id from continuity_hook_attachment where session_id=?",
                (self.binding.session_id,),
            ).fetchone()
            if attachment is None:
                raise PolicyViolation("V4 ingress replay attachment missing")
            process = self.process_manager.capture_process(self.binding)
            if type(process) is not ManagedProcessSnapshot:
                raise ValidationFailed("V4 ingress replay exact process snapshot required")
            process.__post_init__()
            self.process_manager.assert_process(process)
            self._verify_attachment(db, process)
            commands = verify_reviewed_hook_commands(db, str(attachment[0]))
            revision = self._current_revision(db, str(attachment[0]))
            if revision["state"] != "hydrated" or revision["operation_key"] != key:
                raise PolicyViolation("V4 ingress exact hydrated replay required")
            verified_frozen = self._verify_hydrated_graph(
                db, event=event, entry=entry, revision=revision
            )
            self._verify_invocation_generation(db, invocation)
            frozen = self.context_port.build(
                self.binding, hydration_key=key, observed_at=str(revision["created_at"])
            )
            if type(frozen) is not FrozenCurrentStartupContext:
                raise ValidationFailed("V4 ingress replay exact context required")
            self.context_port.assert_current(self.binding, frozen)
            if frozen != verified_frozen:
                raise PolicyViolation("V4 ingress repeated context verification drift")
            metadata = db.execute(
                "select manifest_digest,session_id,checkpoint_digest,token_budget,token_count,"
                "typeof(body_json) as body_type,length(cast(body_json as blob)) as body_bytes "
                "from context_manifest where manifest_digest=? and session_id=?",
                (revision["active_manifest_digest"], self.binding.session_id),
            ).fetchone()
            if (
                metadata is None
                or metadata["body_type"] != "text"
                or type(metadata["body_bytes"]) is not int
                or not 1 <= metadata["body_bytes"] <= 1_048_576
            ):
                raise PolicyViolation("V4 ingress replay bounded manifest missing")
            manifest = db.execute(
                "select manifest_digest,session_id,checkpoint_digest,token_budget,token_count,"
                "body_json from context_manifest where manifest_digest=? and session_id=?",
                (revision["active_manifest_digest"], self.binding.session_id),
            ).fetchone()
            hydration = db.execute(
                "select * from hydration_receipt where receipt_digest=?",
                (revision["active_hydration_receipt_digest"],),
            ).fetchone()
            latest = db.execute(
                "select receipt_digest from hydration_receipt where session_id=? "
                "order by created_at desc,receipt_digest desc limit 1",
                (self.binding.session_id,),
            ).fetchone()
            source = db.execute(
                "select id,revision_ref from source_snapshot where id=?",
                (self.binding.source_snapshot_id,),
            ).fetchone()
            if (
                manifest is None
                or hydration is None
                or latest is None
                or latest[0] != hydration["receipt_digest"]
                or source is None
                or source["revision_ref"] != frozen.source_snapshot.revision_ref
                or len(str(manifest["body_json"]).encode("utf-8")) != metadata["body_bytes"]
            ):
                raise PolicyViolation("V4 ingress replay context graph missing")
            verify_persisted_context_manifest(
                binding=self.binding,
                manifest_digest=frozen.manifest_digest,
                row_columns=dict(manifest),
                body_json=str(manifest["body_json"]),
                active_hydration_receipt=dict(hydration),
                db_source_revision=str(source["revision_ref"]),
                port_source_revision=frozen.source_snapshot.revision_ref,
            )
            detail = db.execute(
                "select d.*,e.event_kind,e.created_at as event_created_at "
                "from session_event_detail d "
                "join session_event e on e.id=d.event_id where d.event_id=?",
                (event_id,),
            ).fetchone()
            native = db.execute(
                "select * from continuity_native_event_receipt where event_digest=?",
                (None if detail is None else detail["event_digest"],),
            ).fetchone()
            ancestry = db.execute(
                "select * from continuity_hook_invocation_ancestry_receipt where receipt_digest=?",
                (None if native is None else native["ancestry_receipt_digest"],),
            ).fetchone()
            if detail is None or native is None or ancestry is None:
                raise PolicyViolation("V4 ingress replay event/native graph missing")
            envelope = json.loads(detail["body_json"])
            expected_event = {
                "kind": "SESSION_START",
                "idempotency_key": key,
                "occurred_at": invocation.observed_at,
                "source_refs": sorted(
                    {item.source_ref for item in frozen.context.manifest.selected}
                ),
                "evidence_digests": [
                    frozen.manifest_digest,
                    frozen.hydration_receipt_digest,
                    ancestry["receipt_digest"],
                    frozen.output_digest,
                ],
                "spool_digest": entry.entry_digest,
            }
            expected_envelope = {
                "session_id": self.binding.session_id,
                "binding_digest": self.binding.binding_digest,
                "sequence": 1,
                "previous_digest": None,
                "event": expected_event,
            }
            if (
                envelope != expected_envelope
                or canonical_json(envelope) != detail["body_json"]
                or digest(envelope) != detail["event_digest"]
                or detail["session_id"] != self.binding.session_id
                or detail["sequence"] != 1
                or detail["previous_digest"] is not None
                or detail["spool_digest"] != entry.entry_digest
                or envelope["event"]["evidence_digests"][0] != frozen.manifest_digest
                or envelope["event"]["evidence_digests"][1] != frozen.hydration_receipt_digest
                or envelope["event"]["evidence_digests"][2] != ancestry["receipt_digest"]
                or envelope["event"]["evidence_digests"][3] != frozen.output_digest
                or native["attachment_revision_digest"] != revision["previous_revision_digest"]
                or native["hydration_receipt_digest"] != frozen.hydration_receipt_digest
                or native["spool_digest"] != entry.entry_digest
                or native["delivery_id"] != entry.delivery_id
                or detail["event_kind"] != "SESSION_START"
                or detail["event_created_at"] != invocation.observed_at
            ):
                raise PolicyViolation("V4 ingress replay graph parity drift")
            native_body = json.loads(native["body_json"])
            ancestry_body = json.loads(ancestry["body_json"])
            native_columns = {
                key: native[key]
                for key in tuple(native.keys())
                if key not in {"receipt_digest", "body_json"}
            }
            ancestry_columns = {
                key: ancestry[key]
                for key in tuple(ancestry.keys())
                if key not in {"receipt_digest", "body_json"}
            }
            if (
                type(native_body) is not dict
                or native_body != native_columns
                or canonical_json(native_body) != native["body_json"]
                or digest(native_body) != native["receipt_digest"]
                or type(ancestry_body) is not dict
                or ancestry_body
                != {
                    "schema": "zekam-hook-invocation-ancestry-receipt/v1",
                    **ancestry_columns,
                }
                or canonical_json(ancestry_body) != ancestry["body_json"]
                or digest(ancestry_body) != ancestry["receipt_digest"]
                or ancestry["process_generation_digest"] != native["process_generation_digest"]
                or ancestry["launch_command_digest"] != commands[0].command_digest
            ):
                raise PolicyViolation("V4 ingress replay native/ancestry body drift")
            self._verify_spool(event, entry)
            if digest(frozen.additional_context) != frozen.output_digest:
                raise PolicyViolation("V4 ingress replay output digest drift")
            return SessionStartIngressResult(
                frozen.success_stdout,
                frozen.manifest_digest,
                frozen.hydration_receipt_digest,
                str(detail["event_digest"]),
                str(revision["revision_digest"]),
                True,
                False,
            )
