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
from zekam.application.source_discovery import (
    DiscoveryPolicy,
    InventoryStatus,
    StopReason,
    discover,
)
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


def test_project_plan_succeeds_on_complete_inventory(tmp_path: Path) -> None:
    # Gercek sinir fixture'i: varsayilan (10k) sinirlari asmayan agac,
    # envanter COMPLETE oldugu icin plan kabul edilir (geriye uyum).
    root = tmp_path / "gpu"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Service.java").write_text(
        "package app;\npublic class Service {\n  void run() {}\n}\n", encoding="utf-8"
    )
    report = discover(root)
    assert report.inventory_status is InventoryStatus.COMPLETE

    plan = build_project_index_plan(
        project_id=uuid4(),
        project_slug="gpu",
        source_root=root,
        source_revision="abc123",
        expected_tree_digest=report.tree_digest,
    )

    assert plan.selected_file_count == 1
    assert plan.discovery.inventory_status is InventoryStatus.COMPLETE


def test_project_plan_rejects_partial_inventory_even_when_digest_matches(tmp_path: Path) -> None:
    # Fail-closed sinir: tree digest uysa ve truncated False olsa bile eksik
    # (PARTIAL) envanterden indeks plani uretilemez.
    root = tmp_path / "gpu"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Service.java").write_text(
        "package app;\npublic class Service {\n  void run() {}\n}\n", encoding="utf-8"
    )
    (root / "src" / "extra" / "Deep.java").parent.mkdir(parents=True)
    (root / "src" / "extra" / "Deep.java").write_text(
        "package app.extra;\nclass Deep {}\n", encoding="utf-8"
    )
    # max_depth=1: ikinci seviye dosyalar (src/Service.java) eligibility'den
    # disari, envanter PARTIAL (DEPTH_LIMIT) ve truncated False olur.
    policy = DiscoveryPolicy(max_depth=1)
    report = discover(root, policy=policy)
    assert report.inventory_status is InventoryStatus.PARTIAL
    assert report.inventory_stop_reason is StopReason.DEPTH_LIMIT
    assert report.truncated is False

    # Beklenen tree digest, PARTIAL kesfin eligible alt kumesiyle uyusur;
    # yine de plan fail-closed reddedilir.
    with pytest.raises(PolicyViolation, match="envanteri tam degil"):
        build_project_index_plan(
            project_id=uuid4(),
            project_slug="gpu",
            source_root=root,
            source_revision="abc123",
            expected_tree_digest=report.tree_digest,
            discovery_policy=policy,
        )


@pytest.mark.parametrize("limit_name", ["max_dir_entries", "max_inventory_entries"])
def test_project_plan_rejects_partial_inventory_from_real_limits(
    tmp_path: Path, limit_name: str
) -> None:
    # Gercek sinir fixture'i (mock yok): tek dizin icine girdi sinirini asan
    # dosya sayisi, discover'i PARTIAL keser; build_project_index_plan bu
    # sinir fixture'i ile fail-closed reddeder.
    root = tmp_path / "gpu"
    (root / "calisma").mkdir(parents=True)
    for index in range(20):
        (root / "calisma" / f"d{index:02d}.py").write_text("print('x')\n", encoding="utf-8")
    policy = (
        DiscoveryPolicy(max_dir_entries=10)
        if limit_name == "max_dir_entries"
        else DiscoveryPolicy(max_inventory_entries=10)
    )
    report = discover(root, policy=policy)
    assert report.inventory_status is InventoryStatus.PARTIAL
    assert report.truncated is False

    with pytest.raises(PolicyViolation, match="envanteri tam degil"):
        build_project_index_plan(
            project_id=uuid4(),
            project_slug="gpu",
            source_root=root,
            source_revision="abc123",
            expected_tree_digest=report.tree_digest,
            discovery_policy=policy,
        )


def test_project_plan_partial_rejection_exposes_reason_without_source_path(
    tmp_path: Path,
) -> None:
    # Red yalniz neden kodunu aciklar; checkpoint yolu hassas olabilir.
    root = tmp_path / "gpu"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Service.java").write_text(
        "package app;\npublic class Service {\n  void run() {}\n}\n", encoding="utf-8"
    )
    (root / "src" / "private-person@example.com").mkdir()
    (root / "src" / "private-person@example.com" / "Deep.java").write_text(
        "package app.extra;\nclass Deep {}\n", encoding="utf-8"
    )
    policy = DiscoveryPolicy(max_depth=1)
    report = discover(root, policy=policy)
    assert report.inventory_status is InventoryStatus.PARTIAL

    with pytest.raises(PolicyViolation) as excinfo:
        build_project_index_plan(
            project_id=uuid4(),
            project_slug="gpu",
            source_root=root,
            source_revision="abc123",
            expected_tree_digest=report.tree_digest,
            discovery_policy=policy,
        )

    message = str(excinfo.value)
    # Neden (makine kodu) mesajda gorunur.
    assert "depth-limit" in message
    assert "private-person@example.com" not in message
    assert str(root) not in message
    assert "package app" not in message
