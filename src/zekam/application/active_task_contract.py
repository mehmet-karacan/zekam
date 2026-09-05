"""Living active-task authority and its read-only generated projection.

``AKTIF_GOREV.md`` is the sole scope authority.  The YAML file is deliberately
small and contains only enough metadata to bind an operational plan to the
exact Markdown bytes; it cannot carry independent work state or authority.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from zekam.domain.canonical import digest_of_bytes, parse_digest
from zekam.domain.errors import ValidationFailed

TASK_SCHEMA: Final = "zekam-active-task/v2"
TASK_STATUS: Final = "APPROVED_ACTIVE_TASK"
PROJECTION_SCHEMA: Final = "zekam-active-task-projection/v1"
GENERATOR_VERSION: Final = "active-task-contract/v1"
AUTHORITY_REF: Final = "AKTIF_GOREV.md"
MAX_TASK_BYTES: Final = 4 * 1024 * 1024

_TASK_FIELDS: Final = frozenset(
    {
        "schema",
        "task_id",
        "status",
        "title",
        "created_at",
        "baseline_repository",
        "baseline_branch",
        "baseline_head",
        "legacy_postgresql_data_import",
        "postgresql_runtime_dependency",
        "docker_required_for_zekam_core",
        "push_authorized",
    }
)
_TASK_ID = re.compile(r"[A-Z][A-Z0-9-]{7,127}\Z")
_GIT_HEAD = re.compile(r"[0-9a-f]{40}\Z")


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ValidationFailed("Projection anahtarlari metin olmali")
        if key in result:
            raise ValidationFailed(f"Projection duplicate alan iceriyor: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _front_matter(document: str) -> dict[str, str]:
    if not document.startswith("---\n"):
        raise ValidationFailed("Aktif gorev YAML front matter ile baslamali")
    boundary = document.find("\n---\n", 4)
    if boundary < 0:
        raise ValidationFailed("Aktif gorev front matter kapanisi eksik")
    values: dict[str, str] = {}
    for line_number, line in enumerate(document[4:boundary].splitlines(), start=2):
        if not line or line.startswith((" ", "\t", "#")) or ":" not in line:
            raise ValidationFailed(f"Aktif gorev front matter satiri gecersiz: {line_number}")
        key, raw_value = line.split(":", maxsplit=1)
        key = key.strip()
        value = raw_value.strip()
        if not key or not value:
            raise ValidationFailed(f"Aktif gorev alani bos: {line_number}")
        if key in values:
            raise ValidationFailed(f"Aktif gorev duplicate alan iceriyor: {key}")
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    unknown = set(values) - _TASK_FIELDS
    missing = _TASK_FIELDS - set(values)
    if unknown:
        raise ValidationFailed(f"Aktif gorev bilinmeyen alan iceriyor: {sorted(unknown)[0]}")
    if missing:
        raise ValidationFailed(f"Aktif gorev zorunlu alani eksik: {sorted(missing)[0]}")
    return values


def _parse_false(value: str, field: str) -> bool:
    if value != "false":
        raise ValidationFailed(f"Aktif gorev {field} yalniz false olmali")
    return False


def _required_text(value: str, field: str) -> str:
    if not value.strip() or value.strip().lower() in {"null", "~"}:
        raise ValidationFailed(f"Aktif gorev {field} bos olamaz")
    return value


@dataclass(frozen=True, slots=True)
class ActiveTaskContract:
    """Validated identity of the exact living task document."""

    task_id: str
    title: str
    created_at: str
    baseline_repository: str
    baseline_branch: str
    baseline_head: str
    source_digest: str
    legacy_postgresql_data_import: str
    postgresql_runtime_dependency: str
    docker_required_for_zekam_core: bool
    push_authorized: bool

    @classmethod
    def load(cls, path: Path) -> ActiveTaskContract:
        return cls.from_bytes(path.read_bytes())

    @classmethod
    def from_bytes(cls, payload: bytes) -> ActiveTaskContract:
        """Validate an already bounded snapshot without reopening its source."""
        if not isinstance(payload, bytes):
            raise ValidationFailed("Aktif gorev payload bytes olmali")
        if not payload or len(payload) > MAX_TASK_BYTES:
            raise ValidationFailed("Aktif gorev bos veya boyut sinirini asiyor")
        try:
            document = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationFailed("Aktif gorev strict UTF-8 olmali") from exc
        values = _front_matter(document)
        if values["schema"] != TASK_SCHEMA or values["status"] != TASK_STATUS:
            raise ValidationFailed("Aktif gorev schema/status onayli contract degil")
        if not _TASK_ID.fullmatch(values["task_id"]):
            raise ValidationFailed("Aktif gorev task_id canonical degil")
        title = _required_text(values["title"], "title")
        try:
            created_at = dt.datetime.fromisoformat(values["created_at"])
        except ValueError as exc:
            raise ValidationFailed("Aktif gorev created_at ISO-8601 olmali") from exc
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValidationFailed("Aktif gorev created_at timezone tasimali")
        if not _GIT_HEAD.fullmatch(values["baseline_head"]):
            raise ValidationFailed("Aktif gorev baseline_head 40 kucuk hex olmali")
        for field in ("baseline_repository", "baseline_branch"):
            _required_text(values[field], field)
        if values["legacy_postgresql_data_import"] != "FORBIDDEN":
            raise ValidationFailed("Legacy PostgreSQL veri importu FORBIDDEN olmali")
        if values["postgresql_runtime_dependency"] != "FORBIDDEN":
            raise ValidationFailed("PostgreSQL runtime dependency FORBIDDEN olmali")
        return cls(
            task_id=values["task_id"],
            title=title,
            created_at=values["created_at"],
            baseline_repository=values["baseline_repository"],
            baseline_branch=values["baseline_branch"],
            baseline_head=values["baseline_head"],
            source_digest=digest_of_bytes(payload),
            legacy_postgresql_data_import=values["legacy_postgresql_data_import"],
            postgresql_runtime_dependency=values["postgresql_runtime_dependency"],
            docker_required_for_zekam_core=_parse_false(
                values["docker_required_for_zekam_core"], "docker_required_for_zekam_core"
            ),
            push_authorized=_parse_false(values["push_authorized"], "push_authorized"),
        )

    def projection(self) -> dict[str, Any]:
        return {
            "schema": PROJECTION_SCHEMA,
            "generator_version": GENERATOR_VERSION,
            "generated": True,
            "authority_ref": AUTHORITY_REF,
            "authority_digest": self.source_digest,
            "task_id": self.task_id,
            "status": TASK_STATUS,
            "title": self.title,
            "created_at": self.created_at,
            "baseline_repository": self.baseline_repository,
            "baseline_branch": self.baseline_branch,
            "baseline_head": self.baseline_head,
            "legacy_postgresql_data_import": self.legacy_postgresql_data_import,
            "postgresql_runtime_dependency": self.postgresql_runtime_dependency,
            "docker_required_for_zekam_core": self.docker_required_for_zekam_core,
            "push_authorized": self.push_authorized,
            "read_only": True,
            "grants_authority": False,
            "approval_inherited": False,
        }

    def render_projection(self) -> str:
        return yaml.safe_dump(
            self.projection(), allow_unicode=True, sort_keys=False, default_flow_style=False
        )

    def verify_projection(self, path: Path) -> None:
        try:
            value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ValidationFailed("Aktif gorev projection okunamadi") from exc
        if not isinstance(value, dict) or value != self.projection():
            raise ValidationFailed("AKTIF_GOREV.yaml authority digest veya projection drift")
        parse_digest(str(value["authority_digest"]))
