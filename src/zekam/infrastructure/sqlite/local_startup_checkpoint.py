"""Bounded predecessor checkpoint evidence, never inherited execution authority.

Historical fragments are checked against their durable manifest, not re-resolved or
rendered. The current startup adapter separately validates current sources/policy.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import closing
from dataclasses import fields
from typing import Any

from zekam.application.context_ranking import count_context_tokens
from zekam.application.local_continuity import (
    ContinuityBinding,
    bounded_int,
    digest_text,
    logical,
    timestamp,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.context_continuity import AuthorityLevel, ContextCandidate, ContextCandidateKind
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore

_COMPATIBLE = (
    "project_id",
    "realm_id",
    "work_item_id",
    "run_id",
    "source_snapshot_id",
    "task_digest",
    "policy_digest",
    "plan_digest",
)


def _object(raw: object, maximum: int) -> dict[str, Any]:
    if not isinstance(raw, str) or len(raw.encode()) > maximum:
        raise PolicyViolation("Historical checkpoint bounded document required")
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or canonical_json(body) != raw:
            raise ValueError
        return body
    except (ValueError, TypeError) as exc:
        raise PolicyViolation("Historical checkpoint canonical document required") from exc


class SQLiteStartupCheckpointSource:
    def __init__(self, continuity: SQLiteContinuityStore) -> None:
        if not isinstance(continuity, SQLiteContinuityStore):
            raise ValidationFailed("Checkpoint exact operational store required")
        self.continuity = continuity

    def _current(self, db: sqlite3.Connection, binding: ContinuityBinding) -> None:
        current = self.continuity._assert_binding(db, binding)
        if current["status"] != "open" or binding.run_id is None:
            raise PolicyViolation("Checkpoint startup requires open work/run session")
        self.continuity._no_pending(db, binding)

    @staticmethod
    def _historical_manifest(
        db: sqlite3.Connection, binding: ContinuityBinding, manifest_digest: str
    ) -> list[str]:
        """Check immutable evidence without calling the mutable/current source resolver."""
        digest_text(manifest_digest)
        row = db.execute(
            "select * from context_manifest where manifest_digest=? and session_id=?",
            (manifest_digest, binding.session_id),
        ).fetchone()
        if row is None:
            raise PolicyViolation("Historical checkpoint context missing")
        try:
            body = _object(row["body_json"], 1048576)
            context = body["context"]
            compiler = context["compiler"]
            ranking = context["ranking_request"]
            fragments = context["fragments"]
            bounded_int(compiler["token_budget"], maximum=131072)
            selected_rows, omitted_rows = compiler["selected"], compiler["omitted"]
            source_rows = context["selected_provenance"]
            if not all(
                isinstance(value, list) for value in (selected_rows, omitted_rows, source_rows)
            ):
                raise PolicyViolation("Historical context partitions must be lists")
            if (
                len(selected_rows) > 256
                or len(source_rows) > 256
                or not isinstance(fragments, dict)
            ):
                raise PolicyViolation("Historical context partition bound")
            selected = {item["candidate_id"]: item for item in selected_rows}
            provenance = {item["id"]: item for item in source_rows}
            omitted = {item["candidate_id"] for item in omitted_rows}
            metrics = compiler["compiler_metrics"]
            work_ref = f"work/{binding.work_item_id}"
            if (
                set(body) != {"binding_digest", "session_id", "checkpoint_digest", "context"}
                or digest(body) != manifest_digest
                or body["binding_digest"] != binding.binding_digest
                or body["session_id"] != binding.session_id
                or body["checkpoint_digest"] != row["checkpoint_digest"]
                or compiler["token_budget"] != row["token_budget"]
                or type(compiler["compiler_version"]) is not int
                or compiler["compiler_version"] != 2
                or type(compiler["schema_version"]) is not int
                or compiler["schema_version"] != 2
                or compiler["grants_authority"] is not False
                or len(selected) != len(selected_rows)
                or len(provenance) != len(source_rows)
                or len(omitted) != len(omitted_rows)
                or set(selected) & omitted
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
                raise PolicyViolation("Historical context column/partition/binding drift")
            total = 0
            refs: list[str] = []
            scopes = {
                work_ref,
                f"project/{binding.project_id}",
                f"realm/{binding.realm_id}",
                f"session/{binding.session_id}",
                f"run/{binding.run_id}",
            }
            for identifier, item in selected.items():
                logical(identifier, "Historical candidate")
                source, text = provenance[identifier], fragments[identifier]
                tokens = bounded_int(item["token_count"], maximum=131072)
                if not isinstance(text, str) or not text:
                    raise PolicyViolation("Historical fragment nonempty text required")
                logical(item["source_ref"], "Historical source")
                allowed_global = source["scope_ref"] == "global-user" and (
                    source["kind"] == "system-policy"
                    or (
                        source["kind"] == "knowledge"
                        and ranking.get("additional_scope_refs") == ["global-user"]
                    )
                )
                if (
                    digest(source) != item["candidate_digest"]
                    or digest(text) != item["content_digest"]
                    or source["digest"] != item["content_digest"]
                    or source["source_ref"] != item["source_ref"]
                    or source["revision"] != item["source_revision"]
                    or source["kind"] != item["kind"]
                    or source["kind"] not in {kind.value for kind in ContextCandidateKind}
                    or type(source["authority"]) is not int
                    or type(item["authority"]) is not int
                    or source["authority"] not in {0, 1, 2, 3}
                    or source["authority"] != item["authority"]
                    or type(source["tokens"]) is not int
                    or source["tokens"] != tokens
                    or count_context_tokens(text) != tokens
                    or (source["scope_ref"] not in scopes and not allowed_global)
                ):
                    raise PolicyViolation("Historical fragment/provenance integrity drift")
                total += tokens
                refs.append(item["source_ref"])
            for key, expected in {
                "selected_count": len(selected),
                "selected_tokens": total,
                "omitted_count": len(omitted),
                "token_budget": row["token_budget"],
            }.items():
                if type(metrics[key]) is not int or metrics[key] != expected:
                    raise PolicyViolation("Historical context metrics drift")
            if total != row["token_count"] or total > row["token_budget"]:
                raise PolicyViolation("Historical context token budget drift")
            receipts = db.execute(
                "select * from hydration_receipt where manifest_digest=? order by receipt_digest",
                (manifest_digest,),
            ).fetchall()
            if not receipts:
                raise PolicyViolation("Historical hydration receipt missing")
            for receipt in receipts:
                logical(receipt["idempotency_key"], "Historical hydration key")
                timestamp(receipt["created_at"])
                expected_receipt = digest(
                    {
                        "session_id": binding.session_id,
                        "manifest_digest": manifest_digest,
                        "idempotency_key": receipt["idempotency_key"],
                        "grants_authority": False,
                    }
                )
                if (
                    receipt["session_id"] != binding.session_id
                    or receipt["receipt_digest"] != expected_receipt
                ):
                    raise PolicyViolation("Historical hydration receipt integrity drift")
            return refs
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise PolicyViolation("Historical context malformed evidence") from exc

    def _metadata(
        self, db: sqlite3.Connection, binding: ContinuityBinding, row: sqlite3.Row
    ) -> tuple[ContinuityBinding, str]:
        old_row = db.execute(
            "select * from continuity_session_binding where session_id=?", (row["session_id"],)
        ).fetchone()
        if old_row is None:
            raise PolicyViolation("Historical checkpoint binding missing")
        old = ContinuityBinding(
            **{field.name: old_row[field.name] for field in fields(ContinuityBinding)}
        )
        if old.session_id == binding.session_id or any(
            getattr(old, name) != getattr(binding, name) for name in _COMPATIBLE
        ):
            raise PolicyViolation("Historical checkpoint exact predecessor binding required")
        # Exact-compatible historical owner admission, without any old source/model read.
        self.continuity._assert_binding(db, old)
        self.continuity._no_pending(db, old)
        body = _object(row["body_json"], 65536)
        self.continuity._checkpoint_body(row, old)
        if (
            set(body)
            != {
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
            or body["source_snapshot_id"] != old.source_snapshot_id
        ):
            raise PolicyViolation("Historical checkpoint exact fields/source required")
        timestamp(row["created_at"])
        logical(body["idempotency_key"], "Historical checkpoint key")
        covered = bounded_int(body["covered_sequence"], maximum=10000)
        count = db.execute(
            "select count(*) from session_event where session_id=?", (old.session_id,)
        ).fetchone()[0]
        if count > 10000:
            raise PolicyViolation("Historical checkpoint ledger exceeds bounded verification")
        events = self.continuity._events(db, old.session_id)
        for event in events:
            envelope = _object(event["body_json"], 65536)
            if (
                set(envelope)
                != {"session_id", "binding_digest", "sequence", "previous_digest", "event"}
                or type(envelope["sequence"]) is not int
            ):
                raise PolicyViolation("Historical event exact canonical envelope required")
        if (
            covered > len(events)
            or events[covered - 1]["event_digest"] != body["covered_event_digest"]
        ):
            raise PolicyViolation("Historical checkpoint covered event prefix drift")
        spool = tuple(
            event["spool_digest"] for event in events[:covered] if event["spool_digest"] is not None
        )
        if digest(spool) != body["spool_digest"]:
            raise PolicyViolation("Historical checkpoint covered spool prefix drift")
        refs = self._historical_manifest(db, old, body["context_digest"])
        metadata = {
            "schema": "zekam-startup-checkpoint-evidence/v1",
            "checkpoint_digest": row["checkpoint_digest"],
            "predecessor_session_id": old.session_id,
            "manifest_digest": body["context_digest"],
            "covered_sequence": covered,
            "covered_event_digest": body["covered_event_digest"],
            "source_snapshot_id": old.source_snapshot_id,
            "source_refs": refs[:16],
            "source_ref_count": len(refs),
            "omitted_source_ref_count": max(0, len(refs) - 16),
            "historical_evidence_only": True,
            "current_source_revalidation_required": True,
            "grants_authority": False,
            "approval_inherited": False,
            "reacquire_required": True,
        }
        text = canonical_json(metadata)
        if len(text.encode()) > 16384:
            raise PolicyViolation("Historical checkpoint metadata byte bound exceeded")
        return old, text

    @staticmethod
    def _candidate(
        old: ContinuityBinding, checkpoint: str, text: str, observed_at: dt.datetime
    ) -> ContextCandidate:
        return ContextCandidate(
            candidate_id=f"startup-checkpoint-{checkpoint[7:39]}",
            authority=AuthorityLevel.VERIFIED,
            observed_at=observed_at,
            source_revision=checkpoint,
            content_digest=digest(text),
            token_count=count_context_tokens(text),
            kind=ContextCandidateKind.CHECKPOINT,
            source_ref=f"checkpoint/{checkpoint[7:]}",
            scope_ref=f"work/{old.work_item_id}",
            identity_refs=(f"work/{old.work_item_id}",),
            applicable_roles=("builder",),
            canonical_revision_id=old.session_id,
        )

    def snapshot(
        self, binding: ContinuityBinding, *, observed_at: dt.datetime
    ) -> tuple[tuple[ContextCandidate, str] | None, dict[str, Any]]:
        if not isinstance(observed_at, dt.datetime) or observed_at.tzinfo is None:
            raise ValidationFailed("Checkpoint observation requires aware time")
        with closing(
            sqlite3.connect(f"{self.continuity.path.resolve().as_uri()}?mode=ro", uri=True)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("pragma query_only=on")
            db.execute("begin")
            self._current(db, binding)
            where = " and ".join(f"b.{name}=?" for name in _COMPATIBLE)
            row = db.execute(
                "select c.* from continuity_checkpoint c join continuity_session_binding b"
                " on b.session_id=c.session_id where b.session_id<>? and "
                + where
                + " order by c.created_at desc,c.checkpoint_digest desc limit 1",
                (binding.session_id, *(getattr(binding, name) for name in _COMPATIBLE)),
            ).fetchone()
            if row is None:
                # Never disclose whether a different realm/project/work has history.
                history = db.execute(
                    "select 1 from continuity_checkpoint c join continuity_session_binding b"
                    " on b.session_id=c.session_id where b.session_id<>? and b.project_id=?"
                    " and b.realm_id=? and b.work_item_id=? limit 1",
                    (
                        binding.session_id,
                        binding.project_id,
                        binding.realm_id,
                        binding.work_item_id,
                    ),
                ).fetchone()
                return None, {
                    "state": "fresh-empty" if history is None else "incompatible-history",
                    "grants_authority": False,
                    "fragment_count": 0,
                }
            old, text = self._metadata(db, binding, row)
            candidate = self._candidate(old, row["checkpoint_digest"], text, observed_at)
            return (candidate, text), {
                "state": "compatible-metadata-only",
                "fragment_count": 1,
                "checkpoint_digest": row["checkpoint_digest"],
                "grants_authority": False,
            }

    def __call__(self, binding: ContinuityBinding, provenance: dict[str, Any]) -> str:
        if not isinstance(provenance, dict):
            raise ValidationFailed("Checkpoint provenance object required")
        checkpoint = digest_text(provenance.get("revision"))
        observed = provenance.get("observed_at")
        if isinstance(observed, str):
            try:
                observed = dt.datetime.fromisoformat(observed)
            except ValueError as exc:
                raise ValidationFailed("Checkpoint observation malformed") from exc
        if not isinstance(observed, dt.datetime) or observed.tzinfo is None:
            raise ValidationFailed("Checkpoint observation requires aware time")
        with closing(
            sqlite3.connect(f"{self.continuity.path.resolve().as_uri()}?mode=ro", uri=True)
        ) as db:
            db.row_factory = sqlite3.Row
            db.execute("pragma query_only=on")
            db.execute("begin")
            self._current(db, binding)
            row = db.execute(
                "select * from continuity_checkpoint where checkpoint_digest=?", (checkpoint,)
            ).fetchone()
            if row is None:
                raise PolicyViolation("Pinned historical checkpoint missing")
            old, text = self._metadata(db, binding, row)
            expected = self._candidate(old, checkpoint, text, observed).provenance_body
            if canonical_json(provenance) != canonical_json(expected):
                raise PolicyViolation("Pinned historical checkpoint provenance drift")
            return text
