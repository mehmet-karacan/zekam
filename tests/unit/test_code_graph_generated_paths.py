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
