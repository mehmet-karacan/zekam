"""Read-only verifier for dormant-v4 reviewed native hook command rows."""

from __future__ import annotations

import sqlite3
from typing import Any
from uuid import UUID

from zekam.application.local_hook_command_contract import (
    MAX_REVIEWED_HOOK_COMMAND_BYTES,
    REVIEWED_HOOK_EVENT_TYPES,
    ReviewedHookCommand,
)
from zekam.domain.errors import ConfigurationError, ValidationFailed

_SCALAR_COLUMNS = (
    "command_digest",
    "attachment_id",
    "external_event_type",
    "topology",
    "client_contract_digest",
    "hook_set_digest",
    "shell_artifact_digest",
    "python_launcher_artifact_digest",
    "python_runtime_artifact_digest",
    "argv_recipe_digest",
    "sandbox_profile_digest",
    "created_at",
    "grants_authority",
    "approval_inherited",
)


def _attachment(value: object) -> str:
    if type(value) is not str:
        raise ValidationFailed("Reviewed hook command verifier canonical attachment required")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError as exc:
        raise ValidationFailed(
            "Reviewed hook command verifier canonical attachment required"
        ) from exc
    return value


def _verify_row(row: sqlite3.Row, payload: bytes) -> ReviewedHookCommand:
    command = ReviewedHookCommand.from_json(payload)
    expected: dict[str, Any] = {
        "command_digest": command.command_digest,
        "attachment_id": command.attachment_id,
        "external_event_type": command.external_event_type,
        "topology": command.topology,
        "client_contract_digest": command.client_contract_digest,
        "hook_set_digest": command.hook_set_digest,
        "shell_artifact_digest": command.shell_artifact_digest,
        "python_launcher_artifact_digest": command.python_launcher_artifact_digest,
        "python_runtime_artifact_digest": command.python_runtime_artifact_digest,
        "argv_recipe_digest": command.argv_recipe_digest,
        "sandbox_profile_digest": command.sandbox_profile_digest,
        "created_at": command.created_at,
        "grants_authority": 0,
        "approval_inherited": 0,
    }
    if any(row[column] != value for column, value in expected.items()):
        raise ConfigurationError("Reviewed hook command stored column parity mismatch")
    if (
        row["attachment_client_contract_digest"] != command.client_contract_digest
        or row["attachment_hook_set_digest"] != command.hook_set_digest
    ):
        raise ConfigurationError("Reviewed hook command attachment scope mismatch")
    return command


def verify_reviewed_hook_commands(
    connection: sqlite3.Connection, attachment_id: object
) -> tuple[ReviewedHookCommand, ReviewedHookCommand, ReviewedHookCommand]:
    """Verify all three exact reviewed commands in one pinned read snapshot."""
    if type(connection) is not sqlite3.Connection:
        raise ValidationFailed("Reviewed hook command SQLite connection required")
    attachment = _attachment(attachment_id)
    owns_transaction = not connection.in_transaction
    original_factory = connection.row_factory
    try:
        if owns_transaction:
            connection.execute("begin")
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "select c.command_digest,c.attachment_id,c.external_event_type,c.topology,"
            "c.client_contract_digest,c.hook_set_digest,c.shell_artifact_digest,"
            "c.python_launcher_artifact_digest,c.python_runtime_artifact_digest,"
            "c.argv_recipe_digest,c.sandbox_profile_digest,c.created_at,"
            "c.grants_authority,c.approval_inherited,typeof(c.body_json) as body_type,"
            "length(cast(c.body_json as blob)) as body_size,"
            "a.client_contract_digest as attachment_client_contract_digest,"
            "a.hook_set_digest as attachment_hook_set_digest "
            "from continuity_reviewed_hook_command c "
            "join continuity_hook_attachment a on a.attachment_id=c.attachment_id "
            "where c.attachment_id=? order by case c.external_event_type "
            "when 'SessionStart' then 1 when 'PreCompact' then 2 "
            "when 'PostCompact' then 3 else 4 end",
            (attachment,),
        ).fetchall()
        if (
            len(rows) != 3
            or tuple(row["external_event_type"] for row in rows) != REVIEWED_HOOK_EVENT_TYPES
        ):
            raise ConfigurationError("Reviewed hook command exact event set required")
        commands: list[ReviewedHookCommand] = []
        for row in rows:
            size = row["body_size"]
            if (
                row["body_type"] != "text"
                or type(size) is not int
                or not 1 <= size <= MAX_REVIEWED_HOOK_COMMAND_BYTES
            ):
                raise ConfigurationError("Reviewed hook command stored body bounds invalid")
            body_row = connection.execute(
                "select cast(body_json as blob) as body_blob,"
                "length(cast(body_json as blob)) as body_size "
                "from continuity_reviewed_hook_command where command_digest=? "
                "and attachment_id=? and typeof(body_json)='text' "
                "and length(cast(body_json as blob)) between 1 and ?",
                (row["command_digest"], attachment, MAX_REVIEWED_HOOK_COMMAND_BYTES),
            ).fetchone()
            if body_row is None:
                raise ConfigurationError("Reviewed hook command stored body unavailable")
            payload = body_row["body_blob"]
            if type(payload) is not bytes or body_row["body_size"] != size or len(payload) != size:
                raise ConfigurationError("Reviewed hook command body snapshot mismatch")
            commands.append(_verify_row(row, payload))
        return commands[0], commands[1], commands[2]
    except (sqlite3.Error, ValidationFailed) as exc:
        if isinstance(exc, ValidationFailed):
            raise
        raise ConfigurationError("Reviewed hook command verification failed") from exc
    finally:
        connection.row_factory = original_factory
        if owns_transaction and connection.in_transaction:
            connection.rollback()
