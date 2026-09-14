"""Provider-free Agent Skills package parsing and canonical manifests."""

from __future__ import annotations

import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

import yaml

from zekam.domain.canonical import digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed

MAX_FILES = 128
MAX_FILE_BYTES = 256 * 1024
MAX_PACKAGE_BYTES = 1024 * 1024
MAX_SKILL_MD_BYTES = 256 * 1024
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_SECRET_PATTERN = re.compile(
    rb"(?:-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"
    rb"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
    rb"|\bgh[pousr]_[A-Za-z0-9]{30,}\b"
    rb"|\bxox[baprs]-[A-Za-z0-9-]{10,}\b"
    rb"|\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|"
    rb"client[_-]?secret|password|passwd|credential)\b\s*[:=]\s*['\"]?[^\s'\"]{8,})",
    re.IGNORECASE,
)
_SECRET_FILE_NAMES = frozenset({".env", "id_rsa", "id_ed25519", "credentials.json"})
_WINDOWS_RESERVED_STEMS = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_FRONTMATTER_FIELDS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(
    loader: _UniqueLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValidationFailed("Skill frontmatter duplicate/non-text key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


def _canonical_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw or ":" in raw:
        raise PolicyViolation("Skill package path unsafe")
    if unicodedata.normalize("NFC", raw) != raw:
        raise PolicyViolation("Skill package path must use canonical Unicode NFC")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PolicyViolation("Skill package path traversal/absolute path rejected")
    for part in path.parts:
        if (
            part[-1] in {" ", "."}
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            or part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED_STEMS
        ):
            raise PolicyViolation("Skill package path is not portable to Windows")
    normalized = path.as_posix()
    if normalized.startswith("/") or len(normalized.encode("utf-8")) > 240:
        raise PolicyViolation("Skill package path bound exceeded")
    return normalized


def _frontmatter(payload: bytes) -> tuple[dict[str, Any], str]:
    if not payload or len(payload) > MAX_SKILL_MD_BYTES or payload.startswith(b"\xef\xbb\xbf"):
        raise ValidationFailed("SKILL.md empty, BOM-prefixed or too large")
    try:
        document = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationFailed("SKILL.md strict UTF-8 required") from exc
    if "\r" in document.replace("\r\n", ""):
        raise ValidationFailed("SKILL.md line endings invalid")
    document = document.replace("\r\n", "\n")
    if not document.startswith("---\n"):
        raise ValidationFailed("SKILL.md YAML frontmatter required")
    boundary = document.find("\n---\n", 4)
    if boundary < 0:
        raise ValidationFailed("SKILL.md frontmatter closing boundary missing")
    try:
        values = yaml.load(document[4:boundary], Loader=_UniqueLoader)
    except yaml.YAMLError as exc:
        raise ValidationFailed("SKILL.md frontmatter invalid YAML") from exc
    if not isinstance(values, dict):
        raise ValidationFailed("SKILL.md frontmatter must be an object")
    unknown = set(values) - _FRONTMATTER_FIELDS
    if unknown:
        raise ValidationFailed(f"SKILL.md unknown frontmatter field: {sorted(unknown)[0]}")
    if set(values) < {"name", "description"}:
        raise ValidationFailed("SKILL.md name and description required")
    return values, document[boundary + 5 :]


def _optional_text(values: Mapping[str, Any], field: str, maximum: int) -> str | None:
    value = values.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValidationFailed(f"SKILL.md {field} invalid")
    return value


@dataclass(frozen=True, slots=True)
class SkillPackage:
    name: str
    description: str
    instructions: str
    files: Mapping[str, bytes]
    license: str | None = None
    compatibility: str | None = None
    metadata: Mapping[str, str] = MappingProxyType({})
    declared_allowed_tools: str | None = None

    @classmethod
    def parse(cls, directory_name: str, files: Mapping[str, bytes]) -> SkillPackage:
        if not _NAME.fullmatch(directory_name) or len(directory_name) > 64:
            raise ValidationFailed("Skill directory name is not canonical")
        if not isinstance(files, Mapping) or not 1 <= len(files) <= MAX_FILES:
            raise ValidationFailed("Skill package file count invalid")
        normalized: dict[str, bytes] = {}
        folded: set[str] = set()
        total = 0
        for raw_path, payload in files.items():
            path = _canonical_path(raw_path)
            if path.casefold() == ".zekam-managed.json":
                raise PolicyViolation("Skill package reserved management path rejected")
            if PurePosixPath(path).name.casefold() in _SECRET_FILE_NAMES:
                raise PolicyViolation("Skill package secret-bearing filename rejected")
            if not isinstance(payload, bytes) or not payload or len(payload) > MAX_FILE_BYTES:
                raise ValidationFailed("Skill package file payload invalid")
            if _SECRET_PATTERN.search(payload):
                raise PolicyViolation("Skill package suspected secret content rejected")
            key = path.casefold()
            if key in folded:
                raise PolicyViolation("Skill package duplicate canonical path")
            folded.add(key)
            normalized[path] = payload
            total += len(payload)
        if total > MAX_PACKAGE_BYTES or "SKILL.md" not in normalized:
            raise ValidationFailed("Skill package size bound or SKILL.md missing")
        values, instructions = _frontmatter(normalized["SKILL.md"])
        name = values.get("name")
        description = values.get("description")
        if (
            not isinstance(name, str)
            or not _NAME.fullmatch(name)
            or len(name) > 64
            or name != directory_name
            or not isinstance(description, str)
            or not description.strip()
            or len(description) > 1024
        ):
            raise ValidationFailed("Skill name/description specification mismatch")
        metadata = values.get("metadata", {})
        if not isinstance(metadata, dict) or len(metadata) > 32 or not all(
            isinstance(key, str)
            and isinstance(value, str)
            and key.strip()
            and value.strip()
            and len(key) <= 128
            and len(value) <= 1024
            for key, value in metadata.items()
        ):
            raise ValidationFailed("SKILL.md metadata must be bounded string pairs")
        allowed = _optional_text(values, "allowed-tools", 1024)
        return cls(
            name=name,
            description=description,
            instructions=instructions,
            files=MappingProxyType(dict(sorted(normalized.items()))),
            license=_optional_text(values, "license", 1024),
            compatibility=_optional_text(values, "compatibility", 500),
            metadata=MappingProxyType(dict(sorted(metadata.items()))),
            declared_allowed_tools=allowed,
        )

    @classmethod
    def read_directory(cls, root: Path) -> SkillPackage:
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise PolicyViolation("Skill package root must be a real absolute directory")
        files: dict[str, bytes] = {}
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            attributes = getattr(info, "st_file_attributes", 0)
            reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            if path.is_symlink() or reparse:
                raise PolicyViolation("Skill package symlink/junction rejected")
            if path.is_dir():
                continue
            if not path.is_file():
                raise PolicyViolation("Skill package special file rejected")
            files[path.relative_to(root).as_posix()] = path.read_bytes()
            if len(files) > MAX_FILES:
                raise ValidationFailed("Skill package file count invalid")
        return cls.parse(root.name, files)

    @property
    def file_manifest(self) -> tuple[dict[str, object], ...]:
        return tuple(
            {
                "path": path,
                "size": len(payload),
                "content_digest": digest_of_bytes(payload),
                "file_type": "markdown" if path.endswith(".md") else "data",
            }
            for path, payload in self.files.items()
        )

    @property
    def package_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-skill-package-manifest/v1",
                "name": self.name,
                "description": self.description,
                "license": self.license,
                "compatibility": self.compatibility,
                "metadata": dict(self.metadata),
                "files": self.file_manifest,
                "provenance_required": True,
                "grants_authority": False,
            }
        )

    @property
    def semantic_digest(self) -> str:
        return digest(
            {
                "name": self.name,
                "description": self.description,
                "instructions": self.instructions,
                "references": [
                    item for item in self.file_manifest if item["path"] != "SKILL.md"
                ],
            }
        )

    def safe_projection_files(self) -> dict[str, bytes]:
        """Drop permission-bearing experimental metadata from managed exports."""

        frontmatter: dict[str, object] = {
            "name": self.name,
            "description": self.description,
        }
        if self.license is not None:
            frontmatter["license"] = self.license
        if self.compatibility is not None:
            frontmatter["compatibility"] = self.compatibility
        if self.metadata:
            frontmatter["metadata"] = dict(self.metadata)
        rendered = (
            "---\n"
            + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).rstrip("\n")
            + "\n---\n"
            + self.instructions
        ).encode("utf-8")
        return {**self.files, "SKILL.md": rendered}
