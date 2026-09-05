"""Independent raw-SQL rejection tests for the dormant operational-v4 DDL."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.unit.test_operational_schema_v4 import (
    ATTACHMENT_ID,
    NOW,
    _attachment,
    _generation_one,
    _sha,
    _v3_fixture_promoted_for_ddl_test,
)

from zekam.domain.canonical import digest
from zekam.infrastructure.sqlite import operational_schema as schema

pytestmark = pytest.mark.unit


def _revision_body(**changes: object) -> str:
    body: dict[str, object] = {
        "revision_digest": _sha("1"),
        "attachment_id": ATTACHMENT_ID,
        "revision_number": 1,
        "previous_revision_digest": None,
        "operation_key": "attach",
        "state": "attached",
        "process_generation_digest": _sha("e"),
        "active_manifest_digest": None,
        "active_hydration_receipt_digest": None,
        "checkpoint_digest": None,
        "pre_compaction_event_digest": None,
        "post_compaction_event_digest": None,
        "close_request_digest": None,
        "pre_close_event_digest": None,
        "close_receipt_digest": None,
        "session_closed_event_digest": None,
        "hook_recovery_case_id": None,
        "hook_recovery_resolution_id": None,
        "local_recovery_case_id": None,
        "local_recovery_resolution_id": None,
        "crash_recovered_event_digest": None,
        "crash_recovered_receipt_digest": None,
        "created_at": NOW,
    }
    body.update(changes)
    return json.dumps(body, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _revision_one(connection: sqlite3.Connection) -> None:
    connection.execute(
        "insert into continuity_hook_attachment_revision("
        "revision_digest,attachment_id,revision_number,previous_revision_digest,"
        "operation_key,state,process_generation_digest,body_json,created_at)"
        " values(?,?,?,?,?,?,?,?,?)",
        (
            _sha("1"),
            ATTACHMENT_ID,
            1,
            None,
            "attach",
            "attached",
            _sha("e"),
            _revision_body(),
            NOW,
        ),
    )


def _insert_revision(connection: sqlite3.Connection, **changes: object) -> str:
    values: dict[str, object] = {
        "revision_digest": _sha("1"),
        "attachment_id": ATTACHMENT_ID,
        "revision_number": 1,
        "previous_revision_digest": None,
        "operation_key": "attach",
        "state": "attached",
        "process_generation_digest": _sha("e"),
        "active_manifest_digest": None,
        "active_hydration_receipt_digest": None,
        "checkpoint_digest": None,
        "pre_compaction_event_digest": None,
        "post_compaction_event_digest": None,
        "close_request_digest": None,
        "pre_close_event_digest": None,
        "close_receipt_digest": None,
        "session_closed_event_digest": None,
        "hook_recovery_case_id": None,
        "hook_recovery_resolution_id": None,
        "local_recovery_case_id": None,
        "local_recovery_resolution_id": None,
        "crash_recovered_event_digest": None,
        "crash_recovered_receipt_digest": None,
        "created_at": NOW,
    }
    values.update(changes)
    columns = tuple(values)
    body_changes = {key: values[key] for key in columns if key != "created_at"}
    values["body_json"] = _revision_body(**body_changes)
    columns = (*columns[:-1], "body_json", "created_at")
    connection.execute(
        f"insert into continuity_hook_attachment_revision({','.join(columns)})"
        f" values({','.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )
    return str(values["revision_digest"])


def _insert_internal_event(
    connection: sqlite3.Connection,
    *,
    receipt_digest: str,
    event_digest: str,
    event_id: str,
    event_kind: str,
    operation_key: str,
    sequence: int,
    previous: str,
    attachment_revision_digest: str,
    close_request_digest: str | None = None,
    close_receipt_digest: str | None = None,
) -> None:
    binding_digest = str(
        connection.execute(
            "select binding_digest from continuity_session_binding where session_id='session'"
        ).fetchone()[0]
    )
    body = json.dumps(
        {
            "attachment_revision_digest": attachment_revision_digest,
            "binding_digest": binding_digest,
            "created_at": NOW,
            "event_digest": event_digest,
            "event_kind": event_kind,
            "expected_previous_event_digest": previous,
            "operation_key": operation_key,
            "session_id": "session",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        "insert into continuity_internal_event_receipt("
        "receipt_digest,event_digest,session_id,binding_digest,event_kind,operation_key,"
        "expected_previous_event_digest,close_request_digest,close_receipt_digest,"
        "attachment_revision_digest,body_json,created_at) values(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            receipt_digest,
            event_digest,
            "session",
            binding_digest,
            event_kind,
            operation_key,
            previous,
            close_request_digest,
            close_receipt_digest,
            attachment_revision_digest,
            body,
            NOW,
        ),
    )
    connection.execute(
        "insert into session_event values(?,?,?,?,?)",
        (event_id, "session", event_kind, event_digest, NOW),
    )
    connection.execute(
        "insert into session_event_detail values(?,?,?,?,?,?,null,'{}')",
        (event_id, "session", sequence, previous, operation_key, event_digest),
    )


def _seed_hydrated_v4(path: Path) -> None:
    _v3_fixture_promoted_for_ddl_test(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        _insert_revision(
            connection,
            revision_digest=_sha("2"),
            revision_number=2,
            previous_revision_digest=_sha("1"),
            operation_key="hydrate",
            state="hydrated",
            active_manifest_digest=digest("context"),
            active_hydration_receipt_digest=digest("hydration"),
        )


def _raw_freeze(
    connection: sqlite3.Connection,
    *,
    revision_changes: dict[str, object] | None = None,
) -> dict[str, str]:
    values = {
        "checkpoint_event": _sha("4"),
        "pre_close_event": _sha("6"),
        "checkpoint": _sha("7"),
        "request": _sha("8"),
        "frozen_revision": _sha("f"),
        "outbox": "close-outbox",
    }
    previous = str(
        connection.execute(
            "select event_digest from session_event_detail where session_id='session'"
            " order by sequence desc limit 1"
        ).fetchone()[0]
    )
    _insert_internal_event(
        connection,
        receipt_digest=_sha("3"),
        event_digest=values["checkpoint_event"],
        event_id="checkpoint-requested",
        event_kind="CHECKPOINT_REQUESTED",
        operation_key="checkpoint-requested",
        sequence=2,
        previous=previous,
        attachment_revision_digest=_sha("2"),
        close_request_digest=values["request"],
    )
    _insert_internal_event(
        connection,
        receipt_digest=_sha("5"),
        event_digest=values["pre_close_event"],
        event_id="pre-close",
        event_kind="PRE_CLOSE",
        operation_key="pre-close",
        sequence=3,
        previous=values["checkpoint_event"],
        attachment_revision_digest=_sha("2"),
        close_request_digest=values["request"],
    )
    source_snapshot_id = str(
        connection.execute(
            "select source_snapshot_id from continuity_session_binding where session_id='session'"
        ).fetchone()[0]
    )
    connection.execute(
        "insert into continuity_checkpoint values(?,?,?,?,?,?,?,?,?,?)",
        (
            values["checkpoint"],
            "session",
            "close-checkpoint",
            3,
            values["pre_close_event"],
            source_snapshot_id,
            digest("context"),
            _sha("0"),
            "{}",
            NOW,
        ),
    )
    connection.execute(
        "insert into continuity_close_request values(?,?,?,?,?,?)",
        (
            values["request"],
            "session",
            values["checkpoint"],
            3,
            json.dumps(
                {"manifest_digest": digest("context")},
                separators=(",", ":"),
                sort_keys=True,
            ),
            NOW,
        ),
    )
    binding_digest = str(
        connection.execute(
            "select binding_digest from continuity_session_binding where session_id='session'"
        ).fetchone()[0]
    )
    payload = json.dumps(
        {
            "binding_digest": binding_digest,
            "operation": "continuity.compile",
            "request_digest": values["request"],
            "run_id": None,
            "session_id": "session",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    connection.execute(
        "insert into local_job(id,idempotency_key,payload_json,state,max_attempts,"
        "available_at,created_at,updated_at) values('close-job','close-job-key',?,'ready',1,?,?,?)",
        (payload, NOW, NOW, NOW),
    )
    connection.execute(
        "insert into local_outbox values(?,?,?,?,?,?,?)",
        (
            values["outbox"],
            "close-job",
            "close-outbox-key",
            "continuity.compile",
            payload,
            _sha("a"),
            NOW,
        ),
    )
    connection.execute(
        "insert into local_outbox_delivery values(?, 'pending',0,null,null,null,null,null,?)",
        (values["outbox"], NOW),
    )
    connection.execute(
        "insert into continuity_outbox_binding values(?,?,?,?,?,?)",
        (
            values["outbox"],
            "session",
            "close-job",
            "close",
            values["request"],
            values["request"],
        ),
    )
    revision = {
        "revision_digest": values["frozen_revision"],
        "revision_number": 3,
        "previous_revision_digest": _sha("2"),
        "operation_key": "freeze",
        "state": "frozen",
        "active_manifest_digest": digest("context"),
        "active_hydration_receipt_digest": digest("hydration"),
        "checkpoint_digest": values["checkpoint"],
        "close_request_digest": values["request"],
        "pre_close_event_digest": values["pre_close_event"],
    }
    revision.update(revision_changes or {})
    _insert_revision(connection, **revision)
    connection.execute("update session set status='closing' where id='session'")
    return values


def _raw_finalize(
    connection: sqlite3.Connection,
    values: dict[str, str],
    *,
    revision_changes: dict[str, object] | None = None,
) -> str:
    close_receipt = _sha("9")
    closed_event = _sha("b")
    connection.execute(
        "update local_outbox_delivery set state='delivered',fencing_counter=1,"
        "claim_id='delivery-claim',owner_id='owner',owner_pid=101,owner_token='token',"
        "expires_at=null,updated_at=? where outbox_id=?",
        (NOW, values["outbox"]),
    )
    connection.execute(
        "insert into local_outbox_receipt values(?,?,?,?,?,?,?)",
        ("delivery-receipt", values["outbox"], "delivery-claim", 1, "delivered", _sha("c"), NOW),
    )
    connection.execute(
        "insert into close_receipt values(?,?,?,?,?,?,?,?)",
        (
            close_receipt,
            values["request"],
            "session",
            values["checkpoint"],
            digest("context"),
            values["outbox"],
            "{}",
            NOW,
        ),
    )
    _insert_internal_event(
        connection,
        receipt_digest=_sha("d"),
        event_digest=closed_event,
        event_id="session-closed",
        event_kind="SESSION_CLOSED",
        operation_key="session-closed",
        sequence=4,
        previous=values["pre_close_event"],
        attachment_revision_digest=values["frozen_revision"],
        close_receipt_digest=close_receipt,
    )
    revision = {
        "revision_digest": _sha("a"),
        "revision_number": 4,
        "previous_revision_digest": values["frozen_revision"],
        "operation_key": "finalize",
        "state": "closed",
        "active_manifest_digest": digest("context"),
        "active_hydration_receipt_digest": digest("hydration"),
        "checkpoint_digest": values["checkpoint"],
        "close_request_digest": values["request"],
        "pre_close_event_digest": values["pre_close_event"],
        "close_receipt_digest": close_receipt,
        "session_closed_event_digest": closed_event,
    }
    revision.update(revision_changes or {})
    _insert_revision(
        connection,
        **revision,
    )
    connection.execute(
        "update session set status='closed',closed_at=?,close_receipt_digest=? where id='session'",
        (NOW, close_receipt),
    )
    return close_receipt


def test_turn_content_digest_characters_are_lower_hex(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        binding = str(
            connection.execute(
                "select binding_digest from continuity_session_binding where session_id='session'"
            ).fetchone()[0]
        )
        invalid = "sha256:a" + "g" * 63
        body = json.dumps(
            {
                "binding_digest": binding,
                "content_digest": invalid,
                "created_at": NOW,
                "item_ref": "item",
                "previous_turn_commit_digest": None,
                "role": "user",
                "session_id": "session",
                "store_generation_digest": _sha("2"),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "insert into continuity_turn_commit_receipt values(?,?,?,?,?,?,?,?,?,?)",
                (
                    _sha("3"),
                    "session",
                    binding,
                    "user",
                    "item",
                    invalid,
                    _sha("2"),
                    None,
                    body,
                    NOW,
                ),
            )


def test_recovery_evidence_digest_characters_are_lower_hex(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    schema.bootstrap_v4(path)
    recovery_id = "018f0000-0000-7000-8000-000000000003"
    invalid = "sha256:a" + "g" * 63
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        _generation_one(connection)
        body = json.dumps(
            {
                "attachment_id": ATTACHMENT_ID,
                "case_kind": "source-drift",
                "created_at": NOW,
                "evidence_digest": invalid,
                "process_generation_digest": _sha("e"),
                "recovery_case_id": recovery_id,
                "session_id": "session",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "insert into continuity_hook_recovery_case values(?,?,?,?,?,?,?,?)",
                (
                    recovery_id,
                    ATTACHMENT_ID,
                    "session",
                    _sha("e"),
                    "source-drift",
                    invalid,
                    body,
                    NOW,
                ),
            )


def test_recovery_case_id_requires_canonical_uuid(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        _generation_one(connection)
        invalid_id = "x" * 36
        body = json.dumps(
            {
                "attachment_id": ATTACHMENT_ID,
                "case_kind": "source-drift",
                "created_at": NOW,
                "evidence_digest": _sha("2"),
                "process_generation_digest": _sha("e"),
                "recovery_case_id": invalid_id,
                "session_id": "session",
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "insert into continuity_hook_recovery_case values(?,?,?,?,?,?,?,?)",
                (
                    invalid_id,
                    ATTACHMENT_ID,
                    "session",
                    _sha("e"),
                    "source-drift",
                    _sha("2"),
                    body,
                    NOW,
                ),
            )


@pytest.mark.parametrize(
    ("created_at", "extra"),
    [(NOW, {"unreviewed": "secret"}), ("not-a-utc-timestamp", {})],
)
def test_recovery_case_requires_exact_body_and_canonical_utc_timestamp(
    tmp_path: Path, created_at: str, extra: dict[str, object]
) -> None:
    path = tmp_path / "v4.db"
    schema.bootstrap_v4(path)
    recovery_id = "018f0000-0000-7000-8000-000000000003"
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        _generation_one(connection)
        body = {
            "attachment_id": ATTACHMENT_ID,
            "case_kind": "source-drift",
            "created_at": created_at,
            "evidence_digest": _sha("2"),
            "process_generation_digest": _sha("e"),
            "recovery_case_id": recovery_id,
            "session_id": "session",
            **extra,
        }
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "insert into continuity_hook_recovery_case values(?,?,?,?,?,?,?,?)",
                (
                    recovery_id,
                    ATTACHMENT_ID,
                    "session",
                    _sha("e"),
                    "source-drift",
                    _sha("2"),
                    json.dumps(body, separators=(",", ":"), sort_keys=True),
                    created_at,
                ),
            )


@pytest.mark.parametrize(
    "event_kind",
    [
        "USER_TURN_COMMITTED",
        "ASSISTANT_TURN_COMMITTED",
        "TOOL_EFFECT_CLAIMED",
        "TOOL_EFFECT_COMPLETED",
    ],
)
def test_truthful_internal_event_requires_preinserted_receipt(
    tmp_path: Path, event_kind: str
) -> None:
    path = tmp_path / "v4.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        with pytest.raises(sqlite3.IntegrityError, match="producer receipt"):
            connection.execute(
                "insert into session_event values(?,?,?,?,?)",
                ("event", "session", event_kind, _sha("2"), NOW),
            )


def test_unknown_bound_session_event_kind_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        with pytest.raises(sqlite3.IntegrityError, match=r"event|producer|kind"):
            connection.execute(
                "insert into session_event values(?,?,?,?,?)",
                ("event", "session", "UNREVIEWED_EVENT", _sha("2"), NOW),
            )


def test_hydrated_revision_requires_exact_manifest_receipt_pair(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        _generation_one(connection)
        _revision_one(connection)
        with pytest.raises(sqlite3.IntegrityError, match=r"revision|transition|manifest"):
            connection.execute(
                "insert into continuity_hook_attachment_revision("
                "revision_digest,attachment_id,revision_number,previous_revision_digest,"
                "operation_key,state,process_generation_digest,body_json,created_at)"
                " values(?,?,?,?,?,?,?,?,?)",
                (
                    _sha("2"),
                    ATTACHMENT_ID,
                    2,
                    _sha("1"),
                    "hydrate-without-evidence",
                    "hydrated",
                    _sha("e"),
                    _revision_body(
                        revision_digest=_sha("2"),
                        revision_number=2,
                        previous_revision_digest=_sha("1"),
                        operation_key="hydrate-without-evidence",
                        state="hydrated",
                    ),
                    NOW,
                ),
            )


def test_revision_cannot_change_to_attached_without_next_generation(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        _generation_one(connection)
        _revision_one(connection)
        with pytest.raises(sqlite3.IntegrityError, match=r"revision|transition|generation"):
            connection.execute(
                "insert into continuity_hook_attachment_revision("
                "revision_digest,attachment_id,revision_number,previous_revision_digest,"
                "operation_key,state,process_generation_digest,body_json,created_at)"
                " values(?,?,?,?,?,?,?,?,?)",
                (
                    _sha("2"),
                    ATTACHMENT_ID,
                    2,
                    _sha("1"),
                    "reattach-same-process",
                    "attached",
                    _sha("e"),
                    _revision_body(
                        revision_digest=_sha("2"),
                        revision_number=2,
                        previous_revision_digest=_sha("1"),
                        operation_key="reattach-same-process",
                    ),
                    NOW,
                ),
            )


def test_recovery_required_revision_needs_exact_unresolved_case(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        _generation_one(connection)
        _revision_one(connection)
        with pytest.raises(sqlite3.IntegrityError, match=r"revision|transition|recovery"):
            connection.execute(
                "insert into continuity_hook_attachment_revision("
                "revision_digest,attachment_id,revision_number,previous_revision_digest,"
                "operation_key,state,process_generation_digest,body_json,created_at)"
                " values(?,?,?,?,?,?,?,?,?)",
                (
                    _sha("2"),
                    ATTACHMENT_ID,
                    2,
                    _sha("1"),
                    "recovery-without-case",
                    "recovery-required",
                    _sha("e"),
                    _revision_body(
                        revision_digest=_sha("2"),
                        revision_number=2,
                        previous_revision_digest=_sha("1"),
                        operation_key="recovery-without-case",
                        state="recovery-required",
                    ),
                    NOW,
                ),
            )


def test_raw_sql_freeze_and_finalize_orders_commit_atomically(tmp_path: Path) -> None:
    path = tmp_path / "terminal-order.db"
    _seed_hydrated_v4(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        assert connection.execute("pragma defer_foreign_keys").fetchone() == (0,)
        connection.execute("begin immediate")
        values = _raw_freeze(connection)
        connection.commit()
        assert connection.execute(
            "select status,closed_at,close_receipt_digest from session where id='session'"
        ).fetchone() == ("closing", None, None)
        assert connection.execute(
            "select state,previous_revision_digest from continuity_hook_attachment_revision "
            "where revision_digest=?",
            (values["frozen_revision"],),
        ).fetchone() == ("frozen", _sha("2"))

        connection.execute("begin immediate")
        close_receipt = _raw_finalize(connection, values)
        connection.commit()
        assert connection.execute(
            "select status,closed_at,close_receipt_digest from session where id='session'"
        ).fetchone() == ("closed", NOW, close_receipt)
        assert connection.execute(
            "select state,previous_revision_digest from continuity_hook_attachment_revision "
            "where revision_digest=?",
            (_sha("a"),),
        ).fetchone() == ("closed", values["frozen_revision"])


@pytest.mark.parametrize(
    "revision_changes",
    [
        {"previous_revision_digest": _sha("1")},
        {"checkpoint_digest": digest("checkpoint")},
        {"pre_close_event_digest": _sha("4")},
        {"post_compaction_event_digest": _sha("6")},
    ],
    ids=("predecessor", "checkpoint", "pre-close-event", "unrelated-cycle-field"),
)
def test_freeze_revision_rejects_one_field_relation_mutation(
    tmp_path: Path, revision_changes: dict[str, object]
) -> None:
    path = tmp_path / "freeze-relation.db"
    _seed_hydrated_v4(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute("begin immediate")
        with pytest.raises(sqlite3.IntegrityError, match=r"revision|transition|evidence"):
            _raw_freeze(connection, revision_changes=revision_changes)
        connection.rollback()
        assert connection.execute("select status from session where id='session'").fetchone() == (
            "open",
        )


@pytest.mark.parametrize(
    "revision_changes",
    [
        {"previous_revision_digest": _sha("2")},
        {"checkpoint_digest": digest("checkpoint")},
        {"pre_close_event_digest": _sha("4")},
        {"session_closed_event_digest": _sha("6")},
        {"post_compaction_event_digest": _sha("6")},
    ],
    ids=(
        "predecessor",
        "checkpoint",
        "pre-close-event",
        "session-closed-event",
        "unrelated-cycle-field",
    ),
)
def test_finalize_revision_rejects_one_field_relation_mutation(
    tmp_path: Path, revision_changes: dict[str, object]
) -> None:
    path = tmp_path / "finalize-relation.db"
    _seed_hydrated_v4(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute("begin immediate")
        values = _raw_freeze(connection)
        connection.commit()
        connection.execute("begin immediate")
        with pytest.raises(sqlite3.IntegrityError, match=r"revision|transition|evidence"):
            _raw_finalize(connection, values, revision_changes=revision_changes)
        connection.rollback()
        assert connection.execute("select status from session where id='session'").fetchone() == (
            "closing",
        )


@pytest.mark.parametrize(
    ("relation", "statement", "parameters"),
    [
        (
            "freeze revision predecessor",
            "update continuity_hook_attachment_revision set previous_revision_digest=? "
            "where revision_digest=?",
            (_sha("1"), _sha("f")),
        ),
        (
            "freeze request checkpoint",
            "update continuity_close_request set checkpoint_digest=? where request_digest=?",
            (digest("checkpoint"), _sha("8")),
        ),
        (
            "freeze checkpoint boundary",
            "update continuity_checkpoint set covered_event_digest=? where checkpoint_digest=?",
            (_sha("4"), _sha("7")),
        ),
        (
            "freeze event producer revision",
            "update continuity_internal_event_receipt set attachment_revision_digest=? "
            "where event_kind='PRE_CLOSE'",
            (_sha("1"),),
        ),
    ],
)
def test_committed_freeze_relations_are_immutable(
    tmp_path: Path, relation: str, statement: str, parameters: tuple[str, ...]
) -> None:
    path = tmp_path / f"freeze-{relation.replace(' ', '-')}.db"
    _seed_hydrated_v4(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute("begin immediate")
        _raw_freeze(connection)
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement, parameters)


@pytest.mark.parametrize(
    ("relation", "statement", "parameters"),
    [
        (
            "closed revision predecessor",
            "update continuity_hook_attachment_revision set previous_revision_digest=? "
            "where revision_digest=?",
            (_sha("2"), _sha("a")),
        ),
        (
            "closed receipt request",
            "update close_receipt set request_digest=? where receipt_digest=?",
            (_sha("0"), _sha("9")),
        ),
        (
            "closed event producer revision",
            "update continuity_internal_event_receipt set attachment_revision_digest=? "
            "where event_kind='SESSION_CLOSED'",
            (_sha("2"),),
        ),
        (
            "terminal session receipt",
            "update session set close_receipt_digest=? where id='session'",
            (_sha("0"),),
        ),
    ],
)
def test_committed_finalize_relations_are_immutable(
    tmp_path: Path, relation: str, statement: str, parameters: tuple[str, ...]
) -> None:
    path = tmp_path / f"finalize-{relation.replace(' ', '-')}.db"
    _seed_hydrated_v4(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute("begin immediate")
        values = _raw_freeze(connection)
        connection.commit()
        connection.execute("begin immediate")
        _raw_finalize(connection, values)
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement, parameters)


def test_attached_to_hydrated_rejects_unrelated_cycle_field(tmp_path: Path) -> None:
    path = tmp_path / "hydrate-relation.db"
    _v3_fixture_promoted_for_ddl_test(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        with pytest.raises(sqlite3.IntegrityError, match=r"revision|transition|evidence"):
            _insert_revision(
                connection,
                revision_digest=_sha("2"),
                revision_number=2,
                previous_revision_digest=_sha("1"),
                operation_key="hydrate-with-unrelated-cycle",
                state="hydrated",
                active_manifest_digest=digest("context"),
                active_hydration_receipt_digest=digest("hydration"),
                post_compaction_event_digest=digest("event"),
            )


def test_frozen_to_closed_rejects_unrelated_cycle_field(tmp_path: Path) -> None:
    path = tmp_path / "closed-relation.db"
    _seed_hydrated_v4(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute("begin immediate")
        values = _raw_freeze(connection)
        connection.commit()
        connection.execute("begin immediate")
        with pytest.raises(sqlite3.IntegrityError, match=r"revision|transition|evidence"):
            _raw_finalize(
                connection,
                values,
                revision_changes={"post_compaction_event_digest": values["pre_close_event"]},
            )
        connection.rollback()
