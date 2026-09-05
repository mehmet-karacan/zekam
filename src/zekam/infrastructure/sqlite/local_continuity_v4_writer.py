"""Explicit dormant operational-v4 atomic close writer.

This adapter is intentionally not composed by any production/default-v3 entrypoint.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from zekam.application.local_continuity import (
    ContinuityBinding,
    ContinuityEvent,
    ContinuityTail,
    uuid_text,
)
from zekam.application.local_continuity_close import (
    CANDIDATE_RECIPE_DIGEST,
    COMPILE_OPERATION,
    FrozenClose,
)
from zekam.application.local_continuity_v4_writer import (
    CanonicalManifestProvenance,
    CurrentSourcePort,
    CurrentSourceSnapshot,
    ExactResolvedRecovery,
    FinalizeClosedWriteRequest,
    FrozenCloseWriteRequest,
    FrozenProjectionHandle,
    FrozenProjectionSnapshot,
    FrozenSpoolHandle,
    FrozenSpoolSnapshot,
    LifecycleSpoolBarrier,
    ProjectionEvidencePort,
    ResolvedManifestFragment,
    VerifiedManifest,
    derived_operation_key,
    event_digest,
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
from zekam.domain.identifiers import new_uuid7
from zekam.infrastructure.sqlite import operational_schema
from zekam.infrastructure.sqlite.continuity_native_verifier import (
    verify_reviewed_hook_commands,
)


def _job_payload(binding: ContinuityBinding, request_digest: str) -> dict[str, Any]:
    return {
        "operation": COMPILE_OPERATION,
        "session_id": binding.session_id,
        "project_id": binding.project_id,
        "work_item_id": binding.work_item_id,
        "run_id": binding.run_id,
        "binding_digest": binding.binding_digest,
        "request_digest": request_digest,
    }


def _compile_outbox_payload(binding: ContinuityBinding, request_digest: str) -> dict[str, str]:
    return {
        "session_id": binding.session_id,
        "binding_digest": binding.binding_digest,
        "request_digest": request_digest,
    }


class SQLiteDormantV4CloseWriter:
    """The only mutating surface in this module; construction grants no authority."""

    def __init__(
        self,
        path: Path,
        *,
        source: CurrentSourcePort,
        spool: LifecycleSpoolBarrier,
        projections: ProjectionEvidencePort,
        busy_timeout_ms: int = 5000,
    ) -> None:
        if type(path) is not type(Path()) or not path.is_absolute():
            raise ValidationFailed("V4 close exact absolute database path required")
        if type(busy_timeout_ms) is not int or not 1 <= busy_timeout_ms <= 30000:
            raise ValidationFailed("V4 close busy timeout outside bound")
        for port, names in (
            (source, ("snapshot", "resolve_fragment", "assert_current")),
            (spool, ("frozen",)),
            (projections, ("frozen",)),
        ):
            if any(not callable(getattr(port, name, None)) for name in names):
                raise ValidationFailed("V4 close fixed evidence ports required")
        self.path = path
        self.source = source
        self.spool = spool
        self.projections = projections
        self.busy_timeout_ms = busy_timeout_ms

    @contextmanager
    def _frozen_spool(self, binding: ContinuityBinding) -> Iterator[FrozenSpoolHandle]:
        try:
            with self.spool.frozen(binding) as handle:
                yield handle
        except PolicyViolation:
            raise
        except (OSError, TimeoutError) as exc:
            raise PolicyViolation("V4 close spool evidence unavailable") from exc

    @contextmanager
    def _frozen_projections(self, frozen: FrozenClose) -> Iterator[FrozenProjectionHandle]:
        try:
            with self.projections.frozen(frozen) as handle:
                yield handle
        except PolicyViolation:
            raise
        except (OSError, TimeoutError) as exc:
            raise PolicyViolation("V4 close projection evidence unavailable") from exc

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if self.path.is_symlink() or not self.path.is_file():
            raise ConfigurationError("V4 close existing regular database required")
        mode = "ro" if read_only else "rw"
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode={mode}",
            uri=True,
            timeout=self.busy_timeout_ms / 1000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys=on")
        if connection.execute("pragma foreign_keys").fetchone()[0] != 1:
            connection.close()
            raise ConfigurationError("V4 close SQLite foreign keys unavailable")
        connection.execute(f"pragma busy_timeout={self.busy_timeout_ms}")
        return connection

    @staticmethod
    def _schema(db: sqlite3.Connection) -> None:
        if operational_schema._validate_connection(db) != 4:
            raise ConfigurationError("V4 close corrected explicit schema required")

    def _source_snapshot(self, binding: ContinuityBinding) -> CurrentSourceSnapshot:
        try:
            snapshot = self.source.snapshot(binding)
        except PolicyViolation:
            raise
        except Exception as exc:
            raise PolicyViolation("V4 close source snapshot unavailable") from exc
        if type(snapshot) is not CurrentSourceSnapshot:
            raise ValidationFailed("V4 close exact source snapshot required")
        snapshot.__post_init__()
        return snapshot

    def _assert_source_current(
        self, binding: ContinuityBinding, snapshot: CurrentSourceSnapshot
    ) -> None:
        try:
            self.source.assert_current(binding, snapshot)
        except PolicyViolation:
            raise
        except Exception as exc:
            raise PolicyViolation("V4 close current source unavailable") from exc

    @staticmethod
    def _binding(db: sqlite3.Connection, binding: ContinuityBinding) -> sqlite3.Row:
        row = db.execute(
            "select s.status,s.project_id as session_project,s.work_item_id as session_work,"
            "s.client_id as session_client,s.device_id as session_device,s.close_receipt_digest,"
            "s.closed_at,p.slug,b.* from continuity_session_binding b "
            "join session s on s.id=b.session_id join project p on p.id=b.project_id "
            "where b.session_id=?",
            (binding.session_id,),
        ).fetchone()
        if row is None:
            raise PolicyViolation("V4 close session binding unavailable")
        expected = {
            "external_session_id": binding.external_session_id,
            "project_id": binding.project_id,
            "realm_id": binding.realm_id,
            "work_item_id": binding.work_item_id,
            "run_id": binding.run_id,
            "client_id": binding.client_id,
            "device_id": binding.device_id,
            "source_snapshot_id": binding.source_snapshot_id,
            "task_digest": binding.task_digest,
            "plan_digest": binding.plan_digest,
            "policy_digest": binding.policy_digest,
            "binding_digest": binding.binding_digest,
            "session_project": binding.project_id,
            "session_work": binding.work_item_id,
            "session_client": binding.client_id,
            "session_device": binding.device_id,
        }
        if any(row[key] != value for key, value in expected.items()):
            raise PolicyViolation("V4 close exact owner binding drift")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _events(db: sqlite3.Connection, binding: ContinuityBinding) -> list[sqlite3.Row]:
        rows = db.execute(
            "select e.id,e.event_kind,e.event_digest,e.created_at,d.sequence,d.previous_digest,"
            "d.idempotency_key,d.spool_digest,d.body_json from session_event_detail d "
            "join session_event e on e.id=d.event_id and e.session_id=d.session_id "
            "where d.session_id=? order by d.sequence",
            (binding.session_id,),
        ).fetchall()
        attachment = db.execute(
            "select attachment_id from continuity_hook_attachment where session_id=?",
            (binding.session_id,),
        ).fetchone()
        if attachment is None:
            raise PolicyViolation("V4 close native attachment missing")
        reviewed = {
            command.external_event_type: command
            for command in verify_reviewed_hook_commands(db, attachment["attachment_id"])
        }
        previous: str | None = None
        for index, row in enumerate(rows, start=1):
            try:
                body = json.loads(row["body_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise PolicyViolation("V4 close malformed event ledger") from exc
            if (
                row["sequence"] != index
                or row["previous_digest"] != previous
                or canonical_json(body) != row["body_json"]
                or digest(body) != row["event_digest"]
                or body.get("session_id") != binding.session_id
                or body.get("binding_digest") != binding.binding_digest
                or body.get("sequence") != index
                or body.get("previous_digest") != previous
                or not isinstance(body.get("event"), dict)
                or body["event"].get("kind") != row["event_kind"]
                or body["event"].get("idempotency_key") != row["idempotency_key"]
                or body["event"].get("spool_digest") != row["spool_digest"]
                or body["event"].get("occurred_at") != row["created_at"]
            ):
                raise PolicyViolation("V4 close event chain integrity drift")
            native = row["event_kind"] in {
                "SESSION_START",
                "PRE_COMPACTION",
                "POST_COMPACTION",
            }
            if native:
                receipt = db.execute(
                    "select * from continuity_native_event_receipt where event_digest=?",
                    (row["event_digest"],),
                ).fetchone()
                ancestry = None
                try:
                    receipt_body = None if receipt is None else json.loads(receipt["body_json"])
                    if receipt is not None:
                        ancestry = db.execute(
                            "select * from continuity_hook_invocation_ancestry_receipt "
                            "where receipt_digest=?",
                            (receipt["ancestry_receipt_digest"],),
                        ).fetchone()
                    ancestry_body = None if ancestry is None else json.loads(ancestry["body_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise PolicyViolation("V4 close malformed native producer") from exc
                receipt_columns = (
                    {}
                    if receipt is None
                    else {
                        key: receipt[key]
                        for key in tuple(receipt.keys())
                        if key not in {"receipt_digest", "body_json"}
                    }
                )
                ancestry_columns = (
                    {}
                    if ancestry is None
                    else {
                        key: ancestry[key]
                        for key in tuple(ancestry.keys())
                        if key not in {"receipt_digest", "body_json"}
                    }
                )
                command = (
                    None if ancestry is None else reviewed.get(str(ancestry["external_event_type"]))
                )
                if (
                    receipt is None
                    or type(receipt_body) is not dict
                    or canonical_json(receipt_body) != receipt["body_json"]
                    or receipt_body != receipt_columns
                    or digest(receipt_body) != receipt["receipt_digest"]
                    or receipt["internal_event_type"] != row["event_kind"]
                    or ancestry is None
                    or type(ancestry_body) is not dict
                    or canonical_json(ancestry_body) != ancestry["body_json"]
                    or ancestry_body
                    != {
                        "schema": "zekam-hook-invocation-ancestry-receipt/v1",
                        **ancestry_columns,
                    }
                    or digest(ancestry_body) != ancestry["receipt_digest"]
                    or ancestry["delivery_id"] != receipt["delivery_id"]
                    or ancestry["process_generation_digest"] != receipt["process_generation_digest"]
                    or command is None
                    or ancestry["launch_command_digest"] != command.command_digest
                    or command.attachment_id != attachment["attachment_id"]
                    or ancestry["external_event_type"] != receipt["external_event_type"]
                    or ancestry["external_event_type"] != command.external_event_type
                    or ancestry["topology"] != command.topology
                    or ancestry["shell_artifact_digest"] != command.shell_artifact_digest
                    or ancestry["python_launcher_artifact_digest"]
                    != command.python_launcher_artifact_digest
                    or ancestry["python_runtime_artifact_digest"]
                    != command.python_runtime_artifact_digest
                    or ancestry["delivery_id"] != receipt["delivery_id"]
                    or ancestry["observation_digest"] != receipt["observation_digest"]
                    or ancestry["shell_pid"] != receipt["shell_pid"]
                    or ancestry["shell_uid"] != receipt["shell_uid"]
                    or ancestry["shell_start_token"] != receipt["shell_start_token"]
                    or ancestry["hook_pid"] != receipt["hook_pid"]
                    or ancestry["hook_uid"] != receipt["hook_uid"]
                    or ancestry["hook_start_token"] != receipt["hook_start_token"]
                    or ancestry["shell_artifact_digest"] != receipt["shell_artifact_digest"]
                    or ancestry["python_launcher_artifact_digest"]
                    != receipt["python_launcher_artifact_digest"]
                    or ancestry["python_runtime_artifact_digest"]
                    != receipt["python_runtime_artifact_digest"]
                    or ancestry["observed_at"] != row["created_at"]
                    or receipt["created_at"] != row["created_at"]
                ):
                    raise PolicyViolation("V4 close native producer integrity drift")
            else:
                receipt = db.execute(
                    "select * from continuity_internal_event_receipt where event_digest=?",
                    (row["event_digest"],),
                ).fetchone()
                try:
                    receipt_body = None if receipt is None else json.loads(receipt["body_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise PolicyViolation("V4 close malformed internal producer") from exc
                producer_columns = (
                    "turn_commit_digest",
                    "effect_claim_id",
                    "effect_receipt_id",
                    "native_event_receipt_digest",
                    "close_request_digest",
                    "close_receipt_digest",
                    "hook_recovery_resolution_id",
                    "local_recovery_resolution_id",
                )
                producers = (
                    []
                    if receipt is None
                    else [name for name in producer_columns if receipt[name] is not None]
                )
                expected_body = (
                    {}
                    if receipt is None
                    else {
                        "attachment_revision_digest": receipt["attachment_revision_digest"],
                        "binding_digest": receipt["binding_digest"],
                        "created_at": receipt["created_at"],
                        "event_digest": receipt["event_digest"],
                        "event_kind": receipt["event_kind"],
                        "expected_previous_event_digest": receipt["expected_previous_event_digest"],
                        "operation_key": receipt["operation_key"],
                        "session_id": receipt["session_id"],
                    }
                )
                if (
                    receipt is None
                    or receipt_body != expected_body
                    or canonical_json(expected_body) != receipt["body_json"]
                    or len(producers) != 1
                    or receipt["session_id"] != binding.session_id
                    or receipt["binding_digest"] != binding.binding_digest
                    or receipt["event_kind"] != row["event_kind"]
                    or receipt["created_at"] != row["created_at"]
                    or receipt["expected_previous_event_digest"] != previous
                    or receipt["receipt_digest"]
                    != internal_receipt_digest(
                        expected_body,
                        producer_kind=producers[0],
                        producer_ref=str(receipt[producers[0]]),
                    )
                ):
                    raise PolicyViolation("V4 close internal producer integrity drift")
            previous = str(row["event_digest"])
        return rows

    @staticmethod
    def _spool_gate(
        db: sqlite3.Connection,
        rows: list[sqlite3.Row],
        snapshot: FrozenSpoolSnapshot,
        binding: ContinuityBinding,
        *,
        allow_controls: bool,
    ) -> None:
        if (
            snapshot.session_id != binding.session_id
            or snapshot.external_session_id != binding.external_session_id
            or snapshot.client_id != binding.client_id
            or not rows
            or rows[0]["event_kind"] != "SESSION_START"
        ):
            raise PolicyViolation("V4 close reviewed SessionStart spool required")
        persisted = tuple(
            str(row["spool_digest"]) for row in rows if row["spool_digest"] is not None
        )
        if persisted != snapshot.entry_digests[: len(persisted)]:
            raise PolicyViolation("V4 close ordinary spool prefix drift")
        suffix = snapshot.entry_digests[len(persisted) :]
        if suffix and not allow_controls:
            raise PolicyViolation("V4 close unpersisted spool delta")
        if suffix:
            controls = tuple(
                str(row[0])
                for row in db.execute(
                    "select spool_digest from continuity_control_event where session_id=? "
                    "order by spool_sequence",
                    (binding.session_id,),
                ).fetchall()
            )
            if controls != suffix:
                raise PolicyViolation("V4 close control spool suffix drift")

    @staticmethod
    def _tail(rows: list[sqlite3.Row]) -> ContinuityTail:
        if not rows:
            return ContinuityTail(0, None)
        return ContinuityTail(int(rows[-1]["sequence"]), str(rows[-1]["event_digest"]))

    @staticmethod
    def _verified_revision(row: sqlite3.Row | None) -> sqlite3.Row:
        if row is None:
            raise PolicyViolation("V4 close attachment revision missing")
        try:
            body_json = row["body_json"]
            if type(body_json) is not str or not 1 <= len(body_json.encode("utf-8")) <= 1_048_576:
                raise PolicyViolation("V4 close revision body outside byte bound")
            body = json.loads(body_json)
            columns = {
                key: row[key]
                for key in row.keys()  # noqa: SIM118 - sqlite3.Row iterates values, not keys.
                if key != "body_json"
            }
            digest_body = {key: value for key, value in body.items() if key != "revision_digest"}
            if (
                type(body) is not dict
                or body != columns
                or canonical_json(body) != body_json
                or type(body.get("revision_digest")) is not str
                or revision_digest(digest_body) != body["revision_digest"]
            ):
                raise PolicyViolation("V4 close revision body/column/digest drift")
        except PolicyViolation:
            raise
        except Exception as exc:
            raise PolicyViolation("V4 close revision durable evidence malformed") from exc
        return row

    @classmethod
    def _current_revision(cls, db: sqlite3.Connection, attachment_id: str) -> sqlite3.Row:
        row = db.execute(
            "select * from continuity_hook_attachment_revision where attachment_id=? "
            "order by revision_number desc limit 1",
            (attachment_id,),
        ).fetchone()
        return cls._verified_revision(row)

    @staticmethod
    def _attachment(db: sqlite3.Connection, binding: ContinuityBinding) -> sqlite3.Row:
        rows = db.execute(
            "select * from continuity_hook_attachment where session_id=?",
            (binding.session_id,),
        ).fetchall()
        if len(rows) != 1:
            raise PolicyViolation("V4 close exact attachment required")
        return cast(sqlite3.Row, rows[0])

    def _manifest(
        self,
        db: sqlite3.Connection,
        binding: ContinuityBinding,
        manifest_digest: str,
        revision: sqlite3.Row,
        source_snapshot: CurrentSourceSnapshot,
    ) -> VerifiedManifest:
        metadata = db.execute(
            "select manifest_digest,session_id,checkpoint_digest,token_budget,token_count,"
            "typeof(body_json) as body_type,length(cast(body_json as blob)) as body_bytes "
            "from context_manifest where manifest_digest=? and session_id=?",
            (manifest_digest, binding.session_id),
        ).fetchone()
        if (
            metadata is None
            or metadata["body_type"] != "text"
            or not isinstance(metadata["body_bytes"], int)
            or not 1 <= metadata["body_bytes"] <= 1_048_576
        ):
            raise PolicyViolation("V4 close bounded durable context missing")
        row = db.execute(
            "select manifest_digest,session_id,checkpoint_digest,token_budget,token_count,"
            "body_json from context_manifest where manifest_digest=? and session_id=?",
            (manifest_digest, binding.session_id),
        ).fetchone()
        if row is None or len(str(row["body_json"]).encode("utf-8")) != metadata["body_bytes"]:
            raise PolicyViolation("V4 close manifest changed during bounded read")
        hydration_digest = revision["active_hydration_receipt_digest"]
        hydration = db.execute(
            "select * from hydration_receipt where receipt_digest=? and session_id=? "
            "and manifest_digest=?",
            (hydration_digest, binding.session_id, manifest_digest),
        ).fetchone()
        latest = db.execute(
            "select receipt_digest from hydration_receipt where session_id=? "
            "order by created_at desc,receipt_digest desc limit 1",
            (binding.session_id,),
        ).fetchone()
        source = db.execute(
            "select id,revision_ref from source_snapshot where id=?",
            (binding.source_snapshot_id,),
        ).fetchone()
        if (
            revision["active_manifest_digest"] != manifest_digest
            or hydration is None
            or latest is None
            or latest[0] != hydration_digest
            or source is None
            or source["id"] != source_snapshot.source_snapshot_id
            or source_snapshot.source_snapshot_id != binding.source_snapshot_id
            or source["revision_ref"] != source_snapshot.revision_ref
        ):
            raise PolicyViolation("V4 close active hydration/source binding drift")
        verified = verify_persisted_context_manifest(
            binding=binding,
            manifest_digest=manifest_digest,
            row_columns=dict(row),
            body_json=str(row["body_json"]),
            active_hydration_receipt=dict(hydration),
            db_source_revision=str(source["revision_ref"]),
            port_source_revision=source_snapshot.revision_ref,
        )
        fragments = dict(verified.fragments)
        for selected in verified.selected:
            if type(selected.provenance) is not CanonicalManifestProvenance:
                raise PolicyViolation("V4 close exact selected provenance required")
            try:
                resolved = self.source.resolve_fragment(
                    binding, source_snapshot, selected.provenance
                )
            except PolicyViolation:
                raise
            except Exception as exc:
                raise PolicyViolation("V4 close source fragment unavailable") from exc
            if (
                type(resolved) is not ResolvedManifestFragment
                or resolved.candidate_id != selected.candidate_id
                or resolved.text.encode("utf-8") != fragments[selected.candidate_id].encode("utf-8")
            ):
                raise PolicyViolation("V4 close resolved source fragment drift")
        self._assert_source_current(binding, source_snapshot)
        return verified

    @staticmethod
    def _open_hook_recovery(
        db: sqlite3.Connection, binding: ContinuityBinding
    ) -> list[sqlite3.Row]:
        rows = db.execute(
            "select c.recovery_case_id,c.attachment_id,c.session_id,"
            "c.process_generation_digest,v.process_generation_digest as current_generation,"
            "a.session_id as attachment_session from continuity_hook_recovery_case c "
            "join continuity_hook_attachment a on a.attachment_id=c.attachment_id "
            "join continuity_hook_attachment_revision v on v.attachment_id=a.attachment_id "
            "where c.session_id=? and a.session_id=? "
            "and v.revision_number=(select max(x.revision_number) "
            "from continuity_hook_attachment_revision x "
            "where x.attachment_id=a.attachment_id) "
            "and not exists(select 1 from continuity_hook_recovery_resolution r "
            "where r.recovery_case_id=c.recovery_case_id) order by c.recovery_case_id",
            (binding.session_id, binding.session_id),
        ).fetchall()
        return [cast(sqlite3.Row, row) for row in rows]

    @staticmethod
    def _no_pending(db: sqlite3.Connection, binding: ContinuityBinding) -> None:
        jobs = db.execute(
            "select 1 from local_job where json_extract(payload_json,'$.session_id')=? "
            "and state in ('ready','running','recovery-required') limit 1",
            (binding.session_id,),
        ).fetchone()
        effects = db.execute(
            "select 1 from local_effect_claim c join local_job j on j.id=c.job_id"
            " left join local_effect_receipt r on r.claim_id=c.id"
            " left join continuity_effect_binding b on b.claim_id=c.id"
            " where (b.session_id=? or json_extract(j.payload_json,'$.session_id')=?)"
            " and (r.id is null or r.status='unknown') and not exists("
            " select 1 from local_recovery_case rc join local_recovery_resolution rr"
            " on rr.recovery_case_id=rc.id where rc.effect_claim_id=c.id"
            " and rc.state='resolved' and rr.outcome in ('completed','failed')) limit 1",
            (binding.session_id, binding.session_id),
        ).fetchone()
        outbox = db.execute(
            "select 1 from local_outbox o left join local_outbox_delivery d on d.outbox_id=o.id"
            " left join local_outbox_receipt r on r.outbox_id=o.id"
            " join local_job j on j.id=o.job_id"
            " left join continuity_outbox_binding b on b.outbox_id=o.id"
            " where (b.session_id=? or json_extract(j.payload_json,'$.session_id')=?)"
            " and (d.outbox_id is null or d.state<>'delivered' or r.id is null"
            " or r.claim_id<>d.claim_id or r.fencing_token<>d.fencing_counter"
            " or not(r.status='delivered' or (r.status='unknown' and exists("
            " select 1 from local_recovery_case rc join local_recovery_resolution rr"
            " on rr.recovery_case_id=rc.id where rc.outbox_id=o.id"
            " and rc.state='resolved' and rr.outcome='delivered')))) limit 1",
            (binding.session_id, binding.session_id),
        ).fetchone()
        recovery = db.execute(
            "select 1 from local_recovery_case c join local_job j on j.id=c.job_id"
            " where c.state='open' and json_extract(j.payload_json,'$.session_id')=? limit 1",
            (binding.session_id,),
        ).fetchone()
        hook_recovery = SQLiteDormantV4CloseWriter._open_hook_recovery(db, binding)
        if jobs or effects or outbox or recovery or hook_recovery:
            raise PolicyViolation("V4 close pending work blocks freeze")

    @staticmethod
    def _no_pending_except_close(
        db: sqlite3.Connection,
        binding: ContinuityBinding,
        job_id: str,
    ) -> None:
        unrelated_job = db.execute(
            "select 1 from local_job where id<>? and "
            "json_extract(payload_json,'$.session_id')=? and "
            "state in ('ready','running','recovery-required') limit 1",
            (job_id, binding.session_id),
        ).fetchone()
        unrelated_recovery = db.execute(
            "select 1 from local_recovery_case c join local_job j on j.id=c.job_id "
            "where j.id<>? and c.state='open' and "
            "json_extract(j.payload_json,'$.session_id')=? limit 1",
            (job_id, binding.session_id),
        ).fetchone()
        unrelated_effect = db.execute(
            "select 1 from local_effect_claim c join local_job j on j.id=c.job_id "
            "left join local_effect_receipt r on r.claim_id=c.id "
            "left join continuity_effect_binding b on b.claim_id=c.id "
            "where j.id<>? and (b.session_id=? or "
            "json_extract(j.payload_json,'$.session_id')=?) "
            "and (r.id is null or r.status='unknown') and not exists("
            "select 1 from local_recovery_case rc join local_recovery_resolution rr "
            "on rr.recovery_case_id=rc.id where rc.effect_claim_id=c.id "
            "and rc.state='resolved' and rr.outcome in ('completed','failed')) limit 1",
            (job_id, binding.session_id, binding.session_id),
        ).fetchone()
        unrelated_outbox = db.execute(
            "select 1 from local_outbox o join local_job j on j.id=o.job_id "
            "left join local_outbox_delivery d on d.outbox_id=o.id "
            "left join local_outbox_receipt r on r.outbox_id=o.id "
            "left join continuity_outbox_binding b on b.outbox_id=o.id "
            "where j.id<>? and (b.session_id=? or "
            "json_extract(j.payload_json,'$.session_id')=?) and "
            "(d.outbox_id is null or d.state<>'delivered' or r.id is null "
            "or r.claim_id<>d.claim_id or r.fencing_token<>d.fencing_counter or "
            "not(r.status='delivered' or (r.status='unknown' and exists("
            "select 1 from local_recovery_case rc join local_recovery_resolution rr "
            "on rr.recovery_case_id=rc.id where rc.outbox_id=o.id "
            "and rc.state='resolved' and rr.outcome='delivered')))) limit 1",
            (job_id, binding.session_id, binding.session_id),
        ).fetchone()
        hook_recovery = SQLiteDormantV4CloseWriter._open_hook_recovery(db, binding)
        unexpected_hook_recovery = bool(hook_recovery)
        if (
            unrelated_job
            or unrelated_recovery
            or unrelated_effect
            or unrelated_outbox
            or unexpected_hook_recovery
        ):
            raise PolicyViolation("V4 close replay found unrelated pending work")

    @staticmethod
    def _capacity(db: sqlite3.Connection, required: int) -> None:
        maximum = db.execute(
            "select max_pending_outbox from local_runtime_config where singleton=1"
        ).fetchone()
        pending = db.execute(
            "select count(*) from local_outbox_delivery where state in "
            "('pending','claimed','recovery-required')"
        ).fetchone()[0]
        if maximum is None or int(pending) + required > int(maximum[0]):
            raise PolicyViolation("V4 close outbox capacity unavailable")

    @staticmethod
    def _summary_scope(
        manifest: VerifiedManifest,
        rows: list[sqlite3.Row],
        request: FrozenCloseWriteRequest,
    ) -> None:
        allowed_sources = {(item.source_ref, item.content_digest) for item in manifest.selected}
        allowed_evidence = {
            (f"context/{request.active_manifest_digest[7:]}", request.active_manifest_digest)
        }
        allowed_evidence.update(
            (f"event/{row['event_digest'][7:]}", str(row["event_digest"])) for row in rows
        )
        if (
            not set(request.summary.sources) <= allowed_sources
            or not set(request.summary.evidence) <= allowed_evidence
        ):
            raise PolicyViolation("V4 close summary provenance is not admitted")
        if request.candidates is not None:
            for category in ("memory", "decision", "skill", "failure"):
                for claim in getattr(request.candidates, category):
                    if not set(claim.source_refs) <= set(request.summary.sources) or not set(
                        claim.evidence_refs
                    ) <= set(request.summary.evidence):
                        raise PolicyViolation("V4 close candidate provenance is not admitted")

    @staticmethod
    def _event_values(
        request: FrozenCloseWriteRequest,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        first = ContinuityEvent(
            "CHECKPOINT_REQUESTED",
            derived_operation_key(request.operation_key, "checkpoint-requested"),
            request.observed_at,
            evidence_digests=(request.active_manifest_digest,),
        )
        first_body = {
            "session_id": request.binding.session_id,
            "binding_digest": request.binding.binding_digest,
            "sequence": request.expected_tail.sequence + 1,
            "previous_digest": request.expected_tail.event_digest,
            "event": first.body(),
        }
        first_digest = digest(first_body)
        second = ContinuityEvent(
            "PRE_CLOSE",
            derived_operation_key(request.operation_key, "pre-close"),
            request.observed_at,
            evidence_digests=(request.active_manifest_digest,),
        )
        second_body = {
            "session_id": request.binding.session_id,
            "binding_digest": request.binding.binding_digest,
            "sequence": request.expected_tail.sequence + 2,
            "previous_digest": first_digest,
            "event": second.body(),
        }
        return first_body, second_body

    @staticmethod
    def _close_body(
        request: FrozenCloseWriteRequest,
        *,
        checkpoint_digest: str,
        preclose_digest: str,
        project_slug: str,
    ) -> dict[str, Any]:
        common: dict[str, Any] = {
            "schema": "zekam-local-close/v1",
            "binding_digest": request.binding.binding_digest,
            "session_id": request.binding.session_id,
            "checkpoint_digest": checkpoint_digest,
            "manifest_digest": request.active_manifest_digest,
            "covered_sequence": request.expected_tail.sequence + 2,
            "covered_event_digest": preclose_digest,
            "project_slug": project_slug,
            "summary": request.summary.body(),
            "created_at": request.observed_at,
        }
        if request.candidates is not None:
            common.update(
                {
                    "schema": "zekam-local-close/v2",
                    "projection_recipe": "local-close-candidates/v2",
                    "candidate_recipe_digest": CANDIDATE_RECIPE_DIGEST,
                    "candidate_bundle": request.candidates.body(),
                }
            )
        if len(canonical_json(common).encode("utf-8")) > 65536:
            raise ValidationFailed("V4 close input byte bound exceeded")
        return common

    @staticmethod
    def _receipt_body(
        request: FrozenCloseWriteRequest,
        *,
        event_digest_value: str,
        event_kind: str,
        previous: str | None,
    ) -> dict[str, Any]:
        return {
            "attachment_revision_digest": request.expected_attachment_revision_digest,
            "binding_digest": request.binding.binding_digest,
            "created_at": request.observed_at,
            "event_digest": event_digest_value,
            "event_kind": event_kind,
            "expected_previous_event_digest": previous,
            "operation_key": derived_operation_key(
                request.operation_key,
                "checkpoint-requested" if event_kind == "CHECKPOINT_REQUESTED" else "pre-close",
            ),
            "session_id": request.binding.session_id,
        }

    @staticmethod
    def _checkpoint_body(
        request: FrozenCloseWriteRequest,
        *,
        preclose_digest: str,
        spool: FrozenSpoolSnapshot,
    ) -> dict[str, Any]:
        return {
            "session_id": request.binding.session_id,
            "binding_digest": request.binding.binding_digest,
            "covered_sequence": request.expected_tail.sequence + 2,
            "covered_event_digest": preclose_digest,
            "source_snapshot_id": request.binding.source_snapshot_id,
            "context_digest": request.active_manifest_digest,
            "spool_digest": digest(spool.entry_digests),
            "idempotency_key": request.checkpoint_idempotency_key,
            "grants_authority": False,
            "approval_inherited": False,
        }

    @staticmethod
    def _revision_body(
        predecessor: sqlite3.Row,
        *,
        revision_number: int,
        operation_key: str,
        state: str,
        created_at: str,
        checkpoint_digest: str,
        close_request_digest: str,
        pre_close_event_digest: str,
        close_receipt_digest: str | None = None,
        session_closed_event_digest: str | None = None,
    ) -> dict[str, Any]:
        return {
            "attachment_id": predecessor["attachment_id"],
            "revision_number": revision_number,
            "previous_revision_digest": predecessor["revision_digest"],
            "operation_key": operation_key,
            "state": state,
            "process_generation_digest": predecessor["process_generation_digest"],
            "active_manifest_digest": predecessor["active_manifest_digest"],
            "active_hydration_receipt_digest": predecessor["active_hydration_receipt_digest"],
            "checkpoint_digest": checkpoint_digest,
            "pre_compaction_event_digest": predecessor["pre_compaction_event_digest"],
            "post_compaction_event_digest": predecessor["post_compaction_event_digest"],
            "close_request_digest": close_request_digest,
            "pre_close_event_digest": pre_close_event_digest,
            "close_receipt_digest": close_receipt_digest,
            "session_closed_event_digest": session_closed_event_digest,
            "hook_recovery_case_id": predecessor["hook_recovery_case_id"],
            "hook_recovery_resolution_id": predecessor["hook_recovery_resolution_id"],
            "local_recovery_case_id": predecessor["local_recovery_case_id"],
            "local_recovery_resolution_id": predecessor["local_recovery_resolution_id"],
            "crash_recovered_event_digest": predecessor["crash_recovered_event_digest"],
            "crash_recovered_receipt_digest": predecessor["crash_recovered_receipt_digest"],
            "created_at": created_at,
        }

    @staticmethod
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

    @staticmethod
    def _insert_event(
        db: sqlite3.Connection,
        *,
        binding: ContinuityBinding,
        attachment_revision_digest: str,
        event_body: dict[str, Any],
        close_request_digest: str,
        created_at: str,
    ) -> str:
        event = event_body["event"]
        value = event_digest(
            binding,
            sequence=event_body["sequence"],
            previous_digest=event_body["previous_digest"],
            event_body=event,
        )
        receipt_body = {
            "attachment_revision_digest": attachment_revision_digest,
            "binding_digest": binding.binding_digest,
            "created_at": created_at,
            "event_digest": value,
            "event_kind": event["kind"],
            "expected_previous_event_digest": event_body["previous_digest"],
            "operation_key": event["idempotency_key"],
            "session_id": binding.session_id,
        }
        receipt = internal_receipt_digest(
            receipt_body,
            producer_kind="close_request_digest",
            producer_ref=close_request_digest,
        )
        db.execute(
            "insert into continuity_internal_event_receipt("
            "receipt_digest,event_digest,session_id,binding_digest,event_kind,operation_key,"
            "expected_previous_event_digest,close_request_digest,attachment_revision_digest,"
            "body_json,created_at) values(?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt,
                value,
                binding.session_id,
                binding.binding_digest,
                event["kind"],
                event["idempotency_key"],
                event_body["previous_digest"],
                close_request_digest,
                attachment_revision_digest,
                canonical_json(receipt_body),
                created_at,
            ),
        )
        event_id = str(new_uuid7())
        db.execute(
            "insert into session_event values(?,?,?,?,?)",
            (event_id, binding.session_id, event["kind"], value, created_at),
        )
        db.execute(
            "insert into session_event_detail values(?,?,?,?,?,?,null,?)",
            (
                event_id,
                binding.session_id,
                event_body["sequence"],
                event_body["previous_digest"],
                event["idempotency_key"],
                value,
                canonical_json(event_body),
            ),
        )
        return value

    @staticmethod
    def _insert_outbox(
        db: sqlite3.Connection,
        *,
        job_id: str,
        key: str,
        kind: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> str:
        outbox_id = str(new_uuid7())
        db.execute(
            "insert into local_outbox values(?,?,?,?,?,?,?)",
            (
                outbox_id,
                job_id,
                key,
                kind,
                canonical_json(payload),
                digest(payload),
                created_at,
            ),
        )
        db.execute(
            "insert into local_outbox_delivery(outbox_id,state,updated_at) values(?,'pending',?)",
            (outbox_id, created_at),
        )
        return outbox_id

    @staticmethod
    def _checkpoint_graph(
        db: sqlite3.Connection,
        binding: ContinuityBinding,
        body: dict[str, Any],
    ) -> sqlite3.Row:
        expected_keys = {
            "session_id",
            "binding_digest",
            "covered_sequence",
            "covered_event_digest",
            "source_snapshot_id",
            "context_digest",
            "spool_digest",
            "idempotency_key",
            "grants_authority",
            "approval_inherited",
        }
        checkpoint_digest = body.get("checkpoint_digest")
        if type(checkpoint_digest) is not str:
            raise PolicyViolation("V4 close checkpoint reference malformed")
        row = db.execute(
            "select * from continuity_checkpoint where checkpoint_digest=? and session_id=?",
            (checkpoint_digest, binding.session_id),
        ).fetchone()
        if row is None:
            raise PolicyViolation("V4 close checkpoint missing")
        try:
            checkpoint_body = json.loads(row["body_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PolicyViolation("V4 close checkpoint JSON malformed") from exc
        if (
            type(checkpoint_body) is not dict
            or set(checkpoint_body) != expected_keys
            or canonical_json(checkpoint_body) != row["body_json"]
            or digest(checkpoint_body) != checkpoint_digest
            or checkpoint_body["session_id"] != binding.session_id
            or checkpoint_body["binding_digest"] != binding.binding_digest
            or checkpoint_body["source_snapshot_id"] != binding.source_snapshot_id
            or checkpoint_body["context_digest"] != body.get("manifest_digest")
            or checkpoint_body["covered_sequence"] != body.get("covered_sequence")
            or checkpoint_body["covered_event_digest"] != body.get("covered_event_digest")
            or checkpoint_body["grants_authority"] is not False
            or checkpoint_body["approval_inherited"] is not False
            or row["idempotency_key"] != checkpoint_body["idempotency_key"]
            or row["covered_sequence"] != checkpoint_body["covered_sequence"]
            or row["covered_event_digest"] != checkpoint_body["covered_event_digest"]
            or row["source_snapshot_id"] != checkpoint_body["source_snapshot_id"]
            or row["context_digest"] != checkpoint_body["context_digest"]
            or row["spool_digest"] != checkpoint_body["spool_digest"]
            or row["created_at"] != body.get("created_at")
        ):
            raise PolicyViolation("V4 close checkpoint graph drift")
        return cast(sqlite3.Row, row)

    @staticmethod
    def _recovery_for_effect(
        db: sqlite3.Connection,
        claim: sqlite3.Row,
        receipt: sqlite3.Row | None,
    ) -> tuple[sqlite3.Row | None, sqlite3.Row | None]:
        cases = db.execute(
            "select * from local_recovery_case where effect_claim_id=?",
            (claim["id"],),
        ).fetchall()
        if len(cases) > 1:
            raise PolicyViolation("V4 close duplicate effect recovery")
        case = None if not cases else cases[0]
        resolution = None
        if case is not None:
            claim_created = SQLiteDormantV4CloseWriter._runtime_time(
                claim["claimed_at"], "effect claim"
            )
            case_created = SQLiteDormantV4CloseWriter._runtime_time(
                case["created_at"], "effect recovery case"
            )
            receipt_created = (
                None
                if receipt is None
                else SQLiteDormantV4CloseWriter._runtime_time(
                    receipt["created_at"], "effect receipt"
                )
            )
            resolutions = db.execute(
                "select * from local_recovery_resolution where recovery_case_id=?",
                (case["id"],),
            ).fetchall()
            if len(resolutions) > 1:
                raise PolicyViolation("V4 close duplicate effect resolution")
            resolution = None if not resolutions else resolutions[0]
            recipes = [
                {
                    "case_kind": "effect-unknown",
                    "claim_id": claim["id"],
                    "effect_digest": claim["effect_digest"],
                },
                {
                    "case_kind": "effect-unknown",
                    "claim_id": claim["id"],
                    "effect_digest": claim["effect_digest"],
                    "recovered_fence": claim["fencing_token"],
                },
            ]
            if receipt is not None:
                recipes.append(
                    {
                        "case_kind": "effect-unknown",
                        "claim_id": claim["id"],
                        "receipt_evidence": receipt["evidence_digest"],
                    }
                )
            if (
                case["job_id"] != claim["job_id"]
                or case["case_kind"] != "effect-unknown"
                or case["outbox_id"] is not None
                or case["evidence_digest"] not in {digest(item) for item in recipes}
                or (case["state"] == "open") != (resolution is None)
                or (case["state"] == "resolved") != (resolution is not None)
                or case_created < claim_created
                or (receipt_created is not None and case_created < receipt_created)
                or (
                    receipt is not None
                    and receipt["status"] == "unknown"
                    and case_created != receipt_created
                )
            ):
                raise PolicyViolation("V4 close effect recovery graph drift")
            if resolution is not None:
                resolution_created = SQLiteDormantV4CloseWriter._runtime_time(
                    resolution["created_at"], "effect recovery resolution"
                )
                if (
                    resolution["recovery_case_id"] != case["id"]
                    or resolution["outcome"] not in {"completed", "failed"}
                    or resolution["created_at"] != case["resolved_at"]
                    or resolution_created < case_created
                ):
                    raise PolicyViolation("V4 close effect resolution drift")
        return (
            None if case is None else cast(sqlite3.Row, case),
            None if resolution is None else cast(sqlite3.Row, resolution),
        )

    @staticmethod
    def _trusted_now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _runtime_time(value: object, label: str) -> datetime:
        if type(value) is not str:
            raise PolicyViolation(f"V4 close exact {label} timestamp required")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise PolicyViolation(f"V4 close {label} timestamp malformed") from exc
        if (
            parsed.tzinfo is None
            or parsed.utcoffset() != UTC.utcoffset(parsed)
            or parsed.astimezone(UTC).isoformat() != value
        ):
            raise PolicyViolation(f"V4 close canonical UTC {label} timestamp required")
        return parsed

    @staticmethod
    def _runtime_identity(value: object, label: str) -> str:
        if type(value) is not str or not value or value.strip() != value or len(value) > 512:
            raise PolicyViolation(f"V4 close exact bounded {label} required")
        return value

    @staticmethod
    def _delivery_state(
        db: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        trusted_now: datetime,
        expected_evidence: str | None = None,
        expected_pending_at: str | None = None,
    ) -> str:
        delivery = db.execute(
            "select * from local_outbox_delivery where outbox_id=?",
            (row["id"],),
        ).fetchone()
        receipts = db.execute(
            "select * from local_outbox_receipt where outbox_id=?",
            (row["id"],),
        ).fetchall()
        cases = db.execute(
            "select * from local_recovery_case where outbox_id=?",
            (row["id"],),
        ).fetchall()
        if delivery is None or len(receipts) > 1 or len(cases) > 1:
            raise PolicyViolation("V4 close outbox delivery graph incomplete")
        outbox_created = SQLiteDormantV4CloseWriter._runtime_time(
            row["created_at"], "outbox creation"
        )
        delivery_updated = SQLiteDormantV4CloseWriter._runtime_time(
            delivery["updated_at"], "delivery update"
        )
        if delivery_updated < outbox_created:
            raise PolicyViolation("V4 close delivery preceded outbox creation")
        state = str(delivery["state"])
        null_owner = all(
            delivery[name] is None
            for name in ("claim_id", "owner_id", "owner_pid", "owner_token", "expires_at")
        )
        if state == "pending":
            if (
                delivery["fencing_counter"] != 0
                or not null_owner
                or receipts
                or cases
                or (
                    expected_pending_at is not None
                    and delivery["updated_at"] != expected_pending_at
                )
            ):
                raise PolicyViolation("V4 close pending delivery drift")
            return state
        if (
            delivery["claim_id"] is None
            or type(delivery["owner_pid"]) is not int
            or delivery["owner_pid"] <= 0
            or delivery["owner_token"] is None
            or delivery["expires_at"] is None
            or type(delivery["fencing_counter"]) is not int
            or delivery["fencing_counter"] <= 0
        ):
            raise PolicyViolation("V4 close claimed delivery owner/fence drift")
        try:
            uuid_text(delivery["claim_id"], "V4 outbox claim")
            SQLiteDormantV4CloseWriter._runtime_identity(delivery["owner_id"], "delivery owner")
            SQLiteDormantV4CloseWriter._runtime_identity(
                delivery["owner_token"], "delivery owner token"
            )
        except ValidationFailed as exc:
            raise PolicyViolation("V4 close claimed delivery identity drift") from exc
        delivery_expiry = SQLiteDormantV4CloseWriter._runtime_time(
            delivery["expires_at"], "delivery expiry"
        )
        if state == "claimed":
            expired = delivery_expiry <= trusted_now
            if delivery_updated >= delivery_expiry:
                raise PolicyViolation("V4 close delivery timestamp order drift")
            if receipts or cases or expired:
                raise PolicyViolation("V4 close claimed delivery has terminal evidence")
            return state
        if len(receipts) != 1:
            raise PolicyViolation("V4 close terminal delivery receipt missing")
        receipt = receipts[0]
        try:
            uuid_text(receipt["id"], "V4 outbox receipt")
            receipt_created = SQLiteDormantV4CloseWriter._runtime_time(
                receipt["created_at"], "outbox receipt"
            )
        except ValidationFailed as exc:
            raise PolicyViolation("V4 close outbox receipt identity drift") from exc
        if (
            receipt["claim_id"] != delivery["claim_id"]
            or receipt["fencing_token"] != delivery["fencing_counter"]
            or (expected_evidence is not None and receipt["evidence_digest"] != expected_evidence)
            or receipt_created < outbox_created
            or receipt_created > delivery_updated
        ):
            raise PolicyViolation("V4 close terminal delivery receipt drift")
        status = str(receipt["status"])
        if status in {"delivered", "failed"}:
            if (
                cases
                or state != status
                or delivery["updated_at"] != receipt["created_at"]
                or receipt_created >= delivery_expiry
            ):
                raise PolicyViolation("V4 close direct delivery state drift")
            return state
        if status != "unknown" or len(cases) != 1:
            raise PolicyViolation("V4 close delivery receipt status drift")
        case = cases[0]
        resolutions = db.execute(
            "select * from local_recovery_resolution where recovery_case_id=?",
            (case["id"],),
        ).fetchall()
        direct_recipe = digest(
            {
                "case_kind": "outbox-delivery-unknown",
                "outbox_id": row["id"],
                "claim_id": receipt["claim_id"],
                "receipt_evidence": receipt["evidence_digest"],
            }
        )
        recovered_recipe = digest(
            {
                "case_kind": "outbox-delivery-unknown",
                "outbox_id": row["id"],
                "claim_id": receipt["claim_id"],
                "fencing_token": receipt["fencing_token"],
            }
        )
        if (
            case["job_id"] != row["job_id"]
            or case["case_kind"] != "outbox-delivery-unknown"
            or case["effect_claim_id"] is not None
            or case["evidence_digest"] not in {direct_recipe, recovered_recipe}
            or len(resolutions) > 1
            or (case["evidence_digest"] == direct_recipe and receipt_created >= delivery_expiry)
            or (
                case["evidence_digest"] == recovered_recipe
                and receipt["evidence_digest"] != recovered_recipe
            )
        ):
            raise PolicyViolation("V4 close delivery recovery graph drift")
        SQLiteDormantV4CloseWriter._runtime_time(case["created_at"], "outbox recovery case")
        if case["created_at"] != receipt["created_at"]:
            raise PolicyViolation("V4 close delivery recovery graph drift")
        if case["state"] == "open":
            if (
                resolutions
                or state != "recovery-required"
                or delivery["updated_at"] != receipt["created_at"]
                or case["created_at"] != receipt["created_at"]
            ):
                raise PolicyViolation("V4 close open delivery recovery drift")
            return state
        if case["state"] != "resolved" or len(resolutions) != 1:
            raise PolicyViolation("V4 close resolved delivery recovery missing")
        resolution = resolutions[0]
        if (
            resolution["recovery_case_id"] != case["id"]
            or resolution["outcome"] not in {"delivered", "failed"}
            or resolution["created_at"] != case["resolved_at"]
            or state != resolution["outcome"]
            or delivery["updated_at"] != resolution["created_at"]
        ):
            raise PolicyViolation("V4 close resolved delivery outcome drift")
        return state

    def _runtime_graph(
        self,
        db: sqlite3.Connection,
        binding: ContinuityBinding,
        frozen: FrozenClose,
        *,
        require_completed: bool,
    ) -> tuple[str, str]:
        trusted_now = self._trusted_now()
        job = db.execute("select * from local_job where id=?", (frozen.job_id,)).fetchone()
        if job is None:
            raise PolicyViolation("V4 close job missing")
        link = db.execute(
            "select * from continuity_outbox_binding where outbox_id=?",
            (frozen.outbox_id,),
        ).fetchone()
        job_body = _job_payload(binding, frozen.request_digest)
        job_created = self._runtime_time(job["created_at"], "job creation")
        job_available = self._runtime_time(job["available_at"], "job availability")
        job_updated = self._runtime_time(job["updated_at"], "job update")
        if (
            link is None
            or link["session_id"] != binding.session_id
            or link["job_id"] != frozen.job_id
            or link["purpose"] != "close"
            or link["input_digest"] != frozen.request_digest
            or link["close_request_digest"] != frozen.request_digest
            or job["idempotency_key"] != f"close:{frozen.request_digest}"
            or job["payload_json"] != canonical_json(job_body)
            or job["max_attempts"] != 1
            or job["created_at"] != frozen.input_body["created_at"]
            or job["available_at"] != frozen.input_body["created_at"]
            or job["timeout_at"] is not None
            or job_available != job_created
            or job_updated < job_created
            or type(job["attempt_count"]) is not int
            or not 0 <= job["attempt_count"] <= 1
            or type(job["fencing_counter"]) is not int
            or not 0 <= job["fencing_counter"] <= 1
        ):
            raise PolicyViolation("V4 close immutable job/binding graph drift")
        claims = db.execute(
            "select * from local_effect_claim where job_id=? order by id",
            (frozen.job_id,),
        ).fetchall()
        leases = db.execute(
            "select * from local_lease where job_id=? order by id",
            (frozen.job_id,),
        ).fetchall()
        if len(claims) > 1 or len(leases) > 1:
            raise PolicyViolation("V4 close compile claim/lease cardinality drift")
        claim = None if not claims else claims[0]
        receipt = None
        case = resolution = None
        claim_created: datetime | None = None
        receipt_created: datetime | None = None
        evidence = frozen.compile_evidence(binding)
        if claim is not None:
            try:
                uuid_text(claim["id"], "V4 compile claim")
                uuid_text(claim["lease_id"], "V4 compile claim lease")
                claim_created = self._runtime_time(claim["claimed_at"], "effect claim")
            except ValidationFailed as exc:
                raise PolicyViolation("V4 close compile claim identity drift") from exc
            receipts = db.execute(
                "select * from local_effect_receipt where claim_id=?", (claim["id"],)
            ).fetchall()
            if len(receipts) > 1:
                raise PolicyViolation("V4 close duplicate effect receipt")
            receipt = None if not receipts else receipts[0]
            bound = db.execute(
                "select * from continuity_effect_binding where claim_id=?", (claim["id"],)
            ).fetchone()
            if (
                claim["operation"] != COMPILE_OPERATION
                or claim["idempotency_key"] != frozen.effect_key
                or claim["effect_digest"] != evidence
                or bound is None
                or bound["session_id"] != binding.session_id
                or bound["job_id"] != frozen.job_id
                or bound["binding_digest"] != binding.binding_digest
            ):
                raise PolicyViolation("V4 close compile effect scope drift")
            case, resolution = self._recovery_for_effect(db, claim, receipt)
            if receipt is not None and receipt["evidence_digest"] != evidence:
                raise PolicyViolation("V4 close compile receipt evidence drift")
            if receipt is not None:
                try:
                    uuid_text(receipt["id"], "V4 effect receipt")
                    receipt_created = self._runtime_time(receipt["created_at"], "effect receipt")
                except ValidationFailed as exc:
                    raise PolicyViolation("V4 close effect receipt identity drift") from exc
                if receipt_created < claim_created:
                    raise PolicyViolation("V4 close effect receipt preceded claim")
            if (receipt is not None and receipt["status"] == "unknown" and case is None) or (
                receipt is not None
                and receipt["status"] in {"completed", "failed"}
                and case is not None
            ):
                raise PolicyViolation("V4 close compile receipt/recovery state drift")
        state = str(job["state"])
        fence = int(job["fencing_counter"])
        attempt = int(job["attempt_count"])
        if claim is not None:
            try:
                claim_lease_id = uuid_text(claim["lease_id"], "V4 compile claim lease")
            except (TypeError, ValidationFailed) as exc:
                raise PolicyViolation("V4 close compile claim lease identity drift") from exc
            if (
                not claim_lease_id
                or claim["fencing_token"] != fence
                or fence != attempt
                or attempt != 1
            ):
                raise PolicyViolation("V4 close compile claim fencing generation drift")
            if claim_created is None or claim_created < job_created:
                raise PolicyViolation("V4 close compile claim preceded job generation")
        if state == "ready":
            if (
                leases
                or claim is not None
                or job["terminal_evidence_digest"] is not None
                or attempt != 0
                or fence != 0
                or job_updated != job_created
            ):
                raise PolicyViolation("V4 close ready job graph drift")
        elif state == "running":
            if len(leases) != 1 or job["terminal_evidence_digest"] is not None:
                raise PolicyViolation("V4 close running job lease drift")
            lease = leases[0]
            try:
                uuid_text(lease["id"], "V4 runtime lease")
                self._runtime_identity(lease["owner_id"], "lease owner")
                self._runtime_identity(lease["owner_token"], "lease owner token")
            except ValidationFailed as exc:
                raise PolicyViolation("V4 close running lease identity drift") from exc
            lease_heartbeat = self._runtime_time(lease["heartbeat_at"], "lease heartbeat")
            lease_expiry = self._runtime_time(lease["expires_at"], "lease expiry")
            if (
                lease["fencing_token"] != fence
                or lease["fencing_token"] <= 0
                or type(lease["owner_pid"]) is not int
                or lease["owner_pid"] <= 0
                or attempt != 1
                or fence != 1
                or job_updated > lease_heartbeat
                or lease_heartbeat >= lease_expiry
                or lease_expiry <= trusted_now
                or (
                    claim is not None
                    and (claim["lease_id"] != lease["id"] or claim["fencing_token"] != fence)
                )
            ):
                raise PolicyViolation("V4 close running job owner/fence drift")
            if claim is not None and (claim_created is None or claim_created < job_updated):
                raise PolicyViolation("V4 close compile claim preceded running generation")
            if claim_created is not None and claim_created >= lease_expiry:
                raise PolicyViolation("V4 close compile claim exceeded running lease")
        else:
            if leases:
                raise PolicyViolation("V4 close terminal job retained lease")
            if state not in {"completed", "failed", "recovery-required", "quarantined"}:
                raise PolicyViolation("V4 close job state unsupported")
            if job["terminal_evidence_digest"] is None:
                raise PolicyViolation("V4 close terminal job evidence missing")
            if state == "quarantined" and (attempt != 1 or fence != 1):
                raise PolicyViolation("V4 close quarantine generation drift")
            if claim is not None and (
                claim_created is None
                or claim_created > job_updated
                or (receipt_created is not None and receipt_created > job_updated)
            ):
                raise PolicyViolation("V4 close terminal effect timestamp order drift")
            if resolution is not None and (
                self._runtime_time(resolution["created_at"], "effect recovery resolution")
                > job_updated
            ):
                raise PolicyViolation("V4 close effect resolution postdated reconciled job")

        outboxes = db.execute(
            "select * from local_outbox where job_id=? order by id", (frozen.job_id,)
        ).fetchall()
        initial = [
            row for row in outboxes if row["event_kind"] in {"job.enqueued", COMPILE_OPERATION}
        ]
        state_rows = [row for row in outboxes if row not in initial]
        if len(initial) != 2 or len(state_rows) > 2:
            raise PolicyViolation("V4 close work outbox cardinality drift")
        enqueued = [row for row in initial if row["event_kind"] == "job.enqueued"]
        compile_rows = [row for row in initial if row["id"] == frozen.outbox_id]
        if len(enqueued) != 1 or len(compile_rows) != 1:
            raise PolicyViolation("V4 close initial outbox identity drift")
        enqueue_body = {"job_id": frozen.job_id, "idempotency_key": job["idempotency_key"]}
        compile_body = _compile_outbox_payload(binding, frozen.request_digest)
        if (
            enqueued[0]["idempotency_key"] != f"job:{frozen.job_id}:enqueued"
            or enqueued[0]["payload_json"] != canonical_json(enqueue_body)
            or enqueued[0]["payload_digest"] != digest(enqueue_body)
            or compile_rows[0]["event_kind"] != COMPILE_OPERATION
            or compile_rows[0]["idempotency_key"] != f"close:{frozen.request_digest}:compile"
            or compile_rows[0]["payload_json"] != canonical_json(compile_body)
            or compile_rows[0]["payload_digest"] != digest(compile_body)
            or enqueued[0]["created_at"] != frozen.input_body["created_at"]
            or compile_rows[0]["created_at"] != frozen.input_body["created_at"]
        ):
            raise PolicyViolation("V4 close immutable initial outbox drift")
        delivery_states = {
            str(enqueued[0]["id"]): self._delivery_state(
                db,
                enqueued[0],
                trusted_now=trusted_now,
                expected_pending_at=str(frozen.input_body["created_at"]),
            ),
            str(compile_rows[0]["id"]): self._delivery_state(
                db,
                compile_rows[0],
                trusted_now=trusted_now,
                expected_evidence=frozen.delivery_evidence(binding),
                expected_pending_at=str(frozen.input_body["created_at"]),
            ),
        }
        if (
            delivery_states[str(compile_rows[0]["id"])]
            in {
                "claimed",
                "delivered",
                "failed",
                "recovery-required",
            }
            and state != "completed"
        ):
            raise PolicyViolation("V4 close compile delivery preceded completed job")
        compile_delivery = db.execute(
            "select updated_at from local_outbox_delivery where outbox_id=?",
            (frozen.outbox_id,),
        ).fetchone()
        if compile_delivery is None or (
            delivery_states[str(compile_rows[0]["id"])] != "pending"
            and self._runtime_time(compile_delivery["updated_at"], "compile delivery update")
            < job_updated
        ):
            raise PolicyViolation("V4 close compile delivery preceded completed job")

        expected_rows: list[tuple[str, str, dict[str, Any]]] = []
        recovered_fence: int | None = None
        base_was_recovered = False
        if state in {"completed", "failed", "recovery-required"}:
            direct_key = f"job:{frozen.job_id}:terminal"
            recovery_prefix = f"job:{frozen.job_id}:recovery:"
            direct = [row for row in state_rows if row["idempotency_key"] == direct_key]
            recovered = [
                row for row in state_rows if str(row["idempotency_key"]).startswith(recovery_prefix)
            ]
            reconciled = [
                row
                for row in state_rows
                if row["idempotency_key"] == f"job:{frozen.job_id}:reconciled"
            ]
            base = direct + recovered
            if len(base) != 1 or len(reconciled) > 1:
                raise PolicyViolation("V4 close terminal outbox history drift")
            base_row = base[0]
            if base_row in recovered:
                base_was_recovered = True
                parts = str(base_row["idempotency_key"]).split(":")
                if len(parts) != 5 or not parts[3].isdigit():
                    raise PolicyViolation("V4 close recovery outbox key malformed")
                recovered_fence = int(parts[3])
                base_state = parts[4]
                base_body = {
                    "job_id": frozen.job_id,
                    "state": base_state,
                    "fencing_token": recovered_fence,
                }
                if recovered_fence != fence:
                    raise PolicyViolation("V4 close recovery outbox fence drift")
            else:
                base_state = str(base_row["event_kind"])[4:]
                base_body = {"job_id": frozen.job_id, "state": base_state}
            if base_row["event_kind"] != f"job.{base_state}":
                raise PolicyViolation("V4 close terminal outbox kind drift")
            expected_rows.append((str(base_row["id"]), base_state, base_body))
            if reconciled:
                if base_state != "recovery-required" or state not in {"completed", "failed"}:
                    raise PolicyViolation("V4 close illegal reconciliation history")
                expected_rows.append(
                    (
                        str(reconciled[0]["id"]),
                        state,
                        {"job_id": frozen.job_id, "state": state, "reconciled": True},
                    )
                )
            elif base_state != state:
                raise PolicyViolation("V4 close terminal outbox/job state drift")
        elif state == "quarantined":
            if len(state_rows) != 1:
                raise PolicyViolation("V4 close quarantine outbox missing")
            expected_rows.append(
                (
                    str(state_rows[0]["id"]),
                    "quarantined",
                    {"job_id": frozen.job_id, "state": "quarantined"},
                )
            )
            if state_rows[0]["idempotency_key"] != f"job:{frozen.job_id}:quarantined":
                raise PolicyViolation("V4 close quarantine outbox key drift")
        elif state_rows:
            raise PolicyViolation("V4 close premature job-state outbox")
        if len(expected_rows) != len(state_rows):
            raise PolicyViolation("V4 close unexpected job-state outbox")
        for index, (outbox_id, expected_state, payload) in enumerate(expected_rows):
            row = next(item for item in state_rows if item["id"] == outbox_id)
            row_created = self._runtime_time(row["created_at"], "job-state outbox")
            if (
                row["event_kind"] != f"job.{expected_state}"
                or row["payload_json"] != canonical_json(payload)
                or row["payload_digest"] != digest(payload)
                or (index == len(expected_rows) - 1 and row["created_at"] != job["updated_at"])
                or (
                    index > 0
                    and row_created
                    < self._runtime_time(
                        next(
                            item for item in state_rows if item["id"] == expected_rows[index - 1][0]
                        )["created_at"],
                        "prior job-state outbox",
                    )
                )
            ):
                raise PolicyViolation("V4 close job-state outbox payload drift")
            row_delivery_state = self._delivery_state(
                db,
                row,
                trusted_now=trusted_now,
                expected_pending_at=str(row["created_at"]),
            )
            delivery_states[outbox_id] = row_delivery_state
            if row_delivery_state != "delivered" and require_completed:
                raise PolicyViolation("V4 finalizer job-state delivery incomplete")
        direct_effect = (
            receipt is not None
            and receipt["status"] in {"completed", "failed"}
            and receipt["evidence_digest"] == evidence
        )
        resolved_outcome = None if resolution is None else str(resolution["outcome"])
        if resolution is not None and resolution["evidence_digest"] != evidence:
            raise PolicyViolation("V4 close effect resolution evidence drift")
        effective_effect = (
            str(receipt["status"]) if direct_effect and receipt is not None else resolved_outcome
        )
        terminal_values = {
            evidence,
            digest([("completed", evidence)]),
            digest([("failed", evidence)]),
        }
        terminal_values.add(
            digest(
                [
                    (
                        None if receipt is None else receipt["status"],
                        None if receipt is None else receipt["evidence_digest"],
                        resolved_outcome,
                        None if resolution is None else resolution["evidence_digest"],
                    )
                ]
            )
        )
        no_claim_recovery_evidence: str | None = None
        if state == "failed" and claim is None and base_was_recovered:
            exhausted = int(job["attempt_count"]) >= int(job["max_attempts"])
            if not exhausted or attempt != 1 or fence != 1 or recovered_fence != fence:
                raise PolicyViolation("V4 close no-effect recovery generation drift")
            no_claim_recovery_evidence = digest(
                {
                    "reason": "attempts-exhausted",
                    "job_id": frozen.job_id,
                    "fencing_token": recovered_fence,
                }
            )
        if state == "completed" and (
            claim is None
            or effective_effect != "completed"
            or job["terminal_evidence_digest"] not in terminal_values
        ):
            raise PolicyViolation("V4 close completed job evidence drift")
        if state == "recovery-required":
            open_cases = db.execute(
                "select evidence_digest from local_recovery_case where job_id=? "
                "and state='open' order by id",
                (frozen.job_id,),
            ).fetchall()
            if (
                claim is None
                or case is None
                or case["state"] != "open"
                or resolution is not None
                or not open_cases
                or job["terminal_evidence_digest"]
                != digest([row["evidence_digest"] for row in open_cases])
            ):
                raise PolicyViolation("V4 close recovery-required job evidence drift")
        if state == "failed":
            if claim is None:
                if job["terminal_evidence_digest"] != no_claim_recovery_evidence:
                    raise PolicyViolation("V4 close failed no-effect recovery evidence drift")
            elif (
                effective_effect != "failed"
                or job["terminal_evidence_digest"] not in terminal_values
            ):
                raise PolicyViolation("V4 close failed job effect evidence drift")
        if state == "quarantined" and (
            claim is not None or job["terminal_evidence_digest"] is None
        ):
            raise PolicyViolation("V4 close quarantined job evidence drift")
        if require_completed and (
            state != "completed"
            or claim is None
            or effective_effect != "completed"
            or job["terminal_evidence_digest"] not in terminal_values
            or any(value != "delivered" for value in delivery_states.values())
            or any(
                self._delivery_state(
                    db,
                    next(row for row in state_rows if row["id"] == outbox_id),
                    trusted_now=trusted_now,
                )
                != "delivered"
                for outbox_id, _, _ in expected_rows
            )
        ):
            raise PolicyViolation(
                "V4 finalizer exact completed compile evidence/runtime graph required"
            )
        delivery_attention = any(
            value in {"failed", "recovery-required"} for value in delivery_states.values()
        )
        runtime_state = (
            "recovery-required"
            if delivery_attention or state in {"failed", "recovery-required", "quarantined"}
            else "pending"
        )
        return runtime_state, frozen.delivery_evidence(binding)

    def _verify_close_graph(
        self,
        db: sqlite3.Connection,
        *,
        binding: ContinuityBinding,
        source_snapshot: CurrentSourceSnapshot,
        frozen: FrozenClose,
        revision: sqlite3.Row,
        request: FrozenCloseWriteRequest | None,
        require_completed: bool,
    ) -> tuple[list[sqlite3.Row], str, str]:
        """Single strict graph verifier for fresh, replay, finalize and closed replay."""

        frozen.assert_integrity(binding)
        rows = self._events(db, binding)
        covered = int(frozen.input_body["covered_sequence"])
        if (
            covered < 1
            or covered > len(rows)
            or rows[covered - 1]["event_digest"] != frozen.input_body["covered_event_digest"]
            or rows[covered - 1]["event_kind"] != "PRE_CLOSE"
        ):
            raise PolicyViolation("V4 close covered event boundary drift")
        checkpoint = self._checkpoint_graph(db, binding, frozen.input_body)
        if (
            revision["close_request_digest"] != frozen.request_digest
            or revision["checkpoint_digest"] != checkpoint["checkpoint_digest"]
            or revision["pre_close_event_digest"] != frozen.input_body["covered_event_digest"]
            or revision["active_manifest_digest"] != frozen.input_body["manifest_digest"]
            or revision["active_hydration_receipt_digest"] is None
        ):
            raise PolicyViolation("V4 close current revision graph drift")
        manifest = self._manifest(
            db,
            binding,
            str(frozen.input_body["manifest_digest"]),
            revision,
            source_snapshot,
        )
        if request is not None:
            self._summary_scope(manifest, rows[:covered], request)
        runtime_state, delivery_evidence = self._runtime_graph(
            db,
            binding,
            frozen,
            require_completed=require_completed,
        )
        return rows, runtime_state, delivery_evidence

    def _load_frozen(
        self,
        db: sqlite3.Connection,
        request: FrozenCloseWriteRequest,
        *,
        source_snapshot: CurrentSourceSnapshot,
        expected_body: dict[str, Any],
        expected_checkpoint: dict[str, Any],
        expected_events: tuple[dict[str, Any], dict[str, Any]],
    ) -> FrozenClose:
        request_digest = digest(expected_body)
        close = db.execute(
            "select * from continuity_close_request where session_id=?",
            (request.binding.session_id,),
        ).fetchone()
        checkpoint = db.execute(
            "select * from continuity_checkpoint where checkpoint_digest=?",
            (expected_body["checkpoint_digest"],),
        ).fetchone()
        binding_row = db.execute(
            "select * from continuity_outbox_binding where close_request_digest=?",
            (request_digest,),
        ).fetchone()
        if close is None or checkpoint is None or binding_row is None:
            raise PolicyViolation("V4 close partial frozen graph")
        job = db.execute("select * from local_job where id=?", (binding_row["job_id"],)).fetchone()
        compile_outbox = db.execute(
            "select * from local_outbox where id=?", (binding_row["outbox_id"],)
        ).fetchone()
        enqueued = db.execute(
            "select * from local_outbox where job_id=? and event_kind='job.enqueued'",
            (binding_row["job_id"],),
        ).fetchall()
        revision = db.execute(
            "select * from continuity_hook_attachment_revision "
            "where close_request_digest=? and state='frozen' order by revision_number limit 1",
            (request_digest,),
        ).fetchone()
        if job is None or compile_outbox is None or len(enqueued) != 1 or revision is None:
            raise PolicyViolation("V4 close partial frozen work graph")
        revision = self._verified_revision(revision)
        job_payload = _job_payload(request.binding, request_digest)
        compile_payload = _compile_outbox_payload(request.binding, request_digest)
        enqueue_payload = {
            "job_id": job["id"],
            "idempotency_key": f"close:{request_digest}",
        }
        if (
            close["request_digest"] != request_digest
            or close["input_json"] != canonical_json(expected_body)
            or close["checkpoint_digest"] != expected_body["checkpoint_digest"]
            or close["covered_sequence"] != expected_body["covered_sequence"]
            or close["created_at"] != request.observed_at
            or checkpoint["body_json"] != canonical_json(expected_checkpoint)
            or checkpoint["created_at"] != request.observed_at
            or checkpoint["idempotency_key"] != request.checkpoint_idempotency_key
            or binding_row["session_id"] != request.binding.session_id
            or binding_row["purpose"] != "close"
            or binding_row["input_digest"] != request_digest
            or job["idempotency_key"] != f"close:{request_digest}"
            or job["payload_json"] != canonical_json(job_payload)
            or job["max_attempts"] != 1
            or job["created_at"] != request.observed_at
            or compile_outbox["idempotency_key"] != f"close:{request_digest}:compile"
            or compile_outbox["event_kind"] != COMPILE_OPERATION
            or compile_outbox["payload_json"] != canonical_json(compile_payload)
            or compile_outbox["payload_digest"] != digest(compile_payload)
            or enqueued[0]["idempotency_key"] != f"job:{job['id']}:enqueued"
            or enqueued[0]["payload_json"] != canonical_json(enqueue_payload)
            or enqueued[0]["payload_digest"] != digest(enqueue_payload)
            or revision["previous_revision_digest"] != request.expected_attachment_revision_digest
            or revision["process_generation_digest"] != request.expected_process_generation_digest
            or revision["active_manifest_digest"] != request.active_manifest_digest
            or revision["checkpoint_digest"] != expected_body["checkpoint_digest"]
            or revision["pre_close_event_digest"] != expected_body["covered_event_digest"]
            or revision["created_at"] != request.observed_at
        ):
            raise PolicyViolation("V4 close frozen graph replay drift")
        for event_body in expected_events:
            event = event_body["event"]
            value = digest(event_body)
            row = db.execute(
                "select d.body_json,r.created_at,r.attachment_revision_digest,"
                "r.close_request_digest from session_event_detail d "
                "join continuity_internal_event_receipt r on r.event_digest=d.event_digest "
                "where d.session_id=? and d.idempotency_key=?",
                (request.binding.session_id, event["idempotency_key"]),
            ).fetchone()
            if (
                row is None
                or row["body_json"] != canonical_json(event_body)
                or row["created_at"] != request.observed_at
                or row["attachment_revision_digest"] != request.expected_attachment_revision_digest
                or row["close_request_digest"] != request_digest
                or value
                != event_digest(
                    request.binding,
                    sequence=event_body["sequence"],
                    previous_digest=event_body["previous_digest"],
                    event_body=event,
                )
            ):
                raise PolicyViolation("V4 close frozen event replay drift")
        session = db.execute(
            "select status from session where id=?", (request.binding.session_id,)
        ).fetchone()
        result = FrozenClose(
            request_digest,
            str(job["id"]),
            str(compile_outbox["id"]),
            expected_body,
            "pending",
        )
        attachment = self._attachment(db, request.binding)
        current = self._current_revision(db, str(attachment["attachment_id"]))
        _, runtime_state, _ = self._verify_close_graph(
            db,
            binding=request.binding,
            source_snapshot=source_snapshot,
            frozen=result,
            revision=current,
            request=request,
            require_completed=session is not None and session[0] == "closed",
        )
        if session is not None and session[0] == "closed":
            receipt = db.execute(
                "select projections_json from close_receipt where request_digest=? "
                "and session_id=?",
                (request_digest, request.binding.session_id),
            ).fetchone()
            try:
                projection_body = None if receipt is None else json.loads(receipt[0])
                if type(projection_body) is not list:
                    raise PolicyViolation("V4 closed replay projection evidence missing")
                projection_snapshot = FrozenProjectionSnapshot(tuple(projection_body))
                projections = [dict(item) for item in projection_snapshot.evidence]
            except PolicyViolation:
                raise
            except Exception as exc:
                raise PolicyViolation("V4 closed replay projection evidence malformed") from exc
            delivery_evidence = self._terminal_work(db, request.binding, result)
            self._closed_graph(
                db,
                request.binding,
                result,
                current,
                projections,
                delivery_evidence,
            )
            runtime_state = "complete"
        return FrozenClose(
            result.request_digest,
            result.job_id,
            result.outbox_id,
            result.input_body,
            runtime_state,
        )

    def freeze_with_preclose(self, request: FrozenCloseWriteRequest) -> FrozenClose:
        if type(request) is not FrozenCloseWriteRequest:
            raise ValidationFailed("V4 close exact freeze request required")
        request.__post_init__()
        preflight = operational_schema.status(self.path)
        if preflight.schema_version != 4 or not preflight.schema_ok or not preflight.integrity_ok:
            raise ConfigurationError("V4 close corrected explicit schema required")
        with self._frozen_spool(request.binding) as spool_handle:
            spool = spool_handle.snapshot
            if type(spool) is not FrozenSpoolSnapshot:
                raise ValidationFailed("V4 close exact spool snapshot required")
            spool.__post_init__()
            source = self._source_snapshot(request.binding)
            db = self._connect()
            try:
                db.execute("begin immediate")
                self._schema(db)
                session = self._binding(db, request.binding)
                attachment = self._attachment(db, request.binding)
                rows = self._events(db, request.binding)
                prefix = rows[: request.expected_tail.sequence]
                if self._tail(prefix) != request.expected_tail:
                    raise ConcurrencyConflict("V4 close expected pre-freeze tail drift")
                self._spool_gate(
                    db, rows, spool, request.binding, allow_controls=session["status"] != "open"
                )
                first_body, second_body = self._event_values(request)
                second_digest = digest(second_body)
                checkpoint_body = self._checkpoint_body(
                    request, preclose_digest=second_digest, spool=spool
                )
                checkpoint_digest = digest(checkpoint_body)
                close_body = self._close_body(
                    request,
                    checkpoint_digest=checkpoint_digest,
                    preclose_digest=second_digest,
                    project_slug=str(session["slug"]),
                )
                request_digest = digest(close_body)
                prior = db.execute(
                    "select request_digest from continuity_close_request where session_id=?",
                    (request.binding.session_id,),
                ).fetchone()
                if prior is not None:
                    result = self._load_frozen(
                        db,
                        request,
                        source_snapshot=source,
                        expected_body=close_body,
                        expected_checkpoint=checkpoint_body,
                        expected_events=(first_body, second_body),
                    )
                    self._no_pending_except_close(db, request.binding, result.job_id)
                    self._assert_source_current(request.binding, source)
                    spool_handle.recheck()
                    db.commit()
                    return result
                if session["status"] != "open":
                    raise PolicyViolation("V4 close freeze requires open session")
                revision = self._current_revision(db, str(attachment["attachment_id"]))
                if (
                    revision["revision_digest"] != request.expected_attachment_revision_digest
                    or revision["process_generation_digest"]
                    != request.expected_process_generation_digest
                    or revision["state"] != "hydrated"
                    or revision["active_manifest_digest"] != request.active_manifest_digest
                    or revision["active_hydration_receipt_digest"] is None
                    or revision["checkpoint_digest"] is not None
                    or revision["close_request_digest"] is not None
                    or revision["hook_recovery_case_id"] is not None
                    or revision["local_recovery_case_id"] is not None
                ):
                    raise PolicyViolation("V4 close hydrated attachment admission drift")
                if self._tail(rows) != request.expected_tail:
                    raise ConcurrencyConflict("V4 close event tail changed")
                self._no_pending(db, request.binding)
                self._capacity(db, 2)
                manifest = self._manifest(
                    db,
                    request.binding,
                    request.active_manifest_digest,
                    revision,
                    source,
                )
                self._summary_scope(manifest, rows, request)
                self._assert_source_current(request.binding, source)
                spool_handle.recheck()

                self._insert_event(
                    db,
                    binding=request.binding,
                    attachment_revision_digest=request.expected_attachment_revision_digest,
                    event_body=first_body,
                    close_request_digest=request_digest,
                    created_at=request.observed_at,
                )
                self._insert_event(
                    db,
                    binding=request.binding,
                    attachment_revision_digest=request.expected_attachment_revision_digest,
                    event_body=second_body,
                    close_request_digest=request_digest,
                    created_at=request.observed_at,
                )
                db.execute(
                    "insert into continuity_checkpoint values(?,?,?,?,?,?,?,?,?,?)",
                    (
                        checkpoint_digest,
                        request.binding.session_id,
                        request.checkpoint_idempotency_key,
                        request.expected_tail.sequence + 2,
                        second_digest,
                        request.binding.source_snapshot_id,
                        request.active_manifest_digest,
                        digest(spool.entry_digests),
                        canonical_json(checkpoint_body),
                        request.observed_at,
                    ),
                )
                db.execute(
                    "insert into continuity_close_request values(?,?,?,?,?,?)",
                    (
                        request_digest,
                        request.binding.session_id,
                        checkpoint_digest,
                        request.expected_tail.sequence + 2,
                        canonical_json(close_body),
                        request.observed_at,
                    ),
                )
                job_id = str(new_uuid7())
                job_payload = _job_payload(request.binding, request_digest)
                db.execute(
                    "insert into local_job(id,idempotency_key,payload_json,state,attempt_count,"
                    "max_attempts,available_at,timeout_at,fencing_counter,terminal_evidence_digest,"
                    "created_at,updated_at) values(?,?,?,'ready',0,1,?,null,0,null,?,?)",
                    (
                        job_id,
                        f"close:{request_digest}",
                        canonical_json(job_payload),
                        request.observed_at,
                        request.observed_at,
                        request.observed_at,
                    ),
                )
                self._insert_outbox(
                    db,
                    job_id=job_id,
                    key=f"job:{job_id}:enqueued",
                    kind="job.enqueued",
                    payload={
                        "job_id": job_id,
                        "idempotency_key": f"close:{request_digest}",
                    },
                    created_at=request.observed_at,
                )
                compile_outbox = self._insert_outbox(
                    db,
                    job_id=job_id,
                    key=f"close:{request_digest}:compile",
                    kind=COMPILE_OPERATION,
                    payload=_compile_outbox_payload(request.binding, request_digest),
                    created_at=request.observed_at,
                )
                db.execute(
                    "insert into continuity_outbox_binding values(?,?,?,'close',?,?)",
                    (
                        compile_outbox,
                        request.binding.session_id,
                        job_id,
                        request_digest,
                        request_digest,
                    ),
                )
                revision_body = self._revision_body(
                    revision,
                    revision_number=int(revision["revision_number"]) + 1,
                    operation_key=derived_operation_key(request.operation_key, "freeze-revision"),
                    state="frozen",
                    created_at=request.observed_at,
                    checkpoint_digest=checkpoint_digest,
                    close_request_digest=request_digest,
                    pre_close_event_digest=second_digest,
                )
                frozen_revision = self._insert_revision(db, revision_body)
                changed = db.execute(
                    "update session set status='closing' where id=? and status='open' "
                    "and closed_at is null and close_receipt_digest is null",
                    (request.binding.session_id,),
                )
                if changed.rowcount != 1:
                    raise ConcurrencyConflict("V4 close session freeze state drift")
                if frozen_revision != revision_digest(revision_body):
                    raise PolicyViolation("V4 close frozen revision digest drift")
                result = self._load_frozen(
                    db,
                    request,
                    source_snapshot=source,
                    expected_body=close_body,
                    expected_checkpoint=checkpoint_body,
                    expected_events=(first_body, second_body),
                )
                self._assert_source_current(request.binding, source)
                spool_handle.recheck()
                db.commit()
                return result
            except sqlite3.OperationalError as exc:
                if db.in_transaction:
                    db.rollback()
                raise ConcurrencyConflict("V4 close SQLite writer unavailable") from exc
            except Exception:
                if db.in_transaction:
                    db.rollback()
                raise
            finally:
                db.close()

    def _frozen_for_finalize(
        self,
        db: sqlite3.Connection,
        request: FinalizeClosedWriteRequest,
        source_snapshot: CurrentSourceSnapshot,
    ) -> tuple[FrozenClose, sqlite3.Row, sqlite3.Row]:
        close = db.execute(
            "select * from continuity_close_request where request_digest=? and session_id=?",
            (request.request_digest, request.binding.session_id),
        ).fetchone()
        link = db.execute(
            "select * from continuity_outbox_binding where close_request_digest=? "
            "and session_id=? and purpose='close'",
            (request.request_digest, request.binding.session_id),
        ).fetchone()
        if close is None or link is None:
            raise PolicyViolation("V4 finalizer exact frozen request unavailable")
        try:
            body = json.loads(close["input_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise PolicyViolation("V4 finalizer malformed frozen request") from exc
        if (
            canonical_json(body) != close["input_json"]
            or digest(body) != request.request_digest
            or close["created_at"] != body.get("created_at")
            or close["checkpoint_digest"] != body.get("checkpoint_digest")
            or close["covered_sequence"] != body.get("covered_sequence")
            or link["input_digest"] != request.request_digest
        ):
            raise PolicyViolation("V4 finalizer frozen request integrity drift")
        result = FrozenClose(
            request.request_digest,
            str(link["job_id"]),
            str(link["outbox_id"]),
            body,
            "pending",
        )
        result.assert_integrity(request.binding)
        attachment = self._attachment(db, request.binding)
        revision = self._current_revision(db, str(attachment["attachment_id"]))
        self._verify_close_graph(
            db,
            binding=request.binding,
            source_snapshot=source_snapshot,
            frozen=result,
            revision=revision,
            request=None,
            require_completed=True,
        )
        return result, attachment, revision

    def _terminal_work(
        self, db: sqlite3.Connection, binding: ContinuityBinding, frozen: FrozenClose
    ) -> str:
        _, delivery_evidence = self._runtime_graph(db, binding, frozen, require_completed=True)
        return delivery_evidence

    @staticmethod
    def _projection_gate(
        db: sqlite3.Connection,
        binding: ContinuityBinding,
        frozen: FrozenClose,
        snapshot: FrozenProjectionSnapshot,
    ) -> list[dict[str, str]]:
        projections = [item.evidence() for item in frozen.projections(binding)]
        expected_sorted = tuple(sorted(projections, key=lambda item: item["portable_ref"]))
        if snapshot.evidence != expected_sorted:
            raise PolicyViolation("V4 finalizer locked projection evidence drift")
        for item in frozen.projections(binding):
            note = db.execute(
                "select * from knowledge_note where portable_ref=?",
                (item.manifest.portable_ref,),
            ).fetchone()
            if (
                note is None
                or note["realm_id"] != binding.realm_id
                or note["project_id"] != binding.project_id
                or note["owner_scope"] != item.manifest.owner_scope
                or note["classification"] != item.manifest.classification.value
                or note["content_digest"] != item.manifest.content_digest
                or note["materialized"] != 1
                or note["state"] != "inbox"
                or note["authorship"] != "generated"
            ):
                raise PolicyViolation("V4 finalizer projection manifest incomplete")
        return projections

    @staticmethod
    def _closed_graph(
        db: sqlite3.Connection,
        binding: ContinuityBinding,
        frozen: FrozenClose,
        revision: sqlite3.Row,
        projections: list[dict[str, str]],
        delivery_evidence: str,
    ) -> str:
        session = db.execute(
            "select status,closed_at,close_receipt_digest from session where id=?",
            (binding.session_id,),
        ).fetchone()
        receipt = db.execute(
            "select * from close_receipt where request_digest=? and session_id=?",
            (frozen.request_digest, binding.session_id),
        ).fetchone()
        expected = {
            "request_digest": frozen.request_digest,
            "session_id": binding.session_id,
            "checkpoint_digest": frozen.input_body["checkpoint_digest"],
            "manifest_digest": frozen.input_body["manifest_digest"],
            "outbox_id": frozen.outbox_id,
            "projections": projections,
            "delivery_evidence_digest": delivery_evidence,
        }
        value = digest(expected)
        if (
            session is None
            or receipt is None
            or session["status"] != "closed"
            or session["close_receipt_digest"] != value
            or receipt["receipt_digest"] != value
            or receipt["checkpoint_digest"] != expected["checkpoint_digest"]
            or receipt["manifest_digest"] != expected["manifest_digest"]
            or receipt["outbox_id"] != frozen.outbox_id
            or receipt["projections_json"] != canonical_json(projections)
            or receipt["created_at"] != session["closed_at"]
            or revision["state"] != "closed"
            or revision["close_receipt_digest"] != value
            or revision["session_closed_event_digest"] is None
            or revision["created_at"] != session["closed_at"]
        ):
            raise PolicyViolation("V4 close terminal receipt graph drift")
        closed_event = db.execute(
            "select e.event_kind,r.close_receipt_digest,r.attachment_revision_digest "
            "from session_event e join continuity_internal_event_receipt r "
            "on r.event_digest=e.event_digest where e.event_digest=? and e.session_id=?",
            (revision["session_closed_event_digest"], binding.session_id),
        ).fetchone()
        if (
            closed_event is None
            or closed_event["event_kind"] != "SESSION_CLOSED"
            or closed_event["close_receipt_digest"] != value
            or closed_event["attachment_revision_digest"] != revision["previous_revision_digest"]
        ):
            raise PolicyViolation("V4 close terminal event graph drift")
        return value

    @staticmethod
    def _insert_closed_event(
        db: sqlite3.Connection,
        request: FinalizeClosedWriteRequest,
        *,
        frozen_revision_digest: str,
        close_receipt_digest: str,
        tail: ContinuityTail,
    ) -> str:
        event = ContinuityEvent(
            "SESSION_CLOSED",
            derived_operation_key(request.operation_key, "session-closed"),
            request.finalized_at,
            evidence_digests=(close_receipt_digest,),
        )
        body = {
            "session_id": request.binding.session_id,
            "binding_digest": request.binding.binding_digest,
            "sequence": tail.sequence + 1,
            "previous_digest": tail.event_digest,
            "event": event.body(),
        }
        value = digest(body)
        receipt_body = {
            "attachment_revision_digest": frozen_revision_digest,
            "binding_digest": request.binding.binding_digest,
            "created_at": request.finalized_at,
            "event_digest": value,
            "event_kind": "SESSION_CLOSED",
            "expected_previous_event_digest": tail.event_digest,
            "operation_key": event.idempotency_key,
            "session_id": request.binding.session_id,
        }
        receipt = internal_receipt_digest(
            receipt_body,
            producer_kind="close_receipt_digest",
            producer_ref=close_receipt_digest,
        )
        db.execute(
            "insert into continuity_internal_event_receipt("
            "receipt_digest,event_digest,session_id,binding_digest,event_kind,operation_key,"
            "expected_previous_event_digest,close_receipt_digest,attachment_revision_digest,"
            "body_json,created_at) values(?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt,
                value,
                request.binding.session_id,
                request.binding.binding_digest,
                "SESSION_CLOSED",
                event.idempotency_key,
                tail.event_digest,
                close_receipt_digest,
                frozen_revision_digest,
                canonical_json(receipt_body),
                request.finalized_at,
            ),
        )
        event_id = str(new_uuid7())
        db.execute(
            "insert into session_event values(?,?,?,?,?)",
            (
                event_id,
                request.binding.session_id,
                "SESSION_CLOSED",
                value,
                request.finalized_at,
            ),
        )
        db.execute(
            "insert into session_event_detail values(?,?,?,?,?,?,null,?)",
            (
                event_id,
                request.binding.session_id,
                tail.sequence + 1,
                tail.event_digest,
                event.idempotency_key,
                value,
                canonical_json(body),
            ),
        )
        return value

    @staticmethod
    def _consume_recovery(
        db: sqlite3.Connection,
        request: FinalizeClosedWriteRequest,
        predecessor: sqlite3.Row,
        recovery: ExactResolvedRecovery,
        tail: ContinuityTail,
    ) -> sqlite3.Row:
        if (
            predecessor["state"] != "recovery-required"
            or predecessor["revision_digest"] != recovery.predecessor_revision_digest
            or predecessor["previous_revision_digest"] != request.expected_frozen_revision_digest
        ):
            raise PolicyViolation("V4 recovery predecessor revision drift")
        if recovery.recovery_case_kind == "hook":
            row = db.execute(
                "select c.attachment_id,c.session_id,c.process_generation_digest,"
                "r.resolution_id,r.outcome,r.evidence_digest,r.created_at "
                "from continuity_hook_recovery_case c "
                "join continuity_hook_recovery_resolution r "
                "on r.recovery_case_id=c.recovery_case_id "
                "where c.recovery_case_id=? and r.resolution_id=?",
                (recovery.recovery_case_id, recovery.recovery_resolution_id),
            ).fetchone()
            if (
                row is None
                or predecessor["hook_recovery_case_id"] != recovery.recovery_case_id
                or row["attachment_id"] != predecessor["attachment_id"]
                or row["session_id"] != request.binding.session_id
                or row["process_generation_digest"] != predecessor["process_generation_digest"]
            ):
                raise PolicyViolation("V4 hook recovery scope drift")
            producer_column = "hook_recovery_resolution_id"
            producer_values: tuple[str | None, str | None] = (
                recovery.recovery_resolution_id,
                None,
            )
        else:
            row = db.execute(
                "select c.job_id,r.id as resolution_id,r.outcome,r.evidence_digest,"
                "r.created_at from local_recovery_case c "
                "join local_recovery_resolution r on r.recovery_case_id=c.id "
                "where c.id=? and r.id=?",
                (recovery.recovery_case_id, recovery.recovery_resolution_id),
            ).fetchone()
            scoped = None
            if row is not None:
                scoped = db.execute(
                    "select 1 from local_job where id=? and "
                    "json_extract(payload_json,'$.session_id')=? and "
                    "json_extract(payload_json,'$.request_digest')=?",
                    (row["job_id"], request.binding.session_id, request.request_digest),
                ).fetchone()
            if (
                row is None
                or predecessor["local_recovery_case_id"] != recovery.recovery_case_id
                or scoped is None
            ):
                raise PolicyViolation("V4 local recovery scope drift")
            producer_column = "local_recovery_resolution_id"
            producer_values = (None, recovery.recovery_resolution_id)
        if (
            row["resolution_id"] != recovery.recovery_resolution_id
            or row["outcome"] != recovery.outcome
            or row["created_at"] != recovery.recovered_at
        ):
            raise PolicyViolation("V4 recovery immutable resolution drift")
        event = ContinuityEvent(
            "CRASH_RECOVERED",
            derived_operation_key(request.operation_key, "crash-recovered"),
            recovery.recovered_at,
            evidence_digests=(str(row["evidence_digest"]),),
        )
        event_body = {
            "session_id": request.binding.session_id,
            "binding_digest": request.binding.binding_digest,
            "sequence": tail.sequence + 1,
            "previous_digest": tail.event_digest,
            "event": event.body(),
        }
        event_value = digest(event_body)
        receipt_body = {
            "attachment_revision_digest": predecessor["revision_digest"],
            "binding_digest": request.binding.binding_digest,
            "created_at": recovery.recovered_at,
            "event_digest": event_value,
            "event_kind": "CRASH_RECOVERED",
            "expected_previous_event_digest": tail.event_digest,
            "operation_key": event.idempotency_key,
            "session_id": request.binding.session_id,
        }
        receipt_value = internal_receipt_digest(
            receipt_body,
            producer_kind=producer_column,
            producer_ref=recovery.recovery_resolution_id,
        )
        db.execute(
            "insert into continuity_internal_event_receipt("
            "receipt_digest,event_digest,session_id,binding_digest,event_kind,operation_key,"
            "expected_previous_event_digest,hook_recovery_resolution_id,"
            "local_recovery_resolution_id,attachment_revision_digest,body_json,created_at) "
            "values(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_value,
                event_value,
                request.binding.session_id,
                request.binding.binding_digest,
                "CRASH_RECOVERED",
                event.idempotency_key,
                tail.event_digest,
                *producer_values,
                predecessor["revision_digest"],
                canonical_json(receipt_body),
                recovery.recovered_at,
            ),
        )
        event_id = str(new_uuid7())
        db.execute(
            "insert into session_event values(?,?,?,?,?)",
            (
                event_id,
                request.binding.session_id,
                "CRASH_RECOVERED",
                event_value,
                recovery.recovered_at,
            ),
        )
        db.execute(
            "insert into session_event_detail values(?,?,?,?,?,?,null,?)",
            (
                event_id,
                request.binding.session_id,
                tail.sequence + 1,
                tail.event_digest,
                event.idempotency_key,
                event_value,
                canonical_json(event_body),
            ),
        )
        restored_body = SQLiteDormantV4CloseWriter._revision_body(
            predecessor,
            revision_number=int(predecessor["revision_number"]) + 1,
            operation_key=derived_operation_key(request.operation_key, "restored-revision"),
            state="frozen",
            created_at=recovery.recovered_at,
            checkpoint_digest=str(predecessor["checkpoint_digest"]),
            close_request_digest=request.request_digest,
            pre_close_event_digest=str(predecessor["pre_close_event_digest"]),
        )
        restored_body[producer_column] = recovery.recovery_resolution_id
        restored_body["crash_recovered_event_digest"] = event_value
        restored_body["crash_recovered_receipt_digest"] = receipt_value
        restored_digest = SQLiteDormantV4CloseWriter._insert_revision(db, restored_body)
        restored = db.execute(
            "select * from continuity_hook_attachment_revision where revision_digest=?",
            (restored_digest,),
        ).fetchone()
        return SQLiteDormantV4CloseWriter._verified_revision(restored)

    def _read_frozen(
        self,
        request: FinalizeClosedWriteRequest,
        source_snapshot: CurrentSourceSnapshot,
    ) -> FrozenClose:
        db = self._connect(read_only=True)
        try:
            db.execute("begin")
            self._schema(db)
            self._binding(db, request.binding)
            frozen, _, _ = self._frozen_for_finalize(db, request, source_snapshot)
            db.rollback()
            return frozen
        finally:
            db.close()

    def _read_frozen_preflight(self, request: FinalizeClosedWriteRequest) -> FrozenClose:
        """Read untrusted immutable projection coordinates before taking their lock."""

        db = self._connect(read_only=True)
        try:
            db.execute("begin")
            self._schema(db)
            self._binding(db, request.binding)
            close = db.execute(
                "select * from continuity_close_request where request_digest=? and session_id=?",
                (request.request_digest, request.binding.session_id),
            ).fetchone()
            link = db.execute(
                "select * from continuity_outbox_binding where close_request_digest=? "
                "and session_id=? and purpose='close'",
                (request.request_digest, request.binding.session_id),
            ).fetchone()
            if close is None or link is None:
                raise PolicyViolation("V4 finalizer untrusted frozen preflight missing")
            try:
                body = json.loads(close["input_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise PolicyViolation("V4 finalizer malformed frozen preflight") from exc
            if (
                type(body) is not dict
                or canonical_json(body) != close["input_json"]
                or digest(body) != request.request_digest
                or close["created_at"] != body.get("created_at")
                or close["checkpoint_digest"] != body.get("checkpoint_digest")
                or close["covered_sequence"] != body.get("covered_sequence")
                or link["input_digest"] != request.request_digest
            ):
                raise PolicyViolation("V4 finalizer untrusted frozen preflight drift")
            result = FrozenClose(
                request.request_digest,
                str(link["job_id"]),
                str(link["outbox_id"]),
                body,
                "pending",
            )
            result.assert_integrity(request.binding)
            db.rollback()
            return result
        finally:
            db.close()

    def finalize_with_session_closed(self, request: FinalizeClosedWriteRequest) -> str:
        if type(request) is not FinalizeClosedWriteRequest:
            raise ValidationFailed("V4 close exact finalize request required")
        request.__post_init__()
        preflight = operational_schema.status(self.path)
        if preflight.schema_version != 4 or not preflight.schema_ok or not preflight.integrity_ok:
            raise ConfigurationError("V4 close corrected explicit schema required")
        with self._frozen_spool(request.binding) as spool_handle:
            spool = spool_handle.snapshot
            if type(spool) is not FrozenSpoolSnapshot:
                raise ValidationFailed("V4 close exact spool snapshot required")
            frozen_preflight = self._read_frozen_preflight(request)
            with self._frozen_projections(frozen_preflight) as projection_handle:
                projection = projection_handle.snapshot
                if type(projection) is not FrozenProjectionSnapshot:
                    raise ValidationFailed("V4 close exact projection snapshot required")
                source = self._source_snapshot(request.binding)
                db = self._connect()
                try:
                    db.execute("begin immediate")
                    self._schema(db)
                    session = self._binding(db, request.binding)
                    frozen, _, revision = self._frozen_for_finalize(db, request, source)
                    self._no_pending_except_close(db, request.binding, frozen.job_id)
                    if revision["state"] == "closed" and session["status"] == "closed":
                        predecessor = db.execute(
                            "select * from continuity_hook_attachment_revision "
                            "where revision_digest=?",
                            (revision["previous_revision_digest"],),
                        ).fetchone()
                        predecessor = self._verified_revision(predecessor)
                        if predecessor is None or predecessor["state"] != "frozen":
                            raise PolicyViolation("V4 closed replay frozen predecessor missing")
                        if request.recovery is None:
                            if (
                                predecessor["revision_digest"]
                                != request.expected_frozen_revision_digest
                            ):
                                raise PolicyViolation("V4 closed replay predecessor drift")
                        else:
                            recovery_predecessor = db.execute(
                                "select * from continuity_hook_attachment_revision "
                                "where revision_digest=?",
                                (predecessor["previous_revision_digest"],),
                            ).fetchone()
                            recovery_predecessor = self._verified_revision(recovery_predecessor)
                            producer_column = (
                                "hook_recovery_resolution_id"
                                if request.recovery.recovery_case_kind == "hook"
                                else "local_recovery_resolution_id"
                            )
                            if (
                                recovery_predecessor is None
                                or recovery_predecessor["state"] != "recovery-required"
                                or recovery_predecessor["revision_digest"]
                                != request.recovery.predecessor_revision_digest
                                or recovery_predecessor["previous_revision_digest"]
                                != request.expected_frozen_revision_digest
                                or predecessor[producer_column]
                                != request.recovery.recovery_resolution_id
                                or predecessor["crash_recovered_event_digest"] is None
                                or predecessor["crash_recovered_receipt_digest"] is None
                            ):
                                raise PolicyViolation("V4 closed replay recovery chain drift")
                            if request.recovery.recovery_case_kind == "hook":
                                resolution = db.execute(
                                    "select c.attachment_id,c.session_id,"
                                    "c.process_generation_digest,"
                                    "r.resolution_id,r.outcome,r.created_at "
                                    "from continuity_hook_recovery_case c "
                                    "join continuity_hook_recovery_resolution r "
                                    "on r.recovery_case_id=c.recovery_case_id "
                                    "where c.recovery_case_id=? and r.resolution_id=?",
                                    (
                                        request.recovery.recovery_case_id,
                                        request.recovery.recovery_resolution_id,
                                    ),
                                ).fetchone()
                                resolution_scope = (
                                    resolution is not None
                                    and resolution["attachment_id"] == revision["attachment_id"]
                                    and resolution["session_id"] == request.binding.session_id
                                    and resolution["process_generation_digest"]
                                    == revision["process_generation_digest"]
                                )
                            else:
                                resolution = db.execute(
                                    "select c.job_id,r.id as resolution_id,r.outcome,r.created_at "
                                    "from local_recovery_case c join local_recovery_resolution r "
                                    "on r.recovery_case_id=c.id where c.id=? and r.id=?",
                                    (
                                        request.recovery.recovery_case_id,
                                        request.recovery.recovery_resolution_id,
                                    ),
                                ).fetchone()
                                resolution_scope = (
                                    resolution is not None and resolution["job_id"] == frozen.job_id
                                )
                            if (
                                resolution is None
                                or not resolution_scope
                                or resolution["outcome"] != request.recovery.outcome
                                or resolution["created_at"] != request.recovery.recovered_at
                            ):
                                raise PolicyViolation("V4 closed replay recovery resolution drift")
                        delivery_evidence = self._terminal_work(db, request.binding, frozen)
                        projections = self._projection_gate(db, request.binding, frozen, projection)
                        terminal = self._closed_graph(
                            db,
                            request.binding,
                            frozen,
                            revision,
                            projections,
                            delivery_evidence,
                        )
                        self._assert_source_current(request.binding, source)
                        spool_handle.recheck()
                        projection_handle.recheck()
                        db.commit()
                        return terminal
                    if (
                        session["status"] != "closing"
                        or revision["close_request_digest"] != request.request_digest
                    ):
                        raise PolicyViolation("V4 finalizer frozen revision/session drift")
                    rows = self._events(db, request.binding)
                    self._spool_gate(db, rows, spool, request.binding, allow_controls=True)
                    if request.recovery is None:
                        if (
                            revision["state"] != "frozen"
                            or revision["revision_digest"]
                            != request.expected_frozen_revision_digest
                        ):
                            raise PolicyViolation("V4 finalizer frozen revision/session drift")
                    else:
                        revision = self._consume_recovery(
                            db,
                            request,
                            revision,
                            request.recovery,
                            self._tail(rows),
                        )
                        rows = self._events(db, request.binding)
                    delivery_evidence = self._terminal_work(db, request.binding, frozen)
                    projections = self._projection_gate(db, request.binding, frozen, projection)
                    receipt_body = {
                        "request_digest": request.request_digest,
                        "session_id": request.binding.session_id,
                        "checkpoint_digest": frozen.input_body["checkpoint_digest"],
                        "manifest_digest": frozen.input_body["manifest_digest"],
                        "outbox_id": frozen.outbox_id,
                        "projections": projections,
                        "delivery_evidence_digest": delivery_evidence,
                    }
                    close_receipt = digest(receipt_body)
                    if (
                        db.execute(
                            "select 1 from close_receipt where request_digest=?",
                            (request.request_digest,),
                        ).fetchone()
                        is not None
                    ):
                        raise PolicyViolation("V4 finalizer partial terminal graph")
                    self._assert_source_current(request.binding, source)
                    spool_handle.recheck()
                    projection_handle.recheck()
                    db.execute(
                        "insert into close_receipt values(?,?,?,?,?,?,?,?)",
                        (
                            close_receipt,
                            request.request_digest,
                            request.binding.session_id,
                            frozen.input_body["checkpoint_digest"],
                            frozen.input_body["manifest_digest"],
                            frozen.outbox_id,
                            canonical_json(projections),
                            request.finalized_at,
                        ),
                    )
                    tail = self._tail(rows)
                    closed_event = self._insert_closed_event(
                        db,
                        request,
                        frozen_revision_digest=str(revision["revision_digest"]),
                        close_receipt_digest=close_receipt,
                        tail=tail,
                    )
                    revision_body = self._revision_body(
                        revision,
                        revision_number=int(revision["revision_number"]) + 1,
                        operation_key=derived_operation_key(
                            request.operation_key, "closed-revision"
                        ),
                        state="closed",
                        created_at=request.finalized_at,
                        checkpoint_digest=str(revision["checkpoint_digest"]),
                        close_request_digest=request.request_digest,
                        pre_close_event_digest=str(revision["pre_close_event_digest"]),
                        close_receipt_digest=close_receipt,
                        session_closed_event_digest=closed_event,
                    )
                    self._insert_revision(db, revision_body)
                    changed = db.execute(
                        "update session set status='closed',closed_at=?,close_receipt_digest=? "
                        "where id=? and status='closing'",
                        (
                            request.finalized_at,
                            close_receipt,
                            request.binding.session_id,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise ConcurrencyConflict("V4 finalizer session state drift")
                    closed_revision = self._current_revision(db, str(revision["attachment_id"]))
                    self._verify_close_graph(
                        db,
                        binding=request.binding,
                        source_snapshot=source,
                        frozen=frozen,
                        revision=closed_revision,
                        request=None,
                        require_completed=True,
                    )
                    self._closed_graph(
                        db,
                        request.binding,
                        frozen,
                        closed_revision,
                        projections,
                        delivery_evidence,
                    )
                    self._assert_source_current(request.binding, source)
                    spool_handle.recheck()
                    projection_handle.recheck()
                    db.commit()
                    return close_receipt
                except sqlite3.OperationalError as exc:
                    if db.in_transaction:
                        db.rollback()
                    raise ConcurrencyConflict("V4 close SQLite writer unavailable") from exc
                except Exception:
                    if db.in_transaction:
                        db.rollback()
                    raise
                finally:
                    db.close()
