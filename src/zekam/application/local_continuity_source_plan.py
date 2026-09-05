"""Portable, explicit bounded-source recipe; no path or execution authority."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from zekam.application.knowledge_file_plane import validate_portable_relative
from zekam.application.local_continuity import bounded_int, digest_text, uuid_text
from zekam.domain.canonical import digest
from zekam.domain.errors import ValidationFailed

SOURCE_RECIPE_SCHEMA = "zekam-continuity-bounded-source/v1"
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 8 * 1024 * 1024
MAX_IGNORE_BYTES = 65536


def source_ref(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.encode()) <= 512:
        raise ValidationFailed("Source recipe bounded relative path required")
    checked = validate_portable_relative(value)
    if checked != value or len(value.split("/")) > 16 or "#" in value:
        raise ValidationFailed("Source recipe exact path/depth required")
    return checked


@dataclass(frozen=True, slots=True)
class ContinuitySourceRecipe:
    project_id: str
    realm_id: str
    source_binding_id: str
    allowed_paths: tuple[str, ...]
    task_digest: str
    policy_digest: str

    def __post_init__(self) -> None:
        for name in ("project_id", "realm_id", "source_binding_id"):
            uuid_text(getattr(self, name), name)
        digest_text(self.task_digest)
        digest_text(self.policy_digest)
        if not isinstance(self.allowed_paths, tuple) or not 1 <= len(self.allowed_paths) <= 8:
            raise ValidationFailed("Source recipe requires 1..8 explicit files")
        for path in self.allowed_paths:
            source_ref(path)
        if len({path.casefold() for path in self.allowed_paths}) != len(self.allowed_paths):
            raise ValidationFailed("Source recipe duplicate or case alias")
        if tuple(sorted(self.allowed_paths)) != self.allowed_paths:
            raise ValidationFailed("Source recipe paths must be in canonical order")

    def body(self) -> dict[str, Any]:
        self.__post_init__()
        return {
            "schema": SOURCE_RECIPE_SCHEMA,
            "project_id": self.project_id,
            "realm_id": self.realm_id,
            "source_binding_id": self.source_binding_id,
            "task_digest": self.task_digest,
            "policy_digest": self.policy_digest,
            "allowed_paths": list(self.allowed_paths),
            "max_file_bytes": MAX_SOURCE_BYTES,
            "max_total_bytes": MAX_TOTAL_SOURCE_BYTES,
            "max_ignore_bytes": MAX_IGNORE_BYTES,
            "git_scope": "explicit-tracked-files-only",
            "git_external_config": "disabled",
            "git_layout": "physical-contained-git-directory-only",
            "git_local_excludes": "fingerprinted-metadata-not-corpus",
            "tree_scope": "bounded-files-not-whole-repository",
            "secret_scan": "zekam-secret-rules",
            "custom_ignore_syntax": "restricted-no-escape-or-character-class",
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class CapturedSourceFile:
    relative_path: str
    content_digest: str
    size_bytes: int

    def __post_init__(self) -> None:
        source_ref(self.relative_path)
        digest_text(self.content_digest)
        bounded_int(self.size_bytes, maximum=MAX_SOURCE_BYTES)

    def body(self) -> dict[str, Any]:
        self.__post_init__()
        return {
            "path": self.relative_path,
            "content_digest": self.content_digest,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class ContinuitySourcePlan:
    recipe: ContinuitySourceRecipe
    revision_ref: str
    files: tuple[CapturedSourceFile, ...]
    ignore_digests: tuple[tuple[str, str | None], ...]
    secret_policy_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.recipe, ContinuitySourceRecipe):
            raise ValidationFailed("Typed source recipe required")
        self.recipe.__post_init__()
        if not isinstance(self.revision_ref, str) or not re.fullmatch(
            r"[0-9a-f]{40}(?:[0-9a-f]{24})?", self.revision_ref
        ):
            raise ValidationFailed("Source exact Git commit required")
        if not isinstance(self.files, tuple) or any(
            not isinstance(file, CapturedSourceFile) for file in self.files
        ):
            raise ValidationFailed("Typed source file tuple required")
        for file in self.files:
            file.__post_init__()
        if tuple(file.relative_path for file in self.files) != self.recipe.allowed_paths:
            raise ValidationFailed("Source exact allowlist partition required")
        if sum(file.size_bytes for file in self.files) > MAX_TOTAL_SOURCE_BYTES:
            raise ValidationFailed("Source total byte bound exceeded")
        if not isinstance(self.ignore_digests, tuple) or len(self.ignore_digests) > 256:
            raise ValidationFailed("Source ignore fingerprint bound exceeded")
        names = []
        for entry in self.ignore_digests:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValidationFailed("Source ignore exact pair required")
            name, fingerprint = entry
            source_ref(name)
            if name != ".git/info/exclude" and name.split("/")[-1] not in {
                ".gitignore",
                ".zekamignore",
            }:
                raise ValidationFailed("Source unknown ignore reference")
            if fingerprint is not None:
                digest_text(fingerprint)
            names.append(name)
        if names != sorted(set(names)):
            raise ValidationFailed("Source ignore partition must be unique and sorted")
        digest_text(self.secret_policy_digest)

    @property
    def config_digest(self) -> str:
        return digest(
            {
                "recipe": self.recipe.body(),
                "ignore_digests": self.ignore_digests,
                "secret_policy_digest": self.secret_policy_digest,
            }
        )

    @property
    def tree_digest(self) -> str:
        return digest({"scope": "bounded-files", "files": [file.body() for file in self.files]})

    @property
    def content_digest(self) -> str:
        return digest(self.body())

    def body(self) -> dict[str, Any]:
        self.__post_init__()
        return {
            "schema": SOURCE_RECIPE_SCHEMA,
            "recipe": self.recipe.body(),
            "revision_ref": self.revision_ref,
            "files": [file.body() for file in self.files],
            "ignore_digests": self.ignore_digests,
            "secret_policy_digest": self.secret_policy_digest,
            "grants_authority": False,
            "atomic_filesystem_snapshot": False,
        }
