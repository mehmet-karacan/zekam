"""Reachability and provenance gates for dormant-v4 restored frozen revisions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Literal

import pytest
from tests.unit.test_operational_schema_v4 import ATTACHMENT_ID, NOW, _sha
from tests.unit.test_operational_schema_v4_verifier import (
    _insert_internal_event,
    _insert_revision,
    _raw_freeze,
    _seed_hydrated_v4,
)

from zekam.domain.canonical import digest
from zekam.infrastructure.sqlite import operational_schema as schema

pytestmark = pytest.mark.unit

HOOK_CASE_ID = "018f0000-0000-7000-8000-000000000010"
HOOK_RESOLUTION_ID = "018f0000-0000-7000-8000-000000000011"
RECOVERY_REVISION = _sha("d")
CRASH_RECEIPT = _sha("c")
CRASH_EVENT = _sha("b")
RESTORED_REVISION = _sha("9")


def _json(body: dict[str, object]) -> str:
    return json.dumps(body, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _hook_case(connection: sqlite3.Connection) -> None:
    body: dict[str, object] = {
        "attachment_id": ATTACHMENT_ID,
        "case_kind": "transaction-unknown",
        "created_at": NOW,
        "evidence_digest": _sha("e"),
        "process_generation_digest": _sha("e"),
        "recovery_case_id": HOOK_CASE_ID,
        "session_id": "session",
    }
    connection.execute(
        "insert into continuity_hook_recovery_case values(?,?,?,?,?,?,?,?)",
        (
            HOOK_CASE_ID,
            ATTACHMENT_ID,
            "session",
            _sha("e"),
            "transaction-unknown",
            _sha("e"),
            _json(body),
            NOW,
        ),
    )


def _hook_resolution(connection: sqlite3.Connection) -> None:
    body: dict[str, object] = {
        "created_at": NOW,
        "evidence_digest": _sha("a"),
        "outcome": "restored",
        "recovery_case_id": HOOK_CASE_ID,
        "resolution_id": HOOK_RESOLUTION_ID,
    }
    connection.execute(
        "insert into continuity_hook_recovery_resolution values(?,?,?,?,?,?)",
        (HOOK_RESOLUTION_ID, HOOK_CASE_ID, "restored", _sha("a"), _json(body), NOW),
    )


def _local_case(connection: sqlite3.Connection, outbox_id: str) -> None:
    connection.execute(
        "update local_outbox_delivery set state='claimed',fencing_counter=1,"
        "claim_id='delivery-claim',owner_id='owner',owner_pid=101,owner_token='token',"
        "expires_at=?,updated_at=? where outbox_id=?",
        (NOW, NOW, outbox_id),
    )
    connection.execute(
        "insert into local_outbox_receipt values(?,?,?,?,?,?,?)",
        ("unknown-receipt", outbox_id, "delivery-claim", 1, "unknown", _sha("e"), NOW),
    )
    connection.execute(
        "update local_outbox_delivery set state='recovery-required',updated_at=? where outbox_id=?",
        (NOW, outbox_id),
    )
    connection.execute(
        "insert into local_recovery_case values(?,?,?,?,?,?,?,?,?)",
        (
            "local-case",
            "close-job",
            None,
            outbox_id,
            "outbox-delivery-unknown",
            _sha("e"),
            "open",
            NOW,
            None,
        ),
    )


def _local_resolution(connection: sqlite3.Connection, outbox_id: str) -> None:
    connection.execute(
        "insert into local_recovery_resolution values(?,?,?,?,?)",
        ("local-resolution", "local-case", "delivered", _sha("a"), NOW),
    )
    connection.execute(
        "update local_recovery_case set state='resolved',resolved_at=? where id='local-case'",
        (NOW,),
    )
    connection.execute(
        "update local_outbox_delivery set state='delivered',updated_at=? where outbox_id=?",
        (NOW, outbox_id),
    )


def _insert_crash_recovered(
    connection: sqlite3.Connection,
    *,
    producer: Literal["hook", "local"],
) -> None:
    binding_digest = str(
        connection.execute(
            "select binding_digest from continuity_session_binding where session_id='session'"
        ).fetchone()[0]
    )
    tail = connection.execute(
        "select sequence,event_digest from session_event_detail where session_id='session' "
        "order by sequence desc limit 1"
    ).fetchone()
    body: dict[str, object] = {
        "attachment_revision_digest": RECOVERY_REVISION,
        "binding_digest": binding_digest,
        "created_at": NOW,
        "event_digest": CRASH_EVENT,
        "event_kind": "CRASH_RECOVERED",
        "expected_previous_event_digest": str(tail[1]),
        "operation_key": "crash-recovered",
        "session_id": "session",
    }
    columns = (
        "receipt_digest,event_digest,session_id,binding_digest,event_kind,operation_key,"
        "expected_previous_event_digest,hook_recovery_resolution_id,"
        "local_recovery_resolution_id,attachment_revision_digest,body_json,created_at"
    )
    connection.execute(
        f"insert into continuity_internal_event_receipt({columns}) values(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            CRASH_RECEIPT,
            CRASH_EVENT,
            "session",
            binding_digest,
            "CRASH_RECOVERED",
            "crash-recovered",
            str(tail[1]),
            HOOK_RESOLUTION_ID if producer == "hook" else None,
            "local-resolution" if producer == "local" else None,
            RECOVERY_REVISION,
            _json(body),
            NOW,
        ),
    )
    connection.execute(
        "insert into session_event values('crash-recovered','session','CRASH_RECOVERED',?,?)",
        (CRASH_EVENT, NOW),
    )
    connection.execute(
        "insert into session_event_detail values(?,?,?,?,?,?,null,'{}')",
        (
            "crash-recovered",
            "session",
            int(tail[0]) + 1,
            str(tail[1]),
            "crash-recovered",
            CRASH_EVENT,
        ),
    )


def _insert_recovery_required(
    connection: sqlite3.Connection,
    values: dict[str, str],
    *,
    producer: Literal["hook", "local"],
) -> None:
    _insert_revision(
        connection,
        revision_digest=RECOVERY_REVISION,
        revision_number=4,
        previous_revision_digest=values["frozen_revision"],
        operation_key="recovery-required",
        state="recovery-required",
        active_manifest_digest=digest("context"),
        active_hydration_receipt_digest=digest("hydration"),
        checkpoint_digest=values["checkpoint"],
        close_request_digest=values["request"],
        pre_close_event_digest=values["pre_close_event"],
        hook_recovery_case_id=HOOK_CASE_ID if producer == "hook" else None,
        local_recovery_case_id="local-case" if producer == "local" else None,
    )


def _insert_restored_frozen(
    connection: sqlite3.Connection,
    values: dict[str, str],
    *,
    producer: Literal["hook", "local"],
) -> None:
    _insert_revision(
        connection,
        revision_digest=RESTORED_REVISION,
        revision_number=5,
        previous_revision_digest=RECOVERY_REVISION,
        operation_key="restored-frozen",
        state="frozen",
        active_manifest_digest=digest("context"),
        active_hydration_receipt_digest=digest("hydration"),
        checkpoint_digest=values["checkpoint"],
        close_request_digest=values["request"],
        pre_close_event_digest=values["pre_close_event"],
        hook_recovery_case_id=HOOK_CASE_ID if producer == "hook" else None,
        hook_recovery_resolution_id=HOOK_RESOLUTION_ID if producer == "hook" else None,
        local_recovery_case_id="local-case" if producer == "local" else None,
        local_recovery_resolution_id="local-resolution" if producer == "local" else None,
        crash_recovered_event_digest=CRASH_EVENT,
        crash_recovered_receipt_digest=CRASH_RECEIPT,
    )


def _insert_closed_after_restored(
    connection: sqlite3.Connection,
    values: dict[str, str],
    *,
    producer: Literal["hook", "local"],
    revision_changes: dict[str, object] | None = None,
) -> None:
    delivery = connection.execute(
        "select state from local_outbox_delivery where outbox_id=?",
        (values["outbox"],),
    ).fetchone()
    if delivery == ("pending",):
        connection.execute(
            "update local_outbox_delivery set state='delivered',fencing_counter=1,"
            "claim_id='delivery-claim',owner_id='owner',owner_pid=101,owner_token='token',"
            "expires_at=null,updated_at=? where outbox_id=?",
            (NOW, values["outbox"]),
        )
        connection.execute(
            "insert into local_outbox_receipt values(?,?,?,?,?,?,?)",
            (
                "delivery-receipt",
                values["outbox"],
                "delivery-claim",
                1,
                "delivered",
                _sha("e"),
                NOW,
            ),
        )
    close_receipt = digest("close-receipt-after-restored")
    closed_event = digest("session-closed-after-restored")
    closed_revision = digest("closed-revision-after-restored")
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
    tail = connection.execute(
        "select sequence,event_digest from session_event_detail where session_id='session' "
        "order by sequence desc limit 1"
    ).fetchone()
    _insert_internal_event(
        connection,
        receipt_digest=digest("session-closed-receipt-after-restored"),
        event_digest=closed_event,
        event_id="session-closed-after-restored",
        event_kind="SESSION_CLOSED",
        operation_key="session-closed-after-restored",
        sequence=int(tail[0]) + 1,
        previous=str(tail[1]),
        attachment_revision_digest=RESTORED_REVISION,
        close_receipt_digest=close_receipt,
    )
    revision: dict[str, object] = {
        "revision_digest": closed_revision,
        "revision_number": 6,
        "previous_revision_digest": RESTORED_REVISION,
        "operation_key": "closed-after-restored",
        "state": "closed",
        "active_manifest_digest": digest("context"),
        "active_hydration_receipt_digest": digest("hydration"),
        "checkpoint_digest": values["checkpoint"],
        "close_request_digest": values["request"],
        "pre_close_event_digest": values["pre_close_event"],
        "close_receipt_digest": close_receipt,
        "session_closed_event_digest": closed_event,
        "hook_recovery_case_id": HOOK_CASE_ID if producer == "hook" else None,
        "hook_recovery_resolution_id": HOOK_RESOLUTION_ID if producer == "hook" else None,
        "local_recovery_case_id": "local-case" if producer == "local" else None,
        "local_recovery_resolution_id": "local-resolution" if producer == "local" else None,
        "crash_recovered_event_digest": CRASH_EVENT,
        "crash_recovered_receipt_digest": CRASH_RECEIPT,
    }
    revision.update(revision_changes or {})
    _insert_revision(connection, **revision)
    connection.execute(
        "update session set status='closed',closed_at=?,close_receipt_digest=? where id='session'",
        (NOW, close_receipt),
    )


def _seed_recovery_graph(
    path: Path, *, producer: Literal["hook", "local"]
) -> tuple[sqlite3.Connection, dict[str, str]]:
    _seed_hydrated_v4(path)
    connection = sqlite3.connect(path)
    connection.execute("pragma foreign_keys=on")
    connection.execute("begin immediate")
    values = _raw_freeze(connection)
    connection.commit()
    connection.execute("begin immediate")
    if producer == "hook":
        _hook_case(connection)
    else:
        _local_case(connection, values["outbox"])
    _insert_recovery_required(connection, values, producer=producer)
    if producer == "hook":
        _hook_resolution(connection)
    else:
        _local_resolution(connection, values["outbox"])
    _insert_crash_recovered(connection, producer=producer)
    return connection, values


@pytest.mark.parametrize("producer", ["hook", "local"])
def test_recovery_required_to_frozen_retains_original_preclose_origin(
    producer: Literal["hook", "local"], tmp_path: Path
) -> None:
    path = tmp_path / f"restored-{producer}.db"
    connection, values = _seed_recovery_graph(path, producer=producer)
    with connection:
        _insert_restored_frozen(connection, values, producer=producer)
        _insert_closed_after_restored(connection, values, producer=producer)
    assert schema._validate_connection(connection) == 4
    assert connection.execute(
        "select attachment_revision_digest from continuity_internal_event_receipt "
        "where event_kind='PRE_CLOSE'"
    ).fetchone() == (_sha("2"),)
    assert connection.execute(
        "select attachment_revision_digest from continuity_internal_event_receipt "
        "where event_kind='CRASH_RECOVERED'"
    ).fetchone() == (RECOVERY_REVISION,)
    assert connection.execute(
        "select state,previous_revision_digest,crash_recovered_receipt_digest "
        "from continuity_hook_attachment_revision where state='closed'"
    ).fetchone() == ("closed", RESTORED_REVISION, CRASH_RECEIPT)
    connection.close()


@pytest.mark.parametrize("wrong_origin", [_sha("f"), RECOVERY_REVISION])
def test_restored_frozen_rejects_rebound_preclose_origin(wrong_origin: str, tmp_path: Path) -> None:
    path = tmp_path / "wrong-origin.db"
    connection, values = _seed_recovery_graph(path, producer="hook")
    connection.execute("drop trigger continuity_internal_event_no_update")
    connection.execute(
        "update continuity_internal_event_receipt set attachment_revision_digest=? "
        "where event_kind='PRE_CLOSE'",
        (wrong_origin,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="revision evidence mismatch"):
        _insert_restored_frozen(connection, values, producer="hook")
    connection.rollback()
    assert connection.execute(
        "select count(*) from continuity_hook_attachment_revision where revision_digest=?",
        (RESTORED_REVISION,),
    ).fetchone() == (0,)
    connection.close()


@pytest.mark.parametrize(
    "revision_changes",
    [
        {"hook_recovery_case_id": None},
        {"hook_recovery_resolution_id": None},
        {"crash_recovered_event_digest": None},
        {"crash_recovered_receipt_digest": None},
    ],
    ids=("case-cleared", "resolution-cleared", "event-cleared", "receipt-cleared"),
)
def test_closed_revision_rejects_recovery_evidence_carry_drift(
    revision_changes: dict[str, object], tmp_path: Path
) -> None:
    path = tmp_path / "closed-carry-drift.db"
    connection, values = _seed_recovery_graph(path, producer="hook")
    _insert_restored_frozen(connection, values, producer="hook")
    connection.commit()
    connection.execute("begin immediate")
    with pytest.raises(sqlite3.IntegrityError, match=r"revision|evidence"):
        _insert_closed_after_restored(
            connection,
            values,
            producer="hook",
            revision_changes=revision_changes,
        )
    connection.rollback()
    assert connection.execute(
        "select count(*) from continuity_hook_attachment_revision where state='closed'"
    ).fetchone() == (0,)
    connection.close()


def test_direct_hydrated_to_frozen_still_requires_hydrated_receipt_origin(
    tmp_path: Path,
) -> None:
    path = tmp_path / "direct.db"
    _seed_hydrated_v4(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute("begin immediate")
        values = _raw_freeze(connection)
        connection.commit()
        assert connection.execute(
            "select attachment_revision_digest from continuity_internal_event_receipt "
            "where event_kind='PRE_CLOSE'"
        ).fetchone() == (_sha("2"),)
        assert connection.execute(
            "select previous_revision_digest from continuity_hook_attachment_revision "
            "where revision_digest=?",
            (values["frozen_revision"],),
        ).fetchone() == (_sha("2"),)
