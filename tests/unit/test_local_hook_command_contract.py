"""Canonical reviewed-command value and read-only dormant-v4 verifier gates."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from tests.unit.test_operational_schema_v4 import (
    ATTACHMENT_ID,
    _attachment,
    _insert_reviewed_command,
    _json,
    _reviewed_command,
    _sha,
)

from zekam.application.local_hook_command_contract import (
    MAX_REVIEWED_HOOK_COMMAND_BYTES,
    NATIVE_DOUBLE_EXEC_TOPOLOGY,
    REVIEWED_HOOK_COMMAND_SCHEMA,
    REVIEWED_HOOK_EVENT_TYPES,
    ReviewedHookCommand,
)
from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.infrastructure.sqlite import operational_schema as schema
from zekam.infrastructure.sqlite.continuity_native_verifier import (
    verify_reviewed_hook_commands,
)

pytestmark = pytest.mark.unit


def _in_transaction(connection: sqlite3.Connection) -> bool:
    return connection.in_transaction


def _body(**changes: object) -> dict[str, object]:
    body = _reviewed_command("SessionStart").body()
    body.update(changes)
    return body


def _seed_commands(path: Path) -> None:
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        for event_type in REVIEWED_HOOK_EVENT_TYPES:
            _insert_reviewed_command(connection, event_type)


def test_reviewed_command_body_and_digest_are_exact_and_authority_free() -> None:
    command = _reviewed_command("SessionStart")
    assert command.body() == _body()
    assert command.command_digest == digest(command.body())
    assert len(canonical_json(command.body()).encode()) <= MAX_REVIEWED_HOOK_COMMAND_BYTES
    assert command.body()["schema"] == REVIEWED_HOOK_COMMAND_SCHEMA
    assert command.body()["topology"] == NATIVE_DOUBLE_EXEC_TOPOLOGY
    assert command.body()["grants_authority"] is False
    assert command.body()["approval_inherited"] is False
    assert ReviewedHookCommand.from_body(command.body()) == command
    assert ReviewedHookCommand.from_json(canonical_json(command.body()).encode()) == command


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "zekam-reviewed-hook-command/v2"),
        ("attachment_id", None),
        ("attachment_id", "NOT-A-UUID"),
        ("external_event_type", "SessionEnd"),
        ("external_event_type", 1),
        ("topology", "fork-child"),
        ("client_contract_digest", _sha("G")),
        ("hook_set_digest", None),
        ("shell_artifact_digest", ""),
        ("python_launcher_artifact_digest", True),
        ("python_runtime_artifact_digest", _sha("A")),
        ("argv_recipe_digest", _sha("z")),
        ("sandbox_profile_digest", 1),
        ("created_at", "2026-09-03T03:00:00+03:00"),
        ("created_at", "2026-09-03T00:00:00.1+00:00"),
        ("grants_authority", 0),
        ("grants_authority", True),
        ("approval_inherited", 0),
        ("approval_inherited", True),
    ],
)
def test_reviewed_command_rejects_wrong_null_noncanonical_and_authority_values(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationFailed):
        ReviewedHookCommand.from_body(_body(**{field: value}))


def test_from_body_rejects_nonexact_mapping_missing_extra_and_subclass() -> None:
    class MappingSubclass(dict[str, object]):
        pass

    with pytest.raises(ValidationFailed, match="exact object"):
        ReviewedHookCommand.from_body(None)
    with pytest.raises(ValidationFailed, match="exact object"):
        ReviewedHookCommand.from_body({**_body(), "extra": "secret"})
    missing = _body()
    missing.pop("schema")
    with pytest.raises(ValidationFailed, match="exact object"):
        ReviewedHookCommand.from_body(missing)
    with pytest.raises(ValidationFailed, match="exact object"):
        ReviewedHookCommand.from_body(MappingSubclass(_body()))


@pytest.mark.parametrize("payload", ["{}", bytearray(b"{}"), memoryview(b"{}"), True, None])
def test_from_json_accepts_only_exact_bounded_bytes(payload: object) -> None:
    with pytest.raises(ValidationFailed, match="bounded bytes"):
        ReviewedHookCommand.from_json(payload)
    with pytest.raises(ValidationFailed, match="bounded bytes"):
        ReviewedHookCommand.from_json(b"")
    with pytest.raises(ValidationFailed, match="bounded bytes"):
        ReviewedHookCommand.from_json(b" " * (MAX_REVIEWED_HOOK_COMMAND_BYTES + 1))


def test_from_json_rejects_duplicate_nonfinite_invalid_utf8_and_noncanonical_bytes() -> None:
    canonical = canonical_json(_body()).encode()
    duplicate = canonical[:-1] + b',"schema":"zekam-reviewed-hook-command/v1"}'
    for payload, message in (
        (duplicate, "duplicate"),
        (b'{"value":NaN}', "nonfinite"),
        (b"\xff", "strict JSON"),
        (json.dumps(_body(), indent=2, sort_keys=True).encode(), "canonical JSON"),
    ):
        with pytest.raises(ValidationFailed, match=message):
            ReviewedHookCommand.from_json(payload)
    with pytest.raises(ValidationFailed):
        ReviewedHookCommand.from_json(b"[" * 2000 + b"]" * 2000)


def test_sqlite_reviewed_command_rows_are_exact_and_append_only(tmp_path: Path) -> None:
    path = tmp_path / "commands.db"
    _seed_commands(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "select external_event_type from continuity_reviewed_hook_command "
            "order by case external_event_type when 'SessionStart' then 1 "
            "when 'PreCompact' then 2 else 3 end"
        ).fetchall() == [("SessionStart",), ("PreCompact",), ("PostCompact",)]
        for statement in (
            "update continuity_reviewed_hook_command set body_json=body_json",
            "delete from continuity_reviewed_hook_command",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(statement)


def test_sqlite_command_rejects_wrong_attachment_contract_and_nonboolean_body(
    tmp_path: Path,
) -> None:
    path = tmp_path / "command-scope.db"
    schema.bootstrap_v4(path)
    with sqlite3.connect(path) as connection:
        _attachment(connection)
        command = _reviewed_command("SessionStart")
        values = [
            command.command_digest,
            command.attachment_id,
            command.external_event_type,
            command.topology,
            _sha("1"),
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
        ]
        with pytest.raises(sqlite3.IntegrityError, match="command mismatch"):
            connection.execute(
                "insert into continuity_reviewed_hook_command values("
                "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
        numeric_body = dict(command.body())
        numeric_body["grants_authority"] = 0
        values[4] = command.client_contract_digest
        values[11] = _json(numeric_body)
        with pytest.raises(sqlite3.IntegrityError, match="command mismatch"):
            connection.execute(
                "insert into continuity_reviewed_hook_command values("
                "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )


def test_read_only_verifier_returns_fixed_order_without_owning_caller_transaction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "verify.db"
    _seed_commands(path)
    with sqlite3.connect(path) as connection:
        assert not _in_transaction(connection)
        original_factory = connection.row_factory
        commands = verify_reviewed_hook_commands(connection, ATTACHMENT_ID)
        assert tuple(command.external_event_type for command in commands) == (
            "SessionStart",
            "PreCompact",
            "PostCompact",
        )
        assert not _in_transaction(connection)
        assert connection.row_factory is original_factory
        connection.execute("begin")
        assert verify_reviewed_hook_commands(connection, ATTACHMENT_ID) == commands
        assert _in_transaction(connection)
        connection.rollback()


def test_verifier_rejects_missing_command_and_forged_digest(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    schema.bootstrap_v4(missing)
    with sqlite3.connect(missing) as connection:
        _attachment(connection)
        _insert_reviewed_command(connection, "SessionStart")
        with pytest.raises(ConfigurationError, match="exact event set"):
            verify_reviewed_hook_commands(connection, ATTACHMENT_ID)

    forged = tmp_path / "forged.db"
    _seed_commands(forged)
    with sqlite3.connect(forged) as connection:
        connection.execute("drop trigger continuity_reviewed_hook_command_no_update")
        connection.execute(
            "update continuity_reviewed_hook_command set command_digest=? "
            "where external_event_type='SessionStart'",
            (_sha("0"),),
        )
        with pytest.raises(ConfigurationError, match="column parity"):
            verify_reviewed_hook_commands(connection, ATTACHMENT_ID)


@pytest.mark.parametrize(
    ("sql_value", "message"),
    [
        ("cast(x'ff' as text)", "strict JSON"),
        ("'{}'", "exact object"),
        ("printf('%.*c',32769,32)", "bounds"),
    ],
)
def test_verifier_prefetch_bounds_then_rejects_corrupt_body_without_echo(
    sql_value: str, message: str, tmp_path: Path
) -> None:
    path = tmp_path / "corrupt.db"
    _seed_commands(path)
    with sqlite3.connect(path) as connection:
        connection.execute("drop trigger continuity_reviewed_hook_command_no_update")
        connection.execute("pragma ignore_check_constraints=on")
        connection.execute(
            "update continuity_reviewed_hook_command set body_json="
            + sql_value
            + " where external_event_type='SessionStart'"
        )
        with pytest.raises((ConfigurationError, ValidationFailed), match=message):
            verify_reviewed_hook_commands(connection, ATTACHMENT_ID)


def test_verifier_rejects_wrong_argument_types_without_transaction_side_effect() -> None:
    connection = sqlite3.connect(":memory:")
    try:
        for bad in (None, "", 1, True, ATTACHMENT_ID.upper()):
            with pytest.raises(ValidationFailed):
                verify_reviewed_hook_commands(connection, bad)
            assert not connection.in_transaction
        with pytest.raises(ValidationFailed, match="connection"):
            verify_reviewed_hook_commands(object(), ATTACHMENT_ID)  # type: ignore[arg-type]
    finally:
        connection.close()
