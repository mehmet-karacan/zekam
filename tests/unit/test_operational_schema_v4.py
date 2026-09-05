"""Dormant native lifecycle v4 schema and immutable-v1-v3 regression gates."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from zekam.application.local_hook_command_contract import (
    NATIVE_DOUBLE_EXEC_TOPOLOGY,
    ReviewedHookCommand,
)
from zekam.domain.errors import ConfigurationError
from zekam.infrastructure.sqlite import operational_schema as schema

pytestmark = pytest.mark.unit

NOW = "2026-09-03T00:00:00+00:00"
ATTACHMENT_ID = "018f0000-0000-7000-8000-000000000001"
REALM_ID = "018f0000-0000-7000-8000-000000000002"


def _sha(char: str) -> str:
    return "sha256:" + char * 64


def _json(body: dict[str, object]) -> str:
    return json.dumps(body, separators=(",", ":"), sort_keys=True)


def _revision_body(**changes: object) -> str:
    body: dict[str, object] = {
        "active_hydration_receipt_digest": None,
        "active_manifest_digest": None,
        "attachment_id": ATTACHMENT_ID,
        "checkpoint_digest": None,
        "close_receipt_digest": None,
        "close_request_digest": None,
        "crash_recovered_event_digest": None,
        "crash_recovered_receipt_digest": None,
        "created_at": NOW,
        "hook_recovery_case_id": None,
        "hook_recovery_resolution_id": None,
        "local_recovery_case_id": None,
        "local_recovery_resolution_id": None,
        "operation_key": "attach",
        "post_compaction_event_digest": None,
        "pre_close_event_digest": None,
        "pre_compaction_event_digest": None,
        "previous_revision_digest": None,
        "process_generation_digest": _sha("e"),
        "revision_digest": _sha("1"),
        "revision_number": 1,
        "session_closed_event_digest": None,
        "state": "attached",
    }
    body.update(changes)
    return _json(body)


def _internal_body(
    *,
    event_digest: str,
    binding_digest: str,
    event_kind: str,
    operation_key: str,
    previous: str | None,
    attachment_revision_digest: str = _sha("1"),
) -> str:
    return _json(
        {
            "attachment_revision_digest": attachment_revision_digest,
            "binding_digest": binding_digest,
            "created_at": NOW,
            "event_digest": event_digest,
            "event_kind": event_kind,
            "expected_previous_event_digest": previous,
            "operation_key": operation_key,
            "session_id": "session",
        }
    )


def _attachment(connection: sqlite3.Connection) -> None:
    connection.execute("pragma foreign_keys=on")
    connection.execute(
        "insert into project(id,slug,display_name,created_at) values('project','p','P',?)",
        (NOW,),
    )
    connection.execute("insert into project_knowledge_realm values('project',?,?)", (REALM_ID, NOW))
    connection.execute(
        "insert into source_binding values('source','project','source:p','directory',1,?)",
        (NOW,),
    )
    connection.execute(
        "insert into source_snapshot values('snapshot','source','rev',?,?,?,?)",
        (_sha("1"), _sha("2"), _sha("3"), NOW),
    )
    connection.execute(
        "insert into session(id,client_id,device_id,project_id,status,opened_at)"
        " values('session','codex','device','project','open',?)",
        (NOW,),
    )
    connection.execute(
        "insert into continuity_session_binding values("
        "'session','external','project',?,null,null,'codex','device','snapshot',?,?,?,?,?)",
        (REALM_ID, _sha("4"), _sha("5"), _sha("6"), _sha("7"), NOW),
    )
    _attach_existing(connection)


def _attach_existing(connection: sqlite3.Connection) -> None:
    body = _json(
        {
            "attachment_id": ATTACHMENT_ID,
            "client_contract_digest": _sha("8"),
            "created_at": NOW,
            "hook_set_digest": _sha("a"),
            "native_artifact_digest": _sha("9"),
            "session_id": "session",
        }
    )
    connection.execute(
        "insert into continuity_hook_attachment values(?,?,?,?,?,?,?,?)",
        (
            ATTACHMENT_ID,
            "session",
            _sha("8"),
            _sha("9"),
            _sha("a"),
            _sha("b"),
            body,
            NOW,
        ),
    )


def _generation_one(connection: sqlite3.Connection) -> None:
    receipt = _json(
        {
            "ancestry_policy_digest": _sha("c"),
            "attachment_id": ATTACHMENT_ID,
            "created_at": NOW,
            "hook_set_digest": _sha("a"),
            "native_artifact_digest": _sha("9"),
            "native_pid": 101,
            "native_start_token": "start-1",
            "native_uid": 501,
            "predecessor_process_generation_digest": None,
            "transition_kind": "initial-attach",
        }
    )
    connection.execute(
        "insert into continuity_managed_process_receipt values(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _sha("d"),
            ATTACHMENT_ID,
            None,
            101,
            501,
            "start-1",
            _sha("9"),
            _sha("a"),
            _sha("c"),
            "initial-attach",
            receipt,
            NOW,
        ),
    )
    body = _json(
        {
            "ancestry_policy_digest": _sha("c"),
            "attachment_id": ATTACHMENT_ID,
            "created_at": NOW,
            "generation": 1,
            "hook_set_digest": _sha("a"),
            "managed_launch_receipt_digest": _sha("d"),
            "native_artifact_digest": _sha("9"),
            "native_pid": 101,
            "native_start_token": "start-1",
            "native_uid": 501,
            "previous_process_generation_digest": None,
        }
    )
    connection.execute(
        "insert into continuity_hook_process_generation values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _sha("e"),
            ATTACHMENT_ID,
            1,
            101,
            501,
            "start-1",
            _sha("9"),
            _sha("a"),
            _sha("c"),
            None,
            _sha("d"),
            body,
            NOW,
        ),
    )


def _initial_revision(connection: sqlite3.Connection) -> None:
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


def _names(path: Path, kind: str) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute("select name from sqlite_master where type=?", (kind,))
        }


def test_fresh_v4_is_explicit_dormant_and_does_not_change_default(tmp_path: Path) -> None:
    path = tmp_path / "fresh-v4.db"
    result = schema.bootstrap_v4(path)
    assert schema.SCHEMA_VERSION == 3
    assert result.schema_version == 4 and result.schema_ok and result.integrity_ok
    assert schema.status(path) == result
    assert len(schema.MIGRATION_LEDGER) == 3
    assert schema.MIGRATION_LEDGER == schema.V3_MIGRATION_LEDGER
    assert schema.V4_MIGRATION_LEDGER[:3] == schema.V3_MIGRATION_LEDGER
    with sqlite3.connect(path) as connection:
        for table in (
            "continuity_hook_attachment",
            "continuity_hook_process_generation",
            "continuity_managed_process_receipt",
            "continuity_hook_attachment_revision",
            "continuity_hook_recovery_case",
            "continuity_hook_recovery_resolution",
            "continuity_reviewed_hook_command",
            "continuity_hook_invocation_ancestry_receipt",
            "continuity_native_event_receipt",
            "continuity_turn_commit_receipt",
            "continuity_internal_event_receipt",
        ):
            assert connection.execute(f'select count(*) from "{table}"').fetchone() == (0,)


def test_v4_replaces_exact_four_v2_trigger_names(tmp_path: Path) -> None:
    v3 = tmp_path / "v3.db"
    schema.bootstrap(v3)
    with sqlite3.connect(v3) as connection:
        old = {
            row[0]: row[1]
            for row in connection.execute(
                "select name,sql from sqlite_master where type='trigger' and name in "
                "('continuity_closed_event_guard','continuity_event_chain_guard',"
                "'continuity_close_receipt_guard','continuity_session_close_update_guard')"
            )
        }
    v4 = tmp_path / "v4.db"
    schema.bootstrap_v4(v4)
    with sqlite3.connect(v4) as connection:
        new = {
            row[0]: row[1]
            for row in connection.execute(
                "select name,sql from sqlite_master where type='trigger' and name in "
                "('continuity_closed_event_guard','continuity_event_chain_guard',"
                "'continuity_close_receipt_guard','continuity_session_close_update_guard')"
            )
        }
    assert (
        set(new)
        == set(old)
        == {
            "continuity_closed_event_guard",
            "continuity_event_chain_guard",
            "continuity_close_receipt_guard",
            "continuity_session_close_update_guard",
        }
    )
    assert all(new[name] != old[name] for name in new)
    assert "continuity_internal_event_receipt" in new["continuity_closed_event_guard"]
    assert "continuity_hook_attachment_revision" in new["continuity_close_receipt_guard"]


@pytest.mark.parametrize("source", [1, 2, 3])
def test_default_upgrade_cannot_enter_v4(source: int, tmp_path: Path) -> None:
    path = tmp_path / f"v{source}.db"
    schema.bootstrap(path, target_version=source)
    with pytest.raises(ConfigurationError, match=r"orchestrator|required|unsupported"):
        schema.upgrade(path, target_version=4)
    assert schema.status(path).schema_version == source


def test_v4_internal_receipt_scope_rejects_missing_session_before_deferred_fk(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v4.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        assert connection.execute("pragma defer_foreign_keys").fetchone() == (0,)
        connection.execute("begin")
        with pytest.raises(sqlite3.IntegrityError, match="producer mismatch"):
            connection.execute(
                "insert into continuity_internal_event_receipt("
                "receipt_digest,event_digest,session_id,binding_digest,event_kind,operation_key,"
                "expected_previous_event_digest,turn_commit_digest,effect_claim_id,effect_receipt_id,"
                "native_event_receipt_digest,close_request_digest,close_receipt_digest,"
                "hook_recovery_resolution_id,local_recovery_resolution_id,"
                "attachment_revision_digest,body_json,created_at) values("
                "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "sha256:" + "1" * 64,
                    "sha256:" + "2" * 64,
                    "missing-session",
                    "sha256:" + "3" * 64,
                    "PRE_CLOSE",
                    "operation",
                    None,
                    None,
                    None,
                    None,
                    None,
                    "sha256:" + "4" * 64,
                    None,
                    None,
                    None,
                    None,
                    "{}",
                    "2026-09-03T00:00:00+00:00",
                ),
            )


def test_v4_schema_drift_is_read_only_rejected(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        connection.execute("drop trigger continuity_closed_event_guard")
    before = path.read_bytes()
    assert not schema.status(path).schema_ok
    assert path.read_bytes() == before


def test_fresh_v4_refuses_existing_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "existing.db"
    path.touch()
    with pytest.raises(ConfigurationError, match="empty destination"):
        schema.bootstrap_v4(path)


def test_attachment_body_scope_and_immutable_guards_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("update continuity_hook_attachment set body_json=body_json")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("delete from continuity_hook_attachment")


def test_process_generation_chain_rejects_skip_and_is_append_only(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        _generation_one(connection)
        connection.execute(
            "insert into continuity_managed_process_receipt values(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _sha("f"),
                ATTACHMENT_ID,
                _sha("e"),
                102,
                501,
                "start-2",
                _sha("9"),
                _sha("a"),
                _sha("c"),
                "orderly-reattach",
                _json(
                    {
                        "ancestry_policy_digest": _sha("c"),
                        "attachment_id": ATTACHMENT_ID,
                        "created_at": NOW,
                        "hook_set_digest": _sha("a"),
                        "native_artifact_digest": _sha("9"),
                        "native_pid": 102,
                        "native_start_token": "start-2",
                        "native_uid": 501,
                        "predecessor_process_generation_digest": _sha("e"),
                        "transition_kind": "orderly-reattach",
                    }
                ),
                NOW,
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="generation chain"):
            connection.execute(
                "insert into continuity_hook_process_generation values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _sha("0"),
                    ATTACHMENT_ID,
                    3,
                    102,
                    501,
                    "start-2",
                    _sha("9"),
                    _sha("a"),
                    _sha("c"),
                    _sha("e"),
                    _sha("f"),
                    _json(
                        {
                            "ancestry_policy_digest": _sha("c"),
                            "attachment_id": ATTACHMENT_ID,
                            "created_at": NOW,
                            "generation": 3,
                            "hook_set_digest": _sha("a"),
                            "managed_launch_receipt_digest": _sha("f"),
                            "native_artifact_digest": _sha("9"),
                            "native_pid": 102,
                            "native_start_token": "start-2",
                            "native_uid": 501,
                            "previous_process_generation_digest": _sha("e"),
                        }
                    ),
                    NOW,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "update continuity_hook_process_generation set native_pid=native_pid"
            )


def test_revision_state_and_bound_session_status_reject_illegal_shortcuts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "v4.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        _generation_one(connection)
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
        with pytest.raises(
            sqlite3.IntegrityError, match=r"transition|exact invariant|evidence mismatch"
        ):
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
                    "close-shortcut",
                    "closed",
                    _sha("e"),
                    _revision_body(
                        revision_digest=_sha("2"),
                        revision_number=2,
                        previous_revision_digest=_sha("1"),
                        operation_key="close-shortcut",
                        state="closed",
                    ),
                    NOW,
                ),
            )
        with pytest.raises(sqlite3.IntegrityError, match="terminal transition"):
            connection.execute("update session set status='closing' where id='session'")
        with pytest.raises(sqlite3.IntegrityError, match="terminal transition"):
            connection.execute(
                "update session set status='closed',closed_at=?,close_receipt_digest=?"
                " where id='session'",
                (NOW, _sha("3")),
            )


def test_recovery_case_scope_and_resolution_are_unique_append_only(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        _generation_one(connection)
        with pytest.raises(sqlite3.IntegrityError, match="recovery scope"):
            connection.execute(
                "insert into continuity_hook_recovery_case values(?,?,?,?,?,?,?,?)",
                (
                    "018f0000-0000-7000-8000-000000000003",
                    ATTACHMENT_ID,
                    "foreign-session",
                    _sha("e"),
                    "source-drift",
                    _sha("f"),
                    _json(
                        {
                            "attachment_id": ATTACHMENT_ID,
                            "case_kind": "source-drift",
                            "created_at": NOW,
                            "evidence_digest": _sha("f"),
                            "process_generation_digest": _sha("e"),
                            "recovery_case_id": "018f0000-0000-7000-8000-000000000003",
                            "session_id": "foreign-session",
                        }
                    ),
                    NOW,
                ),
            )


def _v3_fixture_promoted_for_ddl_test(path: Path) -> None:
    from tests.unit.test_operational_schema_v3 import _source

    _source(path, 3)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute("begin immediate")
        schema._apply_migration(connection, 4)
        connection.commit()
        _attach_existing(connection)
        _generation_one(connection)
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
    assert schema.status(path).schema_version == 4


def _ancestry_values(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "receipt_digest": _sha("5"),
        "process_generation_digest": _sha("e"),
        "delivery_id": "native-delivery",
        "topology": NATIVE_DOUBLE_EXEC_TOPOLOGY,
        "launch_command_digest": changes.pop("launch_command_digest", None),
        "external_event_type": "SessionStart",
        "ancestry_policy_digest": _sha("c"),
        "native_pid": 101,
        "native_start_token": "start-1",
        "native_uid": 501,
        "native_artifact_digest": _sha("9"),
        "shell_pid": 202,
        "shell_start_token": "shell-start",
        "shell_uid": 501,
        "shell_parent_pid": 101,
        "shell_parent_start_token": "start-1",
        "shell_parent_uid": 501,
        "shell_artifact_digest": _sha("2"),
        "hook_pid": 202,
        "hook_start_token": "shell-start",
        "hook_uid": 501,
        "hook_parent_pid": 101,
        "hook_parent_start_token": "start-1",
        "hook_parent_uid": 501,
        "python_launcher_artifact_digest": _sha("3"),
        "python_runtime_artifact_digest": _sha("4"),
        "observation_digest": _sha("6"),
        "observed_at": NOW,
        "grants_authority": 0,
        "approval_inherited": 0,
    }
    values.update(changes)
    return values


def _ancestry_body(values: dict[str, object], **changes: object) -> str:
    body = {key: value for key, value in values.items() if key != "receipt_digest"}
    body["schema"] = "zekam-hook-invocation-ancestry-receipt/v1"
    body.update(changes)
    return _json(body)


def _insert_ancestry(
    connection: sqlite3.Connection,
    *,
    changes: dict[str, object] | None = None,
    body_changes: dict[str, object] | None = None,
) -> dict[str, object]:
    values = _ancestry_values(**(changes or {}))
    if values["launch_command_digest"] is None:
        command = _insert_reviewed_command(connection, "SessionStart")
        values["launch_command_digest"] = command.command_digest
    columns = tuple(values)
    connection.execute(
        "insert into continuity_hook_invocation_ancestry_receipt("
        + ",".join(columns)
        + ",body_json) values("
        + ",".join("?" for _ in range(len(columns) + 1))
        + ")",
        (*values.values(), _ancestry_body(values, **(body_changes or {}))),
    )
    return values


def _reviewed_command(event_type: str) -> ReviewedHookCommand:
    event_offset = {
        "SessionStart": ("2", "3", "4"),
        "PreCompact": ("6", "7", "8"),
        "PostCompact": ("d", "f", "0"),
    }[event_type]
    return ReviewedHookCommand(
        attachment_id=ATTACHMENT_ID,
        external_event_type=event_type,
        topology=NATIVE_DOUBLE_EXEC_TOPOLOGY,
        client_contract_digest=_sha("8"),
        hook_set_digest=_sha("a"),
        shell_artifact_digest=_sha(event_offset[0]),
        python_launcher_artifact_digest=_sha(event_offset[1]),
        python_runtime_artifact_digest=_sha(event_offset[2]),
        argv_recipe_digest=_sha("6"),
        sandbox_profile_digest=_sha("7"),
        created_at=NOW,
    )


def _insert_reviewed_command(
    connection: sqlite3.Connection, event_type: str
) -> ReviewedHookCommand:
    command = _reviewed_command(event_type)
    existing = connection.execute(
        "select command_digest from continuity_reviewed_hook_command "
        "where attachment_id=? and external_event_type=?",
        (command.attachment_id, event_type),
    ).fetchone()
    if existing is not None:
        assert existing == (command.command_digest,)
        return command
    connection.execute(
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
            _json(command.body()),
            command.created_at,
            0,
            0,
        ),
    )
    return command


def _native_body(values: dict[str, object], **changes: object) -> str:
    body = {key: value for key, value in values.items() if key != "receipt_digest"}
    body.update(changes)
    return _json(body)


def _insert_native_start(
    connection: sqlite3.Connection,
    ancestry: dict[str, object],
    *,
    native_changes: dict[str, object] | None = None,
    body_changes: dict[str, object] | None = None,
) -> dict[str, object]:
    from zekam.domain.canonical import digest

    values: dict[str, object] = {
        "receipt_digest": _sha("f"),
        "event_digest": _sha("8"),
        "attachment_revision_digest": _sha("1"),
        "process_generation_digest": ancestry["process_generation_digest"],
        "ancestry_receipt_digest": ancestry["receipt_digest"],
        "spool_digest": _sha("0"),
        "previous_spool_digest": None,
        "observation_digest": ancestry["observation_digest"],
        "delivery_id": ancestry["delivery_id"],
        "spool_sequence": 1,
        "external_event_type": "SessionStart",
        "internal_event_type": "SESSION_START",
        "external_turn_id": None,
        "external_trigger_id": None,
        "shell_pid": ancestry["shell_pid"],
        "shell_uid": ancestry["shell_uid"],
        "shell_start_token": ancestry["shell_start_token"],
        "hook_pid": ancestry["hook_pid"],
        "hook_uid": ancestry["hook_uid"],
        "hook_start_token": ancestry["hook_start_token"],
        "shell_artifact_digest": ancestry["shell_artifact_digest"],
        "python_launcher_artifact_digest": ancestry["python_launcher_artifact_digest"],
        "python_runtime_artifact_digest": ancestry["python_runtime_artifact_digest"],
        "hydration_receipt_digest": digest("hydration"),
        "grants_authority": 0,
        "approval_inherited": 0,
        "created_at": NOW,
    }
    values.update(native_changes or {})
    columns = tuple(values)
    connection.execute(
        "insert into continuity_native_event_receipt("
        + ",".join(columns)
        + ",body_json) values("
        + ",".join("?" for _ in range(len(columns) + 1))
        + ")",
        (*values.values(), _native_body(values, **(body_changes or {}))),
    )
    return values


def _preclose_receipt(connection: sqlite3.Connection) -> tuple[str, str]:
    event_digest = _sha("2")
    request_digest = _sha("3")
    binding_digest = str(
        connection.execute(
            "select binding_digest from continuity_session_binding where session_id='session'"
        ).fetchone()[0]
    )
    previous_digest = str(
        connection.execute(
            "select event_digest from session_event_detail where session_id='session'"
            " order by sequence desc limit 1"
        ).fetchone()[0]
    )
    connection.execute(
        "insert into continuity_internal_event_receipt("
        "receipt_digest,event_digest,session_id,binding_digest,event_kind,operation_key,"
        "expected_previous_event_digest,turn_commit_digest,effect_claim_id,effect_receipt_id,"
        "native_event_receipt_digest,close_request_digest,close_receipt_digest,"
        "hook_recovery_resolution_id,local_recovery_resolution_id,"
        "attachment_revision_digest,body_json,created_at) values("
        "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _sha("4"),
            event_digest,
            "session",
            binding_digest,
            "PRE_CLOSE",
            "pre-close",
            previous_digest,
            None,
            None,
            None,
            None,
            request_digest,
            None,
            None,
            None,
            _sha("1"),
            _internal_body(
                event_digest=event_digest,
                binding_digest=binding_digest,
                event_kind="PRE_CLOSE",
                operation_key="pre-close",
                previous=previous_digest,
            ),
            NOW,
        ),
    )
    return event_digest, request_digest


def test_deferred_event_and_close_request_cycle_commits_without_global_defer(
    tmp_path: Path,
) -> None:
    path = tmp_path / "cycle.db"
    _v3_fixture_promoted_for_ddl_test(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        assert connection.execute("pragma defer_foreign_keys").fetchone() == (0,)
        connection.execute("begin")
        event_digest, request_digest = _preclose_receipt(connection)
        checkpoint_digest = str(
            connection.execute(
                "select checkpoint_digest from continuity_checkpoint where session_id='session'"
            ).fetchone()[0]
        )
        connection.execute(
            "insert into continuity_close_request values(?,'session',?,1,'{}',?)",
            (request_digest, checkpoint_digest, NOW),
        )
        connection.execute(
            "insert into session_event values('pre-close','session','PRE_CLOSE',?,?)",
            (event_digest, NOW),
        )
        previous_digest = str(
            connection.execute(
                "select event_digest from session_event_detail where session_id='session'"
                " order by sequence desc limit 1"
            ).fetchone()[0]
        )
        connection.execute(
            "insert into session_event_detail values("
            "'pre-close','session',2,?,'pre-close-key',?,?,'{}')",
            (previous_digest, event_digest, _sha("8")),
        )
        connection.commit()
    assert schema.status(path).schema_ok


def test_deferred_cycle_orphan_rolls_back_at_commit(tmp_path: Path) -> None:
    path = tmp_path / "orphan.db"
    _v3_fixture_promoted_for_ddl_test(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute("begin")
        _preclose_receipt(connection)
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.commit()
        connection.rollback()
        assert connection.execute(
            "select count(*) from continuity_internal_event_receipt"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    ("event_kind", "role"),
    [
        ("USER_TURN_COMMITTED", "user"),
        ("ASSISTANT_TURN_COMMITTED", "assistant"),
    ],
)
def test_canonical_turn_kinds_require_matching_trusted_commit_receipt(
    tmp_path: Path, event_kind: str, role: str
) -> None:
    path = tmp_path / f"{role}.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        _generation_one(connection)
        _initial_revision(connection)
        binding_digest = str(
            connection.execute(
                "select binding_digest from continuity_session_binding where session_id='session'"
            ).fetchone()[0]
        )
        connection.execute(
            "insert into continuity_turn_commit_receipt values(?,?,?,?,?,?,?,?,?,?)",
            (
                _sha("c"),
                "session",
                binding_digest,
                role,
                f"item-{role}",
                _sha("4"),
                _sha("5"),
                None,
                _json(
                    {
                        "binding_digest": binding_digest,
                        "content_digest": _sha("4"),
                        "created_at": NOW,
                        "item_ref": f"item-{role}",
                        "previous_turn_commit_digest": None,
                        "role": role,
                        "session_id": "session",
                        "store_generation_digest": _sha("5"),
                    }
                ),
                NOW,
            ),
        )
        connection.commit()
        connection.execute("begin")
        connection.execute(
            "insert into continuity_internal_event_receipt("
            "receipt_digest,event_digest,session_id,binding_digest,event_kind,operation_key,"
            "expected_previous_event_digest,turn_commit_digest,effect_claim_id,effect_receipt_id,"
            "native_event_receipt_digest,close_request_digest,close_receipt_digest,"
            "hook_recovery_resolution_id,local_recovery_resolution_id,"
            "attachment_revision_digest,body_json,created_at) values("
            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                _sha("3"),
                _sha("2"),
                "session",
                binding_digest,
                event_kind,
                f"commit-{role}",
                None,
                _sha("c"),
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                _sha("1"),
                _internal_body(
                    event_digest=_sha("2"),
                    binding_digest=binding_digest,
                    event_kind=event_kind,
                    operation_key=f"commit-{role}",
                    previous=None,
                ),
                NOW,
            ),
        )
        connection.execute(
            "insert into session_event values('turn','session',?,?,?)",
            (event_kind, _sha("2"), NOW),
        )
        connection.execute(
            "insert into session_event_detail values("
            "'turn','session',1,null,'turn-key',?,null,'{}')",
            (_sha("2"),),
        )
        connection.commit()
        assert connection.execute(
            "select event_kind from session_event where id='turn'"
        ).fetchone() == (event_kind,)


@pytest.mark.parametrize(
    "short_kind", ["USER", "ASSISTANT", "TOOL_CLAIMED", "TOOL_COMPLETED", "UNKNOWN"]
)
def test_short_and_unknown_internal_event_kinds_are_rejected_without_bypass(
    tmp_path: Path, short_kind: str
) -> None:
    path = tmp_path / "short.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        with pytest.raises(sqlite3.IntegrityError, match=r"CHECK constraint|producer mismatch"):
            connection.execute(
                "insert into continuity_internal_event_receipt("
                "receipt_digest,event_digest,session_id,binding_digest,event_kind,operation_key,"
                "expected_previous_event_digest,turn_commit_digest,effect_claim_id,effect_receipt_id,"
                "native_event_receipt_digest,close_request_digest,close_receipt_digest,"
                "hook_recovery_resolution_id,local_recovery_resolution_id,"
                "attachment_revision_digest,body_json,created_at) values("
                "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _sha("3"),
                    _sha("2"),
                    "missing",
                    _sha("4"),
                    short_kind,
                    "short",
                    None,
                    _sha("5"),
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    _sha("1"),
                    "{}",
                    NOW,
                ),
            )


def test_canonical_tool_kinds_require_exact_claim_binding_and_terminal_receipt(
    tmp_path: Path,
) -> None:
    path = tmp_path / "tool.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        _generation_one(connection)
        _initial_revision(connection)
        binding_digest = str(
            connection.execute(
                "select binding_digest from continuity_session_binding where session_id='session'"
            ).fetchone()[0]
        )
        payload = _json(
            {
                "binding_digest": binding_digest,
                "operation": "continuity.compile",
                "run_id": None,
                "session_id": "session",
            }
        )
        connection.execute(
            "insert into local_job(id,idempotency_key,payload_json,state,max_attempts,"
            "available_at,created_at,updated_at) values('job','job-key',?,'running',1,?,?,?)",
            (payload, NOW, NOW, NOW),
        )
        connection.execute(
            "insert into local_lease values('lease','job','owner',101,'token',1,?,?)",
            (NOW, NOW),
        )
        connection.execute(
            "insert into local_effect_claim values("
            "'claim','job','lease',1,'continuity.compile',?,'effect-key',?)",
            (_sha("4"), NOW),
        )
        connection.execute(
            "insert into continuity_effect_binding values('claim','session','job',?)",
            (_sha("5"),),
        )
        for ordinal, (kind, producer_column, producer_id) in enumerate(
            (
                ("TOOL_EFFECT_CLAIMED", "effect_claim_id", "claim"),
                ("TOOL_EFFECT_COMPLETED", "effect_receipt_id", "effect-receipt"),
            ),
            start=1,
        ):
            if ordinal == 2:
                connection.execute(
                    "insert into local_effect_receipt values("
                    "'effect-receipt','claim','completed',?,?)",
                    (_sha("6"), NOW),
                )
            event_digest = _sha(("7", "8")[ordinal - 1])
            receipt_digest = _sha(("9", "0")[ordinal - 1])
            previous = None if ordinal == 1 else _sha("7")
            columns = [
                "receipt_digest",
                "event_digest",
                "session_id",
                "binding_digest",
                "event_kind",
                "operation_key",
                "expected_previous_event_digest",
                producer_column,
                "attachment_revision_digest",
                "body_json",
                "created_at",
            ]
            connection.execute(
                f"insert into continuity_internal_event_receipt({','.join(columns)})"
                " values(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_digest,
                    event_digest,
                    "session",
                    binding_digest,
                    kind,
                    f"tool-{ordinal}",
                    previous,
                    producer_id,
                    _sha("1"),
                    _internal_body(
                        event_digest=event_digest,
                        binding_digest=binding_digest,
                        event_kind=kind,
                        operation_key=f"tool-{ordinal}",
                        previous=previous,
                    ),
                    NOW,
                ),
            )
            connection.execute(
                "insert into session_event values(?,?,?,?,?)",
                (f"tool-{ordinal}", "session", kind, event_digest, NOW),
            )
            connection.execute(
                "insert into session_event_detail values(?,?,?,?,?,?,null,'{}')",
                (
                    f"tool-{ordinal}",
                    "session",
                    ordinal,
                    previous,
                    f"tool-key-{ordinal}",
                    event_digest,
                ),
            )
        assert connection.execute(
            "select event_kind from session_event order by rowid"
        ).fetchall() == [("TOOL_EFFECT_CLAIMED",), ("TOOL_EFFECT_COMPLETED",)]


def test_exact_ancestry_native_receipt_event_detail_transaction_commits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ancestry-happy.db"
    _v3_fixture_promoted_for_ddl_test(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute("begin")
        ancestry = _insert_ancestry(connection)
        native = _insert_native_start(connection, ancestry)
        previous = connection.execute(
            "select event_digest from session_event_detail where session_id='session'"
            " order by sequence desc limit 1"
        ).fetchone()[0]
        connection.execute(
            "insert into session_event values('native-start','session','SESSION_START',?,?)",
            (native["event_digest"], NOW),
        )
        connection.execute(
            "insert into session_event_detail values("
            "'native-start','session',2,?,'native-start',?,?, '{}')",
            (previous, native["event_digest"], native["spool_digest"]),
        )
        connection.commit()
        assert connection.execute(
            "select topology,launch_command_digest from continuity_hook_invocation_ancestry_receipt"
        ).fetchone() == (
            "native-fork-shell-exec-launcher-exec-runtime/v1",
            _reviewed_command("SessionStart").command_digest,
        )
        assert connection.execute(
            "select ancestry_receipt_digest,python_launcher_artifact_digest,"
            "python_runtime_artifact_digest from continuity_native_event_receipt"
        ).fetchone() == (_sha("5"), _sha("3"), _sha("4"))


@pytest.mark.parametrize(
    "changes",
    [
        {"native_pid": 999},
        {"native_start_token": "wrong"},
        {"native_uid": 999},
        {"native_artifact_digest": _sha("1")},
        {"ancestry_policy_digest": _sha("1")},
        {"shell_pid": 101, "hook_pid": 101},
        {"shell_parent_pid": 999, "hook_parent_pid": 999},
        {"shell_parent_start_token": "wrong", "hook_parent_start_token": "wrong"},
        {"shell_parent_uid": 999, "hook_parent_uid": 999},
        {"shell_uid": 999, "hook_uid": 999},
        {"hook_pid": 303},
        {"hook_start_token": "wrong"},
        {"hook_uid": 999},
        {"shell_artifact_digest": _sha("9")},
        {"python_launcher_artifact_digest": _sha("2")},
        {"python_runtime_artifact_digest": _sha("3")},
        {"topology": "native-exec-runtime/v0"},
        {"grants_authority": 1},
        {"approval_inherited": 1},
    ],
)
def test_ancestry_rejects_generation_topology_identity_and_authority_drift(
    changes: dict[str, object], tmp_path: Path
) -> None:
    path = tmp_path / "ancestry-drift.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        _generation_one(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_ancestry(connection, changes=changes)
        assert connection.execute(
            "select count(*) from continuity_hook_invocation_ancestry_receipt"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "body_changes",
    [
        {"unreviewed": "secret"},
        {"schema": "zekam-hook-invocation-ancestry-receipt/v2"},
        {"delivery_id": "other"},
        {"observed_at": "not-utc"},
    ],
)
def test_ancestry_body_is_exact_canonical_and_column_bound(
    body_changes: dict[str, object], tmp_path: Path
) -> None:
    path = tmp_path / "ancestry-body.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        _generation_one(connection)
        with pytest.raises(sqlite3.IntegrityError, match="ancestry mismatch"):
            _insert_ancestry(connection, body_changes=body_changes)


def test_ancestry_timestamp_delivery_replay_and_immutability_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ancestry-replay.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        _generation_one(connection)
        _insert_ancestry(connection)
        for statement in (
            "update continuity_hook_invocation_ancestry_receipt set delivery_id=delivery_id",
            "delete from continuity_hook_invocation_ancestry_receipt",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(statement)
        with pytest.raises(sqlite3.IntegrityError, match=r"UNIQUE|unique"):
            _insert_ancestry(connection, changes={"receipt_digest": _sha("8")})
        with pytest.raises(sqlite3.IntegrityError, match="ancestry mismatch"):
            _insert_ancestry(
                connection,
                changes={
                    "receipt_digest": _sha("8"),
                    "delivery_id": "other-delivery",
                    "observed_at": "2026-09-03T03:00:00+03:00",
                },
            )


def test_native_receipt_requires_exact_ancestry_join_and_distinct_artifacts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "native-join.db"
    _v3_fixture_promoted_for_ddl_test(path)
    with sqlite3.connect(path) as connection:
        ancestry = _insert_ancestry(connection)
        native_drifts: tuple[dict[str, object], ...] = (
            {"delivery_id": "wrong"},
            {"observation_digest": _sha("1")},
            {"shell_pid": 303, "hook_pid": 303},
            {"shell_artifact_digest": _sha("1")},
            {"python_launcher_artifact_digest": _sha("1")},
            {"python_runtime_artifact_digest": _sha("1")},
            {"process_generation_digest": _sha("1")},
        )
        for changes in native_drifts:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_native_start(connection, ancestry, native_changes=changes)
        assert connection.execute(
            "select count(*) from continuity_native_event_receipt"
        ).fetchone() == (0,)


def test_ancestry_rejects_free_wrong_event_and_unreviewed_command_roles(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ancestry-command.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        _generation_one(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_ancestry(
                connection,
                changes={"launch_command_digest": _sha("1")},
            )
        precompact = _insert_reviewed_command(connection, "PreCompact")
        with pytest.raises(sqlite3.IntegrityError, match="ancestry mismatch"):
            _insert_ancestry(
                connection,
                changes={"launch_command_digest": precompact.command_digest},
            )
        session_start = _insert_reviewed_command(connection, "SessionStart")
        with pytest.raises(sqlite3.IntegrityError, match="ancestry mismatch"):
            _insert_ancestry(
                connection,
                changes={
                    "launch_command_digest": session_start.command_digest,
                    "shell_artifact_digest": _sha("6"),
                },
            )
        assert connection.execute(
            "select count(*) from continuity_hook_invocation_ancestry_receipt"
        ).fetchone() == (0,)


def test_native_receipt_orphan_ancestry_and_transaction_crash_leave_no_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "native-crash.db"
    _v3_fixture_promoted_for_ddl_test(path)
    with sqlite3.connect(path) as connection:
        connection.execute("pragma foreign_keys=on")
        connection.execute("begin")
        ancestry = _ancestry_values()
        with pytest.raises(sqlite3.IntegrityError):
            _insert_native_start(connection, ancestry)
        connection.rollback()
        connection.execute("begin")
        ancestry = _insert_ancestry(connection)
        _insert_native_start(connection, ancestry)
        connection.rollback()
        assert connection.execute(
            "select count(*) from continuity_hook_invocation_ancestry_receipt"
        ).fetchone() == (0,)
        assert connection.execute(
            "select count(*) from continuity_native_event_receipt"
        ).fetchone() == (0,)
        assert connection.execute(
            "select count(*) from continuity_reviewed_hook_command"
        ).fetchone() == (0,)

        _insert_reviewed_command(connection, "SessionStart")
        connection.commit()
        connection.execute("begin")
        ancestry = _insert_ancestry(connection)
        _insert_native_start(connection, ancestry)
        connection.rollback()
        assert connection.execute(
            "select count(*) from continuity_reviewed_hook_command"
        ).fetchone() == (1,)
        assert connection.execute(
            "select count(*) from continuity_hook_invocation_ancestry_receipt"
        ).fetchone() == (0,)


def test_v4_native_schema_has_no_ambiguous_legacy_python_artifact_column(
    tmp_path: Path,
) -> None:
    path = tmp_path / "native-columns.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute("pragma table_info(continuity_native_event_receipt)")
        }
    assert "python_artifact_digest" not in columns
    assert {
        "python_launcher_artifact_digest",
        "python_runtime_artifact_digest",
        "ancestry_receipt_digest",
    } <= columns


@pytest.mark.parametrize(
    ("label", "migration_digest", "schema_digest"),
    [
        (
            "draft",
            schema.REJECTED_DRAFT_V4_MIGRATION_DIGEST,
            schema.REJECTED_DRAFT_V4_SCHEMA_DIGEST,
        ),
        (
            "unreachable-recovery",
            schema.REJECTED_UNREACHABLE_RECOVERY_V4_MIGRATION_DIGEST,
            schema.REJECTED_UNREACHABLE_RECOVERY_V4_SCHEMA_DIGEST,
        ),
        (
            "partial-restored-frozen",
            schema.REJECTED_PARTIAL_RESTORED_FROZEN_V4_MIGRATION_DIGEST,
            schema.REJECTED_PARTIAL_RESTORED_FROZEN_V4_SCHEMA_DIGEST,
        ),
    ],
)
@pytest.mark.parametrize("surface", ["migration-ledger", "revision-ledger", "metadata"])
def test_rejected_unsafe_v4_fingerprint_is_explicitly_refused(
    label: str,
    migration_digest: str,
    schema_digest: str,
    surface: str,
    tmp_path: Path,
) -> None:
    path = tmp_path / f"stale-{label}-{surface}.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        if surface in {"migration-ledger", "revision-ledger"}:
            table = "schema_migration" if surface == "migration-ledger" else "schema_revision"
            connection.execute(
                f"update {table} set checksum=? where version=4",
                (migration_digest,),
            )
        else:
            connection.execute(
                "update zekam_meta set value=? where key='schema_digest'",
                (schema_digest,),
            )
        with pytest.raises(ConfigurationError, match="rejected unsafe dormant-v4"):
            schema._validate_connection(connection)
    assert not schema.status(path).schema_ok


def test_v1_v2_v3_immutable_digests_remain_exact_after_ancestry_correction() -> None:
    assert schema.V1_MIGRATION_DIGEST == (
        "sha256:d91114ad970241a779d183f9646616b6d5b04d0af8d2e01451473a0c5d6d769e"
    )
    assert schema.V2_MIGRATION_DIGEST == (
        "sha256:a4efb21d80a634c6fe8b42030c19d7ec25de2cc8b6bafeb230cec09744aaafaf"
    )
    assert schema.V3_MIGRATION_DIGEST == (
        "sha256:888535556d91c344720573fb1efb23f4f058b4a509debf557fb052e7a8f439fc"
    )
    assert schema.SCHEMA_DIGEST == (
        "sha256:3a9c5586b334148166e1e9670f07930d775e81f347e87274a2d0846e12be8533"
    )
