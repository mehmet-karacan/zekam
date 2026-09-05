from __future__ import annotations

import builtins
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from zekam.application.local_continuity import digest_text, uuid_text
from zekam.application.local_continuity_source_plan import (
    CapturedSourceFile,
    ContinuitySourcePlan,
    ContinuitySourceRecipe,
)
from zekam.domain.canonical import canonical_json
from zekam.domain.errors import PolicyViolation, ValidationFailed

PORTABLE_RECORD_SCHEMA: Final = "zekam-continuity-source-plan-record/v1"
LOCAL_REVISION_SCHEMA: Final = "zekam-local-source-binding-revision/v1"
OPERATIONAL_IDENTITY_SCHEMA: Final = "zekam-operational-file-identity/v1"
ROOT_SCHEMA: Final = "zekam-local-source-root/v1"
OPERATIONAL_SCHEMA_DIGEST: Final = (
    "sha256:e3dd4973ffd2af800d40e513d0ec42a4f87f12ce1b49648053833e631f6bf2e0"
)
MAX_PORTABLE_PLAN_BYTES: Final = 32768
MAX_LOCAL_BODY_BYTES: Final = 8192
MAX_COMMAND_BYTES: Final = 65536
BACKUP_RESTORE_READY: Final = False
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z")


def _source_authority_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _source_authority_timestamp(value: object) -> str:
    if type(value) is not str:
        raise PolicyViolation("Local source authority timestamp drift")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        raise PolicyViolation("Local source authority timestamp drift") from None
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != value:
        raise PolicyViolation("Local source authority timestamp drift")
    return value


class _SourceAuthorityReplay(Exception):
    pass


def authority_digest(domain: str, body: object) -> str:
    if type(domain) is not str or not domain.isascii() or not domain:
        raise ValidationFailed("Source authority fixed digest domain required")
    payload = domain.encode("ascii") + b"\0" + canonical_json(body).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValidationFailed("Source authority duplicate JSON key")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise ValidationFailed("Source authority nonfinite JSON rejected")


def _walk(value: object, *, depth: int = 0) -> None:
    if depth > 8:
        raise ValidationFailed("Source authority JSON nesting exceeded")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if not -(2**63) <= value <= 2**63 - 1:
            raise ValidationFailed("Source authority integer bound exceeded")
        return
    if type(value) is float:
        raise ValidationFailed("Source authority floating point rejected")
    if type(value) is str:
        if unicodedata.normalize("NFC", value) != value:
            raise ValidationFailed("Source authority non-canonical Unicode rejected")
        if any(
            ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
            for char in value
        ):
            raise ValidationFailed("Source authority control character rejected")
        return
    if type(value) is list:
        if len(value) > 32:
            raise ValidationFailed("Source authority array bound exceeded")
        for item in value:
            _walk(item, depth=depth + 1)
        return
    if type(value) is dict:
        if len(value) > 64 or any(type(key) is not str for key in value):
            raise ValidationFailed("Source authority object bound exceeded")
        for key, item in value.items():
            _walk(key, depth=depth + 1)
            _walk(item, depth=depth + 1)
        return
    raise ValidationFailed("Source authority unsupported JSON type")


def strict_json(raw: bytes, *, maximum: int) -> dict[str, Any]:
    if type(raw) is not bytes or not 1 <= len(raw) <= maximum:
        raise ValidationFailed("Source authority bounded JSON bytes required")
    try:
        text = raw.decode("utf-8")
        parsed = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError):
        raise ValidationFailed("Source authority canonical JSON required") from None
    _walk(parsed)
    if type(parsed) is not dict or canonical_json(parsed).encode("utf-8") != raw:
        raise ValidationFailed("Source authority exact canonical JSON object required")
    return parsed


def _keys(body: dict[str, Any], expected: set[str]) -> None:
    if set(body) != expected:
        raise ValidationFailed("Source authority exact object keys required")


def _text(value: object, label: str, maximum: int) -> str:
    if type(value) is not str or not 1 <= len(value.encode("utf-8")) <= maximum:
        raise ValidationFailed(f"{label} bounded text required")
    _walk(value)
    return value


def _integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValidationFailed(f"{label} exact integer required")
    return value


def _reconstruct_plan(body: object) -> ContinuitySourcePlan:
    if type(body) is not dict:
        raise ValidationFailed("Source plan exact object required")
    plan_body: dict[str, Any] = body
    _keys(
        plan_body,
        {
            "schema",
            "recipe",
            "revision_ref",
            "files",
            "ignore_digests",
            "secret_policy_digest",
            "grants_authority",
            "atomic_filesystem_snapshot",
        },
    )
    recipe_body = plan_body["recipe"]
    if type(recipe_body) is not dict:
        raise ValidationFailed("Source recipe exact object required")
    _keys(
        recipe_body,
        {
            "schema",
            "project_id",
            "realm_id",
            "source_binding_id",
            "task_digest",
            "policy_digest",
            "allowed_paths",
            "max_file_bytes",
            "max_total_bytes",
            "max_ignore_bytes",
            "git_scope",
            "git_external_config",
            "git_layout",
            "git_local_excludes",
            "tree_scope",
            "secret_scan",
            "custom_ignore_syntax",
            "grants_authority",
        },
    )
    paths = recipe_body["allowed_paths"]
    if type(paths) is not list or any(type(path) is not str for path in paths):
        raise ValidationFailed("Source recipe exact path list required")
    recipe = ContinuitySourceRecipe(
        recipe_body["project_id"],
        recipe_body["realm_id"],
        recipe_body["source_binding_id"],
        tuple(paths),
        recipe_body["task_digest"],
        recipe_body["policy_digest"],
    )
    if recipe.body() != recipe_body:
        raise ValidationFailed("Source recipe canonical body mismatch")
    files_body = plan_body["files"]
    if type(files_body) is not list:
        raise ValidationFailed("Source plan exact file list required")
    files: list[CapturedSourceFile] = []
    for item in files_body:
        if type(item) is not dict:
            raise ValidationFailed("Source plan exact file object required")
        _keys(item, {"path", "content_digest", "size_bytes"})
        files.append(CapturedSourceFile(item["path"], item["content_digest"], item["size_bytes"]))
    ignores_body = plan_body["ignore_digests"]
    if type(ignores_body) is not list:
        raise ValidationFailed("Source plan exact ignore list required")
    ignores: list[tuple[str, str | None]] = []
    for item in ignores_body:
        if (
            type(item) is not list
            or len(item) != 2
            or type(item[0]) is not str
            or (item[1] is not None and type(item[1]) is not str)
        ):
            raise ValidationFailed("Source plan exact ignore pair required")
        ignores.append((item[0], item[1]))
    plan = ContinuitySourcePlan(
        recipe,
        plan_body["revision_ref"],
        tuple(files),
        tuple(ignores),
        plan_body["secret_policy_digest"],
    )
    if canonical_json(plan.body()).encode("utf-8") != canonical_json(plan_body).encode("utf-8"):
        raise ValidationFailed("Source plan canonical reconstruction mismatch")
    return plan


@dataclass(frozen=True, slots=True)
class PortableSourcePlanRecord:
    source_snapshot_id: str
    plan: ContinuitySourcePlan

    def __post_init__(self) -> None:
        uuid_text(self.source_snapshot_id, "Source snapshot")
        if type(self.plan) is not ContinuitySourcePlan:
            raise ValidationFailed("Typed source plan required")
        self.plan.__post_init__()

    def body(self) -> dict[str, Any]:
        self.__post_init__()
        return {
            "schema": PORTABLE_RECORD_SCHEMA,
            "source_snapshot_id": self.source_snapshot_id,
            "plan_body": self.plan.body(),
            "plan_config_digest": self.plan.config_digest,
            "plan_tree_digest": self.plan.tree_digest,
            "plan_content_digest": self.plan.content_digest,
            "grants_authority": False,
            "approval_inherited": False,
        }

    def bytes(self) -> bytes:
        raw = canonical_json(self.body()).encode("utf-8")
        if len(raw) > MAX_PORTABLE_PLAN_BYTES:
            raise ValidationFailed("Portable source plan byte bound exceeded")
        return raw

    @classmethod
    def from_bytes(cls, raw: builtins.bytes) -> PortableSourcePlanRecord:
        body = strict_json(raw, maximum=MAX_PORTABLE_PLAN_BYTES)
        _keys(
            body,
            {
                "schema",
                "source_snapshot_id",
                "plan_body",
                "plan_config_digest",
                "plan_tree_digest",
                "plan_content_digest",
                "grants_authority",
                "approval_inherited",
            },
        )
        if (
            body["schema"] != PORTABLE_RECORD_SCHEMA
            or type(body["grants_authority"]) is not bool
            or body["grants_authority"]
            or type(body["approval_inherited"]) is not bool
            or body["approval_inherited"]
        ):
            raise ValidationFailed("Portable source plan fixed fields required")
        plan = _reconstruct_plan(body["plan_body"])
        result = cls(body["source_snapshot_id"], plan)
        if canonical_json(result.body()).encode("utf-8") != raw or result.bytes() != raw:
            raise ValidationFailed("Portable source plan digest or body mismatch")
        return result


@dataclass(frozen=True, slots=True)
class FileIdentity:
    dev: int
    ino: int
    uid: int
    gid: int
    mode: int
    nlink: int
    birthtime_ns: int

    def __post_init__(self) -> None:
        for name in ("dev", "uid", "gid", "mode", "birthtime_ns"):
            _integer(getattr(self, name), name)
        _integer(self.ino, "ino", minimum=1)
        _integer(self.nlink, "nlink", minimum=1)

    def body(self) -> dict[str, int]:
        self.__post_init__()
        return {name: getattr(self, name) for name in self.__slots__}


@dataclass(frozen=True, slots=True)
class LocalBindingRevision:
    device_id: str
    local_instance_id: str
    operational_identity: FileIdentity
    parent_chain_digest: str
    project_id: str
    source_binding_id: str
    root_path: str
    root_identity: FileIdentity
    portable_plan_digest: str
    previous_revision_digest: str | None
    generation: int
    created_at: str

    def __post_init__(self) -> None:
        _text(self.device_id, "Device", 128)
        uuid_text(self.local_instance_id, "Local instance")
        uuid_text(self.project_id, "Project")
        uuid_text(self.source_binding_id, "Source binding")
        digest_text(self.parent_chain_digest)
        digest_text(self.portable_plan_digest)
        if self.previous_revision_digest is not None:
            digest_text(self.previous_revision_digest)
        _integer(self.generation, "Generation", minimum=1)
        if self.generation > 64 or (self.generation == 1) != (
            self.previous_revision_digest is None
        ):
            raise ValidationFailed("Local source revision predecessor mismatch")
        _text(self.root_path, "Source root", 4096)
        if not Path(self.root_path).is_absolute() or ".." in Path(self.root_path).parts:
            raise ValidationFailed("Local source exact absolute root required")
        if (
            type(self.operational_identity) is not FileIdentity
            or type(self.root_identity) is not FileIdentity
        ):
            raise ValidationFailed("Local source typed file identities required")
        if not _UTC.fullmatch(self.created_at):
            raise ValidationFailed("Local source exact UTC timestamp required")

    def operational_body(self) -> dict[str, Any]:
        return {
            "schema": OPERATIONAL_IDENTITY_SCHEMA,
            "local_instance_id": self.local_instance_id,
            **self.operational_identity.body(),
            "parent_chain_digest": self.parent_chain_digest,
            "operational_schema_digest": OPERATIONAL_SCHEMA_DIGEST,
        }

    def root_path_digest(self) -> str:
        return authority_digest(
            "zekam.local-source-root.v1",
            {
                "schema": ROOT_SCHEMA,
                "local_instance_id": self.local_instance_id,
                "root_path": self.root_path,
            },
        )

    def body(self) -> dict[str, Any]:
        self.__post_init__()
        return {
            "schema": LOCAL_REVISION_SCHEMA,
            "device_id": self.device_id,
            "local_instance_id": self.local_instance_id,
            "operational_identity": self.operational_body(),
            "operational_identity_digest": authority_digest(
                "zekam.operational-file-identity.v1", self.operational_body()
            ),
            "project_id": self.project_id,
            "source_binding_id": self.source_binding_id,
            "root": {
                "path": self.root_path,
                "path_digest": self.root_path_digest(),
                **self.root_identity.body(),
            },
            "portable_plan_digest": self.portable_plan_digest,
            "previous_revision_digest": self.previous_revision_digest,
            "generation": self.generation,
            "created_at": self.created_at,
            "grants_authority": False,
            "approval_inherited": False,
        }

    @property
    def revision_digest(self) -> str:
        return authority_digest("zekam.local-source-binding-revision.v1", self.body())

    @property
    def body_json(self) -> str:
        raw = canonical_json(self.body())
        if len(raw.encode("utf-8")) > MAX_LOCAL_BODY_BYTES:
            raise ValidationFailed("Local source revision byte bound exceeded")
        return raw


@dataclass(frozen=True, slots=True)
class SourceAuthorityResult:
    generation: int
    revision_digest: str

    def body(self) -> dict[str, object]:
        _integer(self.generation, "Generation", minimum=1)
        if self.generation > 64:
            raise ValidationFailed("Generation bound exceeded")
        digest_text(self.revision_digest)
        return {
            "state": "bound",
            "generation": self.generation,
            "revision_digest": self.revision_digest,
            "backup_restore_ready": BACKUP_RESTORE_READY,
            "grants_authority": False,
            "approval_inherited": False,
        }
