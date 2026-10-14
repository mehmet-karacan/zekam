"""Generated frontend output is outside the structural graph input set."""

from __future__ import annotations

from pathlib import Path

import pytest

from zekam.application.code_graph import plan_graph_build
from zekam.application.code_graph_polyglot import PolyglotStructuralExtractor

pytestmark = pytest.mark.unit


def test_graph_plan_skips_next_generated_output_but_keeps_source_code(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "project"
    (source_root / "src").mkdir(parents=True)
    (source_root / "src" / "app.js").write_text(
        "export function app() { return true; }\n", encoding="utf-8"
    )
    generated = source_root / "src" / "main" / "resources" / "static" / "_next"
    generated.mkdir(parents=True)
    (generated / "generated.js").write_text(
        "function generated() {\n", encoding="utf-8"
    )

    plan = plan_graph_build(
        source_root,
        project_id="generated-paths",
        project_slug="generated-paths",
        source_revision="r1",
        extractor=PolyglotStructuralExtractor(),
        created_at="2026-10-14T00:00:00Z",
    )

    assert [path for path, _digest in plan.file_manifests] == ["src/app.js"]


def test_graph_plan_skips_symlinked_source_file(tmp_path: Path) -> None:
    source_root = tmp_path / "project"
    source_root.mkdir()
    (source_root / "real.py").write_text("def real():\n    return 1\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("def escaped():\n    return 2\n", encoding="utf-8")
    link = source_root / "escaped.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Symlink olusturulamadi: {exc}")

    plan = plan_graph_build(
        source_root,
        project_id="symlink-paths",
        project_slug="symlink-paths",
        source_revision="r1",
        extractor=PolyglotStructuralExtractor(),
        created_at="2026-10-15T00:00:00Z",
    )

    assert [path for path, _digest in plan.file_manifests] == ["real.py"]


def test_graph_plan_skips_casefolded_generated_and_tool_dirs(tmp_path: Path) -> None:
    source_root = tmp_path / "project"
    source_root.mkdir()
    (source_root / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    for directory in (".GIT", ".VENV", ".PYTEST_CACHE"):
        hidden = source_root / directory
        hidden.mkdir()
        (hidden / "ignored.py").write_text("def ignored():\n    return 0\n", encoding="utf-8")

    plan = plan_graph_build(
        source_root,
        project_id="casefold-paths",
        project_slug="casefold-paths",
        source_revision="r1",
        extractor=PolyglotStructuralExtractor(),
        created_at="2026-10-15T00:00:00Z",
    )

    assert [path for path, _digest in plan.file_manifests] == ["main.py"]
