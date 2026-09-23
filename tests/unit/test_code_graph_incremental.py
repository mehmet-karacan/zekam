"""Incremental graph generation reuse and profile-version tests."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

import pytest

from zekam.application.code_graph import (
    GraphFileExtraction,
    apply_graph_build,
    plan_graph_build,
)
from zekam.application.code_graph_python import PythonAstExtractor
from zekam.domain.canonical import digest
from zekam.domain.code_graph import GraphFile
from zekam.infrastructure.sqlite.code_graph import SQLiteCodeGraphStore

pytestmark = pytest.mark.unit

_CREATED_AT = "2026-09-21T00:00:00Z"
_PROJECT_ID = "p"
_SLUG = "p"
_REVISION = "r1"


class _ProfileOverride:
    def __init__(self, inner: PythonAstExtractor, profile: str) -> None:
        self._inner = inner
        self._profile = profile

    @property
    def profile_digest(self) -> str:
        return self._profile

    def supports(self, relative_path: str) -> bool:
        return self._inner.supports(relative_path)

    def extract_file(
        self, source_root: Path, relative_path: str, content: bytes
    ) -> GraphFileExtraction:
        extraction = self._inner.extract_file(source_root, relative_path, content)
        # Re-stamp the produced file with this profile so a profile bump is observable.
        file = GraphFile(
            relative_path=extraction.file.relative_path,
            content_digest=extraction.file.content_digest,
            parse_state=extraction.file.parse_state,
            error_count=extraction.file.error_count,
            extractor_profile_digest=self._profile,
        )
        return GraphFileExtraction(file=file, symbols=extraction.symbols, edges=extraction.edges)


class _ExtractorLike(Protocol):
    @property
    def profile_digest(self) -> str: ...

    def supports(self, relative_path: str) -> bool: ...

    def extract_file(
        self, source_root: Path, relative_path: str, content: bytes
    ) -> GraphFileExtraction: ...


def _write(path: Path, files: dict[str, str]) -> Path:
    source_root = path / "srcroot"
    source_root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        target = source_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return source_root


def _rebuild(
    store: SQLiteCodeGraphStore,
    source_root: Path,
    extractor: _ExtractorLike | None = None,
) -> str:
    extractor = extractor or PythonAstExtractor()
    plan = plan_graph_build(
        source_root,
        project_id=_PROJECT_ID,
        project_slug=_SLUG,
        source_revision=_REVISION,
        extractor=extractor,
        created_at=_CREATED_AT,
    )
    return apply_graph_build(store, extractor, source_root, plan).generation_digest


def test_zero_change_rebuild_same_generation(tmp_path: Path) -> None:
    source_root = _write(tmp_path, {"mod.py": "def f():\n    return 1\n"})
    store = SQLiteCodeGraphStore(tmp_path / "g.sqlite3", create=True)
    try:
        first = _rebuild(store, source_root)
        second = _rebuild(store, source_root)
        assert first == second
    finally:
        store.close()


def test_one_file_edit_new_generation(tmp_path: Path) -> None:
    source_root = _write(tmp_path, {"mod.py": "def f():\n    return 1\n"})
    store = SQLiteCodeGraphStore(tmp_path / "g.sqlite3", create=True)
    try:
        first = _rebuild(store, source_root)
        (source_root / "mod.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        second = _rebuild(store, source_root)
        assert second != first
        current = store.generation(_PROJECT_ID)
        assert current.generation_digest == second
    finally:
        store.close()


def test_same_size_content_change_new_generation(tmp_path: Path) -> None:
    source_root = _write(tmp_path, {"mod.py": "def f():\n    return 1\n"})
    store = SQLiteCodeGraphStore(tmp_path / "g.sqlite3", create=True)
    try:
        first = _rebuild(store, source_root)
        # Same byte length, different content.
        (source_root / "mod.py").write_text("def f():\n    return 9\n", encoding="utf-8")
        second = _rebuild(store, source_root)
        assert second != first
    finally:
        store.close()


def test_delete_file_new_generation(tmp_path: Path) -> None:
    files = {"a.py": "def f():\n    return 1\n", "b.py": "def g():\n    return 2\n"}
    source_root = _write(tmp_path, files)
    store = SQLiteCodeGraphStore(tmp_path / "g.sqlite3", create=True)
    try:
        first = _rebuild(store, source_root)
        first_files = store.generation(_PROJECT_ID).file_count
        (source_root / "b.py").unlink()
        second = _rebuild(store, source_root)
        assert second != first
        assert store.generation(_PROJECT_ID).file_count == first_files - 1
    finally:
        store.close()


def test_body_unchanged_line_moved_body_digest_stable(tmp_path: Path) -> None:
    source_root = _write(tmp_path, {"mod.py": "def f():\n    return 1\n"})
    store = SQLiteCodeGraphStore(tmp_path / "g.sqlite3", create=True)
    try:
        extractor = PythonAstExtractor()
        first = _rebuild(store, source_root, extractor)
        before = [
            s.body_digest
            for s in extractor.extract_file(
                source_root, "mod.py", (source_root / "mod.py").read_bytes()
            ).symbols
            if s.qualified_name == "mod.f"
        ]
        # Insert a leading blank line: content changes, but the f() body is the same.
        (source_root / "mod.py").write_text("\ndef f():\n    return 1\n", encoding="utf-8")
        second = _rebuild(store, source_root, extractor)
        assert second != first
        after = [
            s.body_digest
            for s in extractor.extract_file(
                source_root, "mod.py", (source_root / "mod.py").read_bytes()
            ).symbols
            if s.qualified_name == "mod.f"
        ]
        assert before and after
        assert before[0] == after[0]
    finally:
        store.close()


def test_extractor_profile_version_change_new_generation(tmp_path: Path) -> None:
    source_root = _write(tmp_path, {"mod.py": "def f():\n    return 1\n"})
    store = SQLiteCodeGraphStore(tmp_path / "g.sqlite3", create=True)
    try:
        inner = PythonAstExtractor()
        first = _rebuild(store, source_root, inner)
        override = _ProfileOverride(inner, digest("new-extractor-profile-v2"))
        second = _rebuild(store, source_root, override)
        assert second != first
        current = store.generation(_PROJECT_ID)
        assert current.generation_digest == second
        assert current.extractor_profile_digest == override.profile_digest
    finally:
        store.close()
