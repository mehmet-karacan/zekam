"""Project source index ve yerel embedding testleri."""

from __future__ import annotations

import math
from pathlib import Path
from uuid import uuid4

import pytest

from zekam.application.embedding_routing import EmbeddingRouteKind
from zekam.application.project_knowledge_index import (
    NON_SEMANTIC_BASELINE_DIMENSION,
    REAL_EMBEDDING_DIMENSION,
    REAL_EMBEDDING_MODEL_REF,
    build_project_index_plan,
    feature_hash_baseline_vector,
)
from zekam.application.source_discovery import discover
from zekam.domain.errors import PolicyViolation

pytestmark = pytest.mark.unit


def test_feature_hash_baseline_is_normalized_stable_and_explicitly_non_semantic() -> None:
    first = feature_hash_baseline_vector("GPU servis baglantisi")
    second = feature_hash_baseline_vector("GPU servis baglantisi")

    assert first == second
    assert len(first) == NON_SEMANTIC_BASELINE_DIMENSION
    assert math.isclose(sum(value * value for value in first), 1.0, rel_tol=1e-9)
    assert first != feature_hash_baseline_vector("tamamen baska metin")
    punctuation = feature_hash_baseline_vector("{} => []")
    assert len(punctuation) == NON_SEMANTIC_BASELINE_DIMENSION
    assert math.isclose(sum(value * value for value in punctuation), 1.0, rel_tol=1e-9)


def test_project_plan_excludes_secret_generated_binary_and_unknown_files(tmp_path: Path) -> None:
    root = tmp_path / "gpu"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Service.java").write_text(
        "package app;\npublic class Service {\n  void run() {}\n}\n", encoding="utf-8"
    )
    (root / "src" / "legacy.java").write_bytes(b"class Legacy { // \xff }")
    (root / "notes.unknown").write_text("not indexed", encoding="utf-8")
    (root / ".env.local").write_text("PASSWORD=must-not-enter-index", encoding="utf-8")
    (root / "tsconfig.tsbuildinfo").write_text("generated", encoding="utf-8")
    report = discover(root)

    plan = build_project_index_plan(
        project_id=uuid4(),
        project_slug="gpu",
        source_root=root,
        source_revision="abc123",
        expected_tree_digest=report.tree_digest,
    )

    assert plan.selected_file_count == 1
    assert plan.skipped_encoding == 1
    assert plan.skipped_unsupported == 1
    assert plan.embedding_profile.model_ref == REAL_EMBEDDING_MODEL_REF
    assert plan.embedding_profile.dimension == REAL_EMBEDDING_DIMENSION
    assert plan.embedding_route.kind is EmbeddingRouteKind.LOCAL_PROVIDER
    assert "qualified-embedding-candidate-missing" in plan.embedding_route.reasons
    assert all(chunk.locator.relative_path == "src/Service.java" for chunk in plan.chunks)
    rendered = repr(plan.as_dict())
    assert "must-not-enter-index" not in rendered
    assert "tsconfig.tsbuildinfo" not in rendered


def test_project_plan_rejects_tree_drift(tmp_path: Path) -> None:
    root = tmp_path / "gpu"
    root.mkdir()
    source = root / "main.ts"
    source.write_text("export const value = 1;", encoding="utf-8")
    tree_digest = discover(root).tree_digest
    source.write_text("export const value = 2;", encoding="utf-8")

    with pytest.raises(PolicyViolation, match="drift"):
        build_project_index_plan(
            project_id=uuid4(),
            project_slug="gpu",
            source_root=root,
            source_revision="abc123",
            expected_tree_digest=tree_digest,
        )
