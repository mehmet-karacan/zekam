"""Bounded, no-follow source resolution for project continuity fragments."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from zekam.application.knowledge_file_plane import validate_portable_relative
from zekam.application.local_continuity import ContinuityBinding
from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.knowledge_files import KnowledgeFileStore


class ProjectContinuitySourceResolver:
    """Trusted composition binds the real project, snapshot and exact allowed files.

    Source metadata supplied by a context candidate cannot change this binding.
    Other source kinds require their own trusted resolver, never a text fallback.
    """

    def __init__(
        self,
        root: Path,
        *,
        project_id: str,
        realm_id: str,
        source_snapshot_id: str,
        allowed_paths: tuple[str, ...],
    ) -> None:
        if (
            not isinstance(allowed_paths, tuple)
            or not 1 <= len(allowed_paths) <= 128
            or len(set(allowed_paths)) != len(allowed_paths)
        ):
            raise ValidationFailed("Continuity source exact bounded allowlist required")
        for value in allowed_paths:
            validate_portable_relative(value)
        self._files = KnowledgeFileStore(root)
        self._project = project_id
        self._realm = realm_id
        self._snapshot = source_snapshot_id
        self._paths = frozenset(allowed_paths)

    def __call__(self, binding: ContinuityBinding, provenance: dict[str, Any]) -> str:
        if provenance.get("kind") != "source-slice":
            raise PolicyViolation("Continuity source kind has no registered resolver")
        text = self.read_fragment(binding, provenance.get("source_ref"))
        if digest(text) != provenance.get("digest"):
            raise PolicyViolation("Continuity resolved source content digest mismatch")
        return text

    def read_fragment(self, binding: ContinuityBinding, ref: object) -> str:
        """Read only an explicitly allowlisted fragment; never traverse the corpus."""
        if (binding.project_id, binding.realm_id, binding.source_snapshot_id) != (
            self._project,
            self._realm,
            self._snapshot,
        ):
            raise PolicyViolation("Continuity resolver exact source binding mismatch")
        if not isinstance(ref, str):
            raise ValidationFailed("Continuity source locator required")
        path, separator, locator = ref.partition("#")
        if path not in self._paths:
            raise PolicyViolation("Continuity source outside approved bounded corpus")
        payload = self._files._read_optional(path, max_bytes=2 * 1024 * 1024)
        if payload is None:
            raise PolicyViolation("Continuity source file missing")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PolicyViolation("Continuity source text corrupt") from exc
        if separator:
            match = re.fullmatch(r"L([1-9][0-9]*)-L([1-9][0-9]*)", locator)
            if match is None:
                raise ValidationFailed("Continuity source line locator malformed")
            first, last = (int(value) for value in match.groups())
            lines = text.splitlines(keepends=True)
            if first > last or last > len(lines):
                raise ValidationFailed("Continuity source line locator outside file")
            text = "".join(lines[first - 1 : last])
        return text
