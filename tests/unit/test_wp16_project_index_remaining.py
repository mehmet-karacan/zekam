from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from zekam.application import project_knowledge_index as index
from zekam.application.source_discovery import discover
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed

PROJECT_ID = UUID("00000000-0000-4000-8000-000000000001")


def test_verified_text_rejects_symlink_digest_drift_and_returns_none_for_binary(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("print('safe')\n", encoding="utf-8")
    link = tmp_path / "link.py"
    link.symlink_to(source)
    with pytest.raises(PolicyViolation, match="symlink"):
        index._verified_text(tmp_path, "link.py", digest_of_bytes(source.read_bytes()))
    with pytest.raises(PolicyViolation, match="degisti"):
        index._verified_text(tmp_path, "source.py", digest_of_bytes(b"wrong"))
    binary = tmp_path / "binary.py"
    binary.write_bytes(b"\xff\xfe\xfd")
    assert index._verified_text(tmp_path, "binary.py", digest_of_bytes(binary.read_bytes())) is None


def test_file_units_handles_empty_flush_threshold_and_long_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert index._file_units("empty.py", "  \n", 0) == ()
    monkeypatch.setattr(index, "MAX_CHUNK_CHARACTERS", 5)
    units = index._file_units("source.py", "aa\nbbbbbbbbb\ncc", 7)
    assert [unit.text for unit in units] == ["aa", "bbbbb", "bbbb", "cc"]
    assert [unit.order for unit in units] == [7, 8, 9, 10]
    assert all(unit.locator.relative_path == "source.py" for unit in units)


@pytest.mark.parametrize("allowed", ((), ("../escape.py",), ("source.py", "source.py")))
def test_build_plan_rejects_empty_unsafe_and_duplicate_allowlists(
    tmp_path: Path, allowed: tuple[str, ...]
) -> None:
    source = tmp_path / "source.py"
    source.write_text("print('safe')\n", encoding="utf-8")
    discovery = discover(tmp_path)
    with pytest.raises(ValidationFailed, match="allowlist"):
        index.build_project_index_plan(
            project_id=PROJECT_ID,
            project_slug="project",
            source_root=tmp_path,
            source_revision="revision",
            expected_tree_digest=discovery.tree_digest,
            allowed_relative_paths=allowed,
        )


def test_build_plan_rejects_missing_unsupported_and_empty_selected_content(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_text("print('safe')\n", encoding="utf-8")
    discovery = discover(tmp_path)
    with pytest.raises(ValidationFailed, match="eksik"):
        index.build_project_index_plan(
            project_id=PROJECT_ID,
            project_slug="project",
            source_root=tmp_path,
            source_revision="revision",
            expected_tree_digest=discovery.tree_digest,
            allowed_relative_paths=("missing.py",),
        )
    unsupported = tmp_path / "source.bin"
    source.unlink()
    unsupported.write_bytes(b"safe")
    discovery = discover(tmp_path)
    with pytest.raises(ValidationFailed, match="dosyasi bulunamadi"):
        index.build_project_index_plan(
            project_id=PROJECT_ID,
            project_slug="project",
            source_root=tmp_path,
            source_revision="revision",
            expected_tree_digest=discovery.tree_digest,
        )
    unsupported.unlink()
    source.write_text("   \n", encoding="utf-8")
    discovery = discover(tmp_path)
    with pytest.raises(ValidationFailed, match="icerigi bulunamadi"):
        index.build_project_index_plan(
            project_id=PROJECT_ID,
            project_slug="project",
            source_root=tmp_path,
            source_revision="revision",
            expected_tree_digest=discovery.tree_digest,
        )


def test_build_plan_rejects_allowlisted_binary_encoding(tmp_path: Path) -> None:
    source = tmp_path / "source.py"
    source.write_bytes(b"\xff\xfe\xfd")
    discovery = discover(tmp_path)
    with pytest.raises(ValidationFailed, match="icerigi bulunamadi"):
        index.build_project_index_plan(
            project_id=PROJECT_ID,
            project_slug="project",
            source_root=tmp_path,
            source_revision="revision",
            expected_tree_digest=discovery.tree_digest,
            allowed_relative_paths=("source.py",),
        )


def test_apply_project_index_rejects_missing_verified_embedding_before_any_write(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.py"
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    discovery = discover(tmp_path)
    plan = index.build_project_index_plan(
        project_id=PROJECT_ID,
        project_slug="project",
        source_root=tmp_path,
        source_revision="revision",
        expected_tree_digest=discovery.tree_digest,
    )
    with pytest.raises(PolicyViolation, match="Verified embedding"):
        index.apply_project_index(
            plan,
            connection=object(),
            knowledge=object(),
            retrieval=object(),
            object_store=object(),  # type: ignore[arg-type]
        )
