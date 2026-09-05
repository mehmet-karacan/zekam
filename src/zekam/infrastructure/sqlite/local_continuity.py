"""Transactional local continuity adapter; no provider or legacy-store dependency."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from zekam.application.context_ranking import count_context_tokens
from zekam.application.local_continuity import (
    ContinuityBinding,
    ContinuityEvent,
    ContinuitySourceResolver,
    ContinuityTail,
    LocalContext,
    bounded_int,
    digest_text,
    logical,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import (
    ConcurrencyConflict,
    ConfigurationError,
    PolicyViolation,
    ValidationFailed,
)
from zekam.domain.identifiers import new_uuid7
from zekam.infrastructure.sqlite.operational_schema import SCHEMA_VERSION, status


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


class SQLiteContinuityStore:
    def __init__(
        self, path: Path, *, source_resolver: ContinuitySourceResolver | None = None
    ) -> None:
        current = status(path)
        if (
            not current.integrity_ok
            or not current.schema_ok
            or current.schema_version != SCHEMA_VERSION
        ):
            raise ConfigurationError("Continuity requires admitted current local schema")
        self.path = path
        self.source_resolver = source_resolver

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("pragma foreign_keys=on")
        try:
            connection.execute("begin immediate")
            yield connection
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise ValidationFailed("Continuity constraint rejected mutation") from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _assert_binding(connection: sqlite3.Connection, binding: ContinuityBinding) -> sqlite3.Row:
        if not isinstance(binding, ContinuityBinding):
            raise ValidationFailed("Typed continuity binding required")
        binding.__post_init__()
        row: sqlite3.Row | None = connection.execute(
            "select b.*,s.status,s.project_id as actual_project,s.work_item_id as actual_work,"
            "s.client_id as actual_client,s.device_id as actual_device"
            " from continuity_session_binding b"
            " join session s on s.id=b.session_id"
            " where b.session_id=?",
            (binding.session_id,),
        ).fetchone()
        if row is None or row["binding_digest"] != binding.binding_digest:
            raise PolicyViolation("Continuity exact binding missing or drifted")
        if any(row[name] != value for name, value in asdict(binding).items()):
            raise PolicyViolation("Continuity stored binding payload drift")
        if (
            row["actual_project"],
            row["actual_work"],
            row["actual_client"],
            row["actual_device"],
        ) != (binding.project_id, binding.work_item_id, binding.client_id, binding.device_id):
            raise PolicyViolation("Continuity canonical session owner drift")
        project = connection.execute(
            "select 1 from project p join project_knowledge_realm r on r.project_id=p.id"
            " where p.id=? and p.status='active' and r.realm_id=?",
            (binding.project_id, binding.realm_id),
        ).fetchone()
        if project is None:
            raise PolicyViolation("Continuity current project/realm binding drift")
        source = connection.execute(
            "select ss.* from source_snapshot ss"
            " join source_binding sb on sb.id=ss.source_binding_id"
            " where ss.id=? and sb.project_id=? and sb.active=1",
            (binding.source_snapshot_id, binding.project_id),
        ).fetchone()
        if source is None:
            raise PolicyViolation("Continuity source unavailable")
        latest = connection.execute(
            "select id from source_snapshot where source_binding_id=?"
            " order by captured_at desc,id desc limit 1",
            (source["source_binding_id"],),
        ).fetchone()
        if latest[0] != binding.source_snapshot_id:
            raise PolicyViolation("Continuity source revision stale")
        config = connection.execute(
            "select id from config_revision where active=1 and task_digest=? and config_digest=?",
            (binding.task_digest, binding.policy_digest),
        ).fetchone()
        if config is None:
            raise PolicyViolation("Continuity task/policy revision stale")
        if binding.run_id is not None:
            run = connection.execute(
                "select r.id from run r join work_item w on w.id=r.work_item_id"
                " where r.id=? and r.work_item_id=? and w.project_id=?"
                " and r.source_snapshot_id=? and r.plan_digest=? and r.config_revision_id=?",
                (
                    binding.run_id,
                    binding.work_item_id,
                    binding.project_id,
                    binding.source_snapshot_id,
                    binding.plan_digest,
                    config[0],
                ),
            ).fetchone()
            if run is None:
                raise PolicyViolation("Continuity run owner/plan revision drift")
        return row

    def bind_session(self, binding: ContinuityBinding) -> str:
        if not isinstance(binding, ContinuityBinding):
            raise ValidationFailed("Typed continuity binding required")
        binding.__post_init__()
        with self._transaction() as connection:
            existing = connection.execute(
                "select session_id from continuity_session_binding where session_id=?",
                (binding.session_id,),
            ).fetchone()
            if existing is not None:
                self._assert_binding(connection, binding)
                return binding.binding_digest
            now = _now()
            connection.execute(
                "insert into session"
                "(id,client_id,device_id,project_id,work_item_id,status,opened_at)"
                " values(?,?,?,?,?,'open',?)",
                (
                    binding.session_id,
                    binding.client_id,
                    binding.device_id,
                    binding.project_id,
                    binding.work_item_id,
                    now,
                ),
            )
            values = asdict(binding) | {"binding_digest": binding.binding_digest, "created_at": now}
            columns = ",".join(values)
            placeholders = ",".join("?" for _ in values)
            connection.execute(
                f"insert into continuity_session_binding({columns}) values({placeholders})",
                tuple(values.values()),
            )
            self._assert_binding(connection, binding)
        return binding.binding_digest

    @staticmethod
    def _events(connection: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
        rows = connection.execute(
            "select d.*,e.event_kind,e.event_digest as canonical_event_digest,"
            "e.session_id as canonical_session,b.binding_digest as actual_binding"
            " from session_event_detail d"
            " join session_event e on e.id=d.event_id"
            " join continuity_session_binding b on b.session_id=d.session_id"
            " where d.session_id=? order by d.sequence",
            (session_id,),
        ).fetchall()
        count = connection.execute(
            "select count(*) from session_event where session_id=?", (session_id,)
        ).fetchone()[0]
        if count != len(rows):
            raise PolicyViolation("Continuity event detail gap; recovery required")
        previous = None
        for sequence, row in enumerate(rows, 1):
            try:
                body = json.loads(row["body_json"])
                event = body["event"]
                if not isinstance(body, dict) or not isinstance(event, dict):
                    raise ValueError("Event object required")
                parsed = ContinuityEvent(
                    event["kind"],
                    event["idempotency_key"],
                    event["occurred_at"],
                    tuple(event["source_refs"]),
                    tuple(event["evidence_digests"]),
                    event["spool_digest"],
                )
                if canonical_json(parsed.body()) != canonical_json(event):
                    raise ValueError("Exact event fields required")
                if (
                    not {"session_id", "binding_digest", "sequence", "previous_digest"}
                    <= body.keys()
                ):
                    raise ValueError("Event envelope incomplete")
            except (KeyError, TypeError, ValueError, ValidationFailed) as exc:
                raise PolicyViolation("Continuity malformed durable event evidence") from exc
            if (
                row["sequence"] != sequence
                or row["previous_digest"] != previous
                or body["previous_digest"] != previous
                or body["sequence"] != sequence
                or body["session_id"] != session_id
                or row["canonical_session"] != session_id
                or body["binding_digest"] != row["actual_binding"]
                or body["event"]["idempotency_key"] != row["idempotency_key"]
                or body["event"]["spool_digest"] != row["spool_digest"]
                or body["event"]["kind"] != row["event_kind"]
                or digest(body) != row["event_digest"]
                or row["event_digest"] != row["canonical_event_digest"]
            ):
                raise PolicyViolation("Continuity event integrity/chain drift")
            previous = row["event_digest"]
        return rows

    @staticmethod
    def _tail(rows: list[sqlite3.Row]) -> ContinuityTail:
        return ContinuityTail(len(rows), rows[-1]["event_digest"] if rows else None)

    def tail(self, binding: ContinuityBinding) -> ContinuityTail:
        with self._transaction() as connection:
            self._assert_binding(connection, binding)
            return self._tail(self._events(connection, binding.session_id))

    def source_content_digest(self, binding: ContinuityBinding) -> str:
        # Source probes may run inside an existing close writer transaction.
        # This observation must not claim a second writer lock or mutate state.
        with closing(
            sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        ) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("pragma query_only=on")
            connection.execute("begin")
            self._assert_binding(connection, binding)
            row = connection.execute(
                "select content_digest from source_snapshot where id=?",
                (binding.source_snapshot_id,),
            ).fetchone()
            return str(row[0])

    def get_binding(self, session_id: str) -> ContinuityBinding:
        logical(session_id, "Session id")
        with self._transaction() as connection:
            row = connection.execute(
                "select * from continuity_session_binding where session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise PolicyViolation("Continuity session binding not found")
            return ContinuityBinding(
                **{field.name: row[field.name] for field in fields(ContinuityBinding)}
            )

    def spool_digests(self, binding: ContinuityBinding) -> tuple[str, ...]:
        with self._transaction() as connection:
            self._assert_binding(connection, binding)
            return tuple(
                row["spool_digest"]
                for row in self._events(connection, binding.session_id)
                if row["spool_digest"] is not None
            )

    def inspect(self, binding: ContinuityBinding) -> dict[str, Any]:
        connection = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("begin")
            current = self._assert_binding(connection, binding)
            rows = self._events(connection, binding.session_id)
            issues = []
            try:
                self._no_pending(connection, binding)
            except PolicyViolation:
                issues.append("pending-effect-outbox-or-recovery")
            checkpoint = connection.execute(
                "select checkpoint_digest,covered_sequence from continuity_checkpoint"
                " where session_id=? order by covered_sequence desc,created_at desc limit 1",
                (binding.session_id,),
            ).fetchone()
            request = connection.execute(
                "select c.request_digest,r.receipt_digest from continuity_close_request c"
                " left join close_receipt r on r.request_digest=c.request_digest"
                " where c.session_id=?",
                (binding.session_id,),
            ).fetchone()
            if request is not None and request["receipt_digest"] is None:
                issues.append("pending-close")
            if checkpoint is None:
                issues.append("missing-checkpoint")
            elif checkpoint["covered_sequence"] < len(rows):
                issues.append("uncovered-session-delta")
            return {
                "schema": "zekam-local-continuity-health/v1",
                "session_id": binding.session_id,
                "session_state": current["status"],
                "event_count": len(rows),
                "checkpoint_digest": None
                if checkpoint is None
                else checkpoint["checkpoint_digest"],
                "close_receipt_digest": None if request is None else request["receipt_digest"],
                "close_request_digest": None if request is None else request["request_digest"],
                "issues": issues,
                "state": "attention-required" if issues else "healthy",
                "grants_authority": False,
                "read_only": True,
            }
        finally:
            connection.close()

    def append_event(
        self, binding: ContinuityBinding, event: ContinuityEvent, *, expected_tail: ContinuityTail
    ) -> ContinuityTail:
        if not isinstance(event, ContinuityEvent) or not isinstance(expected_tail, ContinuityTail):
            raise ValidationFailed("Typed continuity event and tail required")
        event.__post_init__()
        expected_tail.__post_init__()
        with self._transaction() as connection:
            current = self._assert_binding(connection, binding)
            rows = self._events(connection, binding.session_id)
            existing = next(
                (row for row in rows if row["idempotency_key"] == event.idempotency_key), None
            )
            if existing is not None:
                original = json.loads(existing["body_json"])
                if canonical_json(original["event"]) != canonical_json(event.body()):
                    raise PolicyViolation("Continuity event replay payload drift")
                return ContinuityTail(existing["sequence"], existing["event_digest"])
            if current["status"] != "open":
                raise PolicyViolation("Continuity session delta frozen")
            if self._tail(rows) != expected_tail:
                raise ConcurrencyConflict("Continuity event expected sequence drift")
            body = {
                "session_id": binding.session_id,
                "binding_digest": binding.binding_digest,
                "sequence": expected_tail.sequence + 1,
                "previous_digest": expected_tail.event_digest,
                "event": event.body(),
            }
            encoded = canonical_json(body)
            if len(encoded.encode("utf-8")) > 16384:
                raise ValidationFailed("Continuity event byte bound exceeded")
            event_digest = digest(body)
            event_id = str(new_uuid7())
            connection.execute(
                "insert into session_event values(?,?,?,?,?)",
                (event_id, binding.session_id, event.kind, event_digest, event.occurred_at),
            )
            connection.execute(
                "insert into session_event_detail values(?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    binding.session_id,
                    expected_tail.sequence + 1,
                    expected_tail.event_digest,
                    event.idempotency_key,
                    event_digest,
                    event.spool_digest,
                    encoded,
                ),
            )
            return ContinuityTail(expected_tail.sequence + 1, event_digest)

    @staticmethod
    def _no_pending(connection: sqlite3.Connection, binding: ContinuityBinding) -> None:
        jobs = connection.execute(
            "select 1 from local_job where json_extract(payload_json,'$.session_id')=?"
            " and state in ('ready','running','recovery-required') limit 1",
            (binding.session_id,),
        ).fetchone()
        pending = connection.execute(
            "select 1 from local_effect_claim c join local_job j on j.id=c.job_id"
            " left join local_effect_receipt r on r.claim_id=c.id"
            " left join continuity_effect_binding b on b.claim_id=c.id"
            " where (b.session_id=? or json_extract(j.payload_json,'$.session_id')=?)"
            " and (r.id is null or r.status='unknown') and not exists("
            " select 1 from local_recovery_case rc join local_recovery_resolution rr"
            " on rr.recovery_case_id=rc.id where rc.effect_claim_id=c.id and rc.state='resolved'"
            " and rr.outcome in ('completed','failed')) limit 1",
            (binding.session_id, binding.session_id),
        ).fetchone()
        outbox = connection.execute(
            "select 1 from local_outbox o left join local_outbox_delivery d on d.outbox_id=o.id"
            " left join local_outbox_receipt r on r.outbox_id=o.id"
            " join local_job j on j.id=o.job_id"
            " left join continuity_outbox_binding b on b.outbox_id=o.id"
            " where (b.session_id=? or json_extract(j.payload_json,'$.session_id')=?)"
            " and (d.outbox_id is null or d.state<>'delivered' or r.id is null"
            " or r.claim_id<>d.claim_id or r.fencing_token<>d.fencing_counter"
            " or not(r.status='delivered' or (r.status='unknown' and exists("
            " select 1 from local_recovery_case rc join local_recovery_resolution rr"
            " on rr.recovery_case_id=rc.id where rc.outbox_id=o.id and rc.state='resolved'"
            " and rr.outcome='delivered')))) limit 1",
            (binding.session_id, binding.session_id),
        ).fetchone()
        recovery = connection.execute(
            "select 1 from local_recovery_case c join local_job j on j.id=c.job_id"
            " where c.state='open' and json_extract(j.payload_json,'$.session_id')=? limit 1",
            (binding.session_id,),
        ).fetchone()
        if jobs or pending or outbox or recovery:
            raise PolicyViolation("Continuity pending effect/outbox/recovery blocks ACK")

    def hydrate(
        self,
        binding: ContinuityBinding,
        context: LocalContext,
        *,
        idempotency_key: str,
        checkpoint_digest: str | None = None,
    ) -> str:
        logical(idempotency_key, "Hydration key")
        if not isinstance(context, LocalContext):
            raise ValidationFailed("Typed local context required")
        context.__post_init__()
        context.assert_scope(binding)
        if checkpoint_digest is not None:
            digest_text(checkpoint_digest)
        with self._transaction() as connection:
            current = self._assert_binding(connection, binding)
            if current["status"] != "open":
                raise PolicyViolation("Hydration requires open session")
            self._no_pending(connection, binding)
            source = connection.execute(
                "select revision_ref from source_snapshot where id=?", (binding.source_snapshot_id,)
            ).fetchone()[0]
            if any(
                item.kind in {"source-slice", "source-diff"} and item.source_revision != source
                for item in context.manifest.selected
            ):
                raise PolicyViolation("Hydration selected source revision mismatch")
            if self.source_resolver is None:
                raise PolicyViolation("Hydration requires trusted source resolver")
            for candidate in context.selected_provenance:
                resolved = self.source_resolver(binding, candidate.provenance_body)
                if resolved != dict(context.fragments)[candidate.candidate_id]:
                    raise PolicyViolation("Hydration selected fragment source mismatch")
            body = {
                "binding_digest": binding.binding_digest,
                "session_id": binding.session_id,
                "checkpoint_digest": checkpoint_digest,
                "context": context.body(),
            }
            manifest_digest = digest(body)
            now = _now()
            receipt_body = {
                "session_id": binding.session_id,
                "manifest_digest": manifest_digest,
                "idempotency_key": idempotency_key,
                "grants_authority": False,
            }
            receipt_digest = digest(receipt_body)
            existing = connection.execute(
                "select * from hydration_receipt where session_id=? and idempotency_key=?",
                (binding.session_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["receipt_digest"] != receipt_digest
                    or existing["manifest_digest"] != manifest_digest
                    or existing["session_id"] != binding.session_id
                ):
                    raise PolicyViolation("Hydration replay payload drift")
                self._verified_manifest(connection, binding, manifest_digest)
                return manifest_digest
            encoded = canonical_json(body)
            old_manifest = connection.execute(
                "select body_json from context_manifest where manifest_digest=?", (manifest_digest,)
            ).fetchone()
            if old_manifest is None:
                connection.execute(
                    "insert into context_manifest values(?,?,?,?,?,?,?)",
                    (
                        manifest_digest,
                        binding.session_id,
                        checkpoint_digest,
                        context.manifest.token_budget,
                        sum(item.token_count for item in context.manifest.selected),
                        encoded,
                        now,
                    ),
                )
            elif old_manifest[0] != encoded:
                raise PolicyViolation("Stored context payload drift")
            self._verified_manifest(connection, binding, manifest_digest)
            connection.execute(
                "insert into hydration_receipt values(?,?,?,?,?)",
                (receipt_digest, binding.session_id, manifest_digest, idempotency_key, now),
            )
            return manifest_digest

    @staticmethod
    def _checkpoint_body(row: sqlite3.Row, binding: ContinuityBinding) -> dict[str, Any]:
        try:
            body = json.loads(row["body_json"])
            if (
                not isinstance(body, dict)
                or digest(body) != row["checkpoint_digest"]
                or body["binding_digest"] != binding.binding_digest
                or any(
                    body[name] != row[name]
                    for name in (
                        "session_id",
                        "idempotency_key",
                        "covered_sequence",
                        "covered_event_digest",
                        "source_snapshot_id",
                        "context_digest",
                        "spool_digest",
                    )
                )
                or body["grants_authority"] is not False
                or body["approval_inherited"] is not False
            ):
                raise PolicyViolation("Checkpoint body/column integrity mismatch")
            return body
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyViolation("Checkpoint malformed durable evidence") from exc

    def _verified_manifest(
        self, connection: sqlite3.Connection, binding: ContinuityBinding, manifest_digest: str
    ) -> dict[str, Any]:
        row = connection.execute(
            "select * from context_manifest where manifest_digest=? and session_id=?",
            (manifest_digest, binding.session_id),
        ).fetchone()
        if row is None:
            raise PolicyViolation("Continuity durable context missing")
        try:
            body = json.loads(row["body_json"])
            if not isinstance(body, dict):
                raise PolicyViolation("Context durable body must be an object")
            context = body["context"]
            compiler = context["compiler"]
            bounded_int(compiler["token_budget"], maximum=131072)
            ranking = context["ranking_request"]
            fragments = context["fragments"]
            selected = {item["candidate_id"]: item for item in compiler["selected"]}
            provenance = {item["id"]: item for item in context["selected_provenance"]}
            omitted = {item["candidate_id"] for item in compiler["omitted"]}
            metrics = compiler["compiler_metrics"]
            work_ref = None if binding.work_item_id is None else f"work/{binding.work_item_id}"
            if (
                digest(body) != manifest_digest
                or body["binding_digest"] != binding.binding_digest
                or body["session_id"] != row["session_id"]
                or body["checkpoint_digest"] != row["checkpoint_digest"]
                or compiler["token_budget"] != row["token_budget"]
                or compiler["schema_version"] != 2
                or compiler["compiler_version"] != 2
                or compiler["grants_authority"] is not False
                or row["token_count"] > row["token_budget"]
                or sum(item["token_count"] for item in selected.values()) != row["token_count"]
                or metrics["selected_count"] != len(selected)
                or metrics["selected_tokens"] != row["token_count"]
                or metrics["token_budget"] != row["token_budget"]
                or metrics["omitted_count"] != len(omitted)
                or len(omitted) != len(compiler["omitted"])
                or set(selected) & omitted
                or len(selected) > 256
                or len(selected) != len(compiler["selected"])
                or len(provenance) != len(context["selected_provenance"])
                or set(selected) != set(fragments)
                or set(selected) != set(provenance)
                or compiler["ranking_snapshot_digest"] != digest(ranking)
                or ranking["project_scope_ref"] != f"project/{binding.project_id}"
                or ranking["realm_scope_ref"] != f"realm/{binding.realm_id}"
                or ranking["work_scope_ref"] != work_ref
                or ranking["step_scope_ref"] is not None
                or ranking.get("additional_scope_refs", []) not in ([], ["global-user"])
                or context["grants_authority"] is not False
                or context["approval_inherited"] is not False
            ):
                raise PolicyViolation("Context body/column/provenance integrity mismatch")
            if self.source_resolver is None:
                raise PolicyViolation("Resume requires trusted source resolver")
            allowed_scopes = {
                f"project/{binding.project_id}",
                f"realm/{binding.realm_id}",
                f"session/{binding.session_id}",
            }
            if work_ref is not None:
                allowed_scopes.add(work_ref)
            if binding.run_id is not None:
                allowed_scopes.add(f"run/{binding.run_id}")
            current_revision = connection.execute(
                "select revision_ref from source_snapshot where id=?", (binding.source_snapshot_id,)
            ).fetchone()[0]
            for identifier, item in selected.items():
                bounded_int(item["token_count"], maximum=131072)
                source = provenance[identifier]
                if (
                    digest(source) != item["candidate_digest"]
                    or source["digest"] != item["content_digest"]
                    or source["source_ref"] != item["source_ref"]
                    or source["revision"] != item["source_revision"]
                    or (
                        source["kind"] in {"source-slice", "source-diff"}
                        and source["revision"] != current_revision
                    )
                    or source["kind"] != item["kind"]
                    or source["authority"] != item["authority"]
                    or source["tokens"] != item["token_count"]
                    or count_context_tokens(fragments[identifier]) != item["token_count"]
                    or (
                        source["scope_ref"] not in allowed_scopes
                        and not (
                            source["scope_ref"] == "global-user"
                            and (
                                source["kind"] == "system-policy"
                                or (
                                    source["kind"] == "knowledge"
                                    and ranking.get("additional_scope_refs") == ["global-user"]
                                )
                            )
                        )
                    )
                    or self.source_resolver(binding, source) != fragments[identifier]
                ):
                    raise PolicyViolation("Context persisted source provenance mismatch")
            return body
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyViolation("Context malformed durable evidence") from exc

    def checkpoint(
        self,
        binding: ContinuityBinding,
        *,
        expected_tail: ContinuityTail,
        context_digest: str,
        idempotency_key: str,
        spool_digests: tuple[str, ...],
    ) -> str:
        """Caller holds the spool writer barrier through this complete transaction."""
        logical(idempotency_key, "Checkpoint key")
        digest_text(context_digest)
        if not isinstance(expected_tail, ContinuityTail):
            raise ValidationFailed("Typed continuity tail required")
        expected_tail.__post_init__()
        if not isinstance(spool_digests, tuple):
            raise ValidationFailed("Checkpoint exact spool digest tuple required")
        for value in spool_digests:
            digest_text(value)
        with self._transaction() as connection:
            current = self._assert_binding(connection, binding)
            if current["status"] != "open":
                raise PolicyViolation("Checkpoint requires open session")
            rows = self._events(connection, binding.session_id)
            if not rows or self._tail(rows) != expected_tail:
                raise ConcurrencyConflict("Checkpoint event boundary mismatch")
            persisted_spool = tuple(
                row["spool_digest"] for row in rows if row["spool_digest"] is not None
            )
            if persisted_spool != spool_digests:
                raise PolicyViolation("Checkpoint unpersisted/spool delta blocks ACK")
            self._no_pending(connection, binding)
            self._verified_manifest(connection, binding, context_digest)
            body = {
                "session_id": binding.session_id,
                "binding_digest": binding.binding_digest,
                "covered_sequence": expected_tail.sequence,
                "covered_event_digest": expected_tail.event_digest,
                "source_snapshot_id": binding.source_snapshot_id,
                "context_digest": context_digest,
                "spool_digest": digest(spool_digests),
                "idempotency_key": idempotency_key,
                "grants_authority": False,
                "approval_inherited": False,
            }
            result = digest(body)
            existing = connection.execute(
                "select * from continuity_checkpoint where session_id=? and idempotency_key=?",
                (binding.session_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing[0] != result:
                    raise PolicyViolation("Checkpoint replay payload drift")
                self._checkpoint_body(existing, binding)
                return result
            connection.execute(
                "insert into continuity_checkpoint values(?,?,?,?,?,?,?,?,?,?)",
                (
                    result,
                    binding.session_id,
                    idempotency_key,
                    expected_tail.sequence,
                    expected_tail.event_digest,
                    binding.source_snapshot_id,
                    context_digest,
                    digest(spool_digests),
                    canonical_json(body),
                    _now(),
                ),
            )
            return result

    def resume(self, binding: ContinuityBinding, checkpoint_digest: str) -> dict[str, Any]:
        digest_text(checkpoint_digest)
        with self._transaction() as connection:
            self._assert_binding(connection, binding)
            self._no_pending(connection, binding)
            rows = self._events(connection, binding.session_id)
            row = connection.execute(
                "select * from continuity_checkpoint where checkpoint_digest=? and session_id=?",
                (checkpoint_digest, binding.session_id),
            ).fetchone()
            if row is None or digest(json.loads(row["body_json"])) != checkpoint_digest:
                raise PolicyViolation("Resume checkpoint absent or corrupted")
            body = self._checkpoint_body(row, binding)
            if (
                len(rows) < row["covered_sequence"]
                or rows[row["covered_sequence"] - 1]["event_digest"] != row["covered_event_digest"]
            ):
                raise PolicyViolation("Resume covered event mismatch")
            manifest_body = self._verified_manifest(connection, binding, row["context_digest"])
            return {
                "checkpoint": body,
                "context": manifest_body,
                "uncovered_events": len(rows) - row["covered_sequence"],
                "grants_authority": False,
                "approval_inherited": False,
                "reacquire_required": True,
            }

    def bind_effect(self, binding: ContinuityBinding, claim_id: str) -> None:
        logical(claim_id, "Effect claim")
        with self._transaction() as connection:
            current = self._assert_binding(connection, binding)
            claim = connection.execute(
                "select job_id from local_effect_claim where id=?", (claim_id,)
            ).fetchone()
            if claim is None:
                raise PolicyViolation("Continuity effect claim missing")
            value = digest(
                {
                    "session_id": binding.session_id,
                    "claim_id": claim_id,
                    "job_id": claim[0],
                    "binding_digest": binding.binding_digest,
                }
            )
            existing = connection.execute(
                "select binding_digest from continuity_effect_binding where claim_id=?", (claim_id,)
            ).fetchone()
            if existing is not None:
                if existing[0] != value:
                    raise PolicyViolation("Effect binding replay drift")
                return
            if current["status"] == "closed":
                raise PolicyViolation("Closed continuity session cannot bind new effects")
            connection.execute(
                "insert into continuity_effect_binding values(?,?,?,?)",
                (claim_id, binding.session_id, claim[0], value),
            )
