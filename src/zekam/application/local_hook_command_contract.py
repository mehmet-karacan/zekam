"""Authority-free canonical values for reviewed native hook commands."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from zekam.domain.canonical import canonical_json, digest, parse_digest
from zekam.domain.errors import ValidationFailed

REVIEWED_HOOK_COMMAND_SCHEMA = "zekam-reviewed-hook-command/v1"
NATIVE_DOUBLE_EXEC_TOPOLOGY = "native-fork-shell-exec-launcher-exec-runtime/v1"
REVIEWED_HOOK_EVENT_TYPES = ("SessionStart", "PreCompact", "PostCompact")
MAX_REVIEWED_HOOK_COMMAND_BYTES = 32768
_BODY_KEYS = frozenset(
    {
        "schema",
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
    }
)


def _text(value: object, label: str) -> str:
    if type(value) is not str:
        raise ValidationFailed(f"Reviewed hook command {label} text required")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationFailed(f"Reviewed hook command {label} UTF-8 required") from exc
    return value


def _digest(value: object, label: str) -> str:
    text = _text(value, label)
    try:
        parse_digest(text)
    except ValidationFailed as exc:
        raise ValidationFailed(f"Reviewed hook command {label} digest required") from exc
    return text


def _uuid(value: object) -> str:
    text = _text(value, "attachment")
    try:
        if str(UUID(text)) != text:
            raise ValueError
    except ValueError as exc:
        raise ValidationFailed("Reviewed hook command canonical attachment UUID required") from exc
    return text


def _timestamp(value: object) -> str:
    text = _text(value, "timestamp")
    try:
        parsed = dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S+00:00").replace(tzinfo=dt.UTC)
        if parsed.isoformat(timespec="seconds") != text:
            raise ValueError
    except ValueError as exc:
        raise ValidationFailed("Reviewed hook command canonical UTC seconds required") from exc
    return text


@dataclass(frozen=True, slots=True)
class ReviewedHookCommand:
    attachment_id: str
    external_event_type: str
    topology: str
    client_contract_digest: str
    hook_set_digest: str
    shell_artifact_digest: str
    python_launcher_artifact_digest: str
    python_runtime_artifact_digest: str
    argv_recipe_digest: str
    sandbox_profile_digest: str
    created_at: str
    grants_authority: bool = False
    approval_inherited: bool = False

    def __post_init__(self) -> None:
        _uuid(self.attachment_id)
        event_type = _text(self.external_event_type, "event type")
        if event_type not in REVIEWED_HOOK_EVENT_TYPES:
            raise ValidationFailed("Reviewed hook command event type is unsupported")
        if _text(self.topology, "topology") != NATIVE_DOUBLE_EXEC_TOPOLOGY:
            raise ValidationFailed("Reviewed hook command topology is unsupported")
        for name in (
            "client_contract_digest",
            "hook_set_digest",
            "shell_artifact_digest",
            "python_launcher_artifact_digest",
            "python_runtime_artifact_digest",
            "argv_recipe_digest",
            "sandbox_profile_digest",
        ):
            _digest(getattr(self, name), name)
        _timestamp(self.created_at)
        if type(self.grants_authority) is not bool or self.grants_authority:
            raise ValidationFailed("Reviewed hook command cannot grant authority")
        if type(self.approval_inherited) is not bool or self.approval_inherited:
            raise ValidationFailed("Reviewed hook command cannot inherit approval")
        encoded = canonical_json(self.body()).encode("utf-8")
        if not 1 <= len(encoded) <= MAX_REVIEWED_HOOK_COMMAND_BYTES:
            raise ValidationFailed("Reviewed hook command canonical body outside bounds")

    def body(self) -> dict[str, object]:
        return {
            "approval_inherited": self.approval_inherited,
            "argv_recipe_digest": self.argv_recipe_digest,
            "attachment_id": self.attachment_id,
            "client_contract_digest": self.client_contract_digest,
            "created_at": self.created_at,
            "external_event_type": self.external_event_type,
            "grants_authority": self.grants_authority,
            "hook_set_digest": self.hook_set_digest,
            "python_launcher_artifact_digest": self.python_launcher_artifact_digest,
            "python_runtime_artifact_digest": self.python_runtime_artifact_digest,
            "sandbox_profile_digest": self.sandbox_profile_digest,
            "schema": REVIEWED_HOOK_COMMAND_SCHEMA,
            "shell_artifact_digest": self.shell_artifact_digest,
            "topology": self.topology,
        }

    @property
    def command_digest(self) -> str:
        return digest(self.body())

    @classmethod
    def from_body(cls, body: object) -> ReviewedHookCommand:
        if type(body) is not dict or set(body) != _BODY_KEYS:
            raise ValidationFailed("Reviewed hook command exact object required")
        if body.get("schema") != REVIEWED_HOOK_COMMAND_SCHEMA:
            raise ValidationFailed("Reviewed hook command schema is unsupported")
        return cls(
            attachment_id=body["attachment_id"],
            external_event_type=body["external_event_type"],
            topology=body["topology"],
            client_contract_digest=body["client_contract_digest"],
            hook_set_digest=body["hook_set_digest"],
            shell_artifact_digest=body["shell_artifact_digest"],
            python_launcher_artifact_digest=body["python_launcher_artifact_digest"],
            python_runtime_artifact_digest=body["python_runtime_artifact_digest"],
            argv_recipe_digest=body["argv_recipe_digest"],
            sandbox_profile_digest=body["sandbox_profile_digest"],
            created_at=body["created_at"],
            grants_authority=body["grants_authority"],
            approval_inherited=body["approval_inherited"],
        )

    @classmethod
    def from_json(cls, payload: object) -> ReviewedHookCommand:
        if type(payload) is not bytes or not 1 <= len(payload) <= MAX_REVIEWED_HOOK_COMMAND_BYTES:
            raise ValidationFailed("Reviewed hook command bounded bytes required")

        def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValidationFailed("Reviewed hook command duplicate JSON key")
                result[key] = value
            return result

        def reject_constant(_value: str) -> None:
            raise ValidationFailed("Reviewed hook command nonfinite JSON value")

        try:
            decoded = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=reject_duplicate,
                parse_constant=reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValidationFailed("Reviewed hook command strict JSON required") from exc
        command = cls.from_body(decoded)
        if canonical_json(command.body()).encode("utf-8") != payload:
            raise ValidationFailed("Reviewed hook command canonical JSON bytes required")
        return command
