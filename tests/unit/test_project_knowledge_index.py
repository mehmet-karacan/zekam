"""Project source index ve yerel embedding testleri."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from zekam.application import project_knowledge_index as knowledge_index
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
from zekam.domain.knowledge import UnitKind
from zekam.domain.retrieval import estimate_tokens

pytestmark = pytest.mark.unit


def test_python_ast_units_preserve_top_level_symbols_and_all_source_lines() -> None:
    source = (
        "import os\n\n"
        "@trace\n@tag(\n    1,\n)\n"
        "def work():\n    return 1\n\n"
        "flag = True\n\n"
        "class Service:\n    def run(self):\n        return flag\n\n"
        "answer = 42\n"
    )
    units = knowledge_index._file_units("src/main.py", source, 5)

    symbols = [unit for unit in units if unit.kind is UnitKind.SYMBOL]
    assert [
        (unit.locator.symbol, unit.locator.line_start, unit.locator.line_end) for unit in symbols
    ] == [
        ("work", 3, 8),
        ("Service", 12, 14),
    ]
    assert [unit.order for unit in units] == list(range(5, 5 + len(units)))
    assert len({unit.unit_id for unit in units}) == len(units)
    assert all(unit.locator.relative_path == "src/main.py" for unit in units)
    assert [line.strip() for unit in units for line in unit.text.splitlines() if line.strip()] == [
        line.strip() for line in source.splitlines() if line.strip()
    ]


def test_python_ast_oversize_and_invalid_source_use_bounded_line_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(knowledge_index, "MAX_CHUNK_CHARACTERS", 35)
    large = "def long_function():\n" + "".join(f"    value_{n} = {n}\n" for n in range(8))
    units = knowledge_index._file_units("large.py", large, 0)
    assert units
    assert all(unit.kind is UnitKind.CODE and unit.locator.symbol is None for unit in units)
    assert all(len(unit.text) <= 35 for unit in units)
    assert [line.strip() for unit in units for line in unit.text.splitlines() if line.strip()] == [
        line.strip() for line in large.splitlines() if line.strip()
    ]

    invalid = knowledge_index._file_units("broken.py", "def broken(:\n    pass\n", 0)
    assert invalid and all(unit.kind is UnitKind.CODE for unit in invalid)
    assert all(unit.locator.symbol is None for unit in invalid)
    other = knowledge_index._file_units("Main.java", "class Main {}", 0)
    assert other and all(unit.kind is UnitKind.CODE for unit in other)


def test_python_ast_fallback_splits_a_token_heavy_single_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(knowledge_index, "MAX_CHUNK_TOKENS", 5)
    source = "def many():\n    return a + b + c + d + e + f + g\n"
    units = knowledge_index._file_units("many.py", source, 0)

    assert len(units) > 1
    assert all(unit.kind is UnitKind.CODE and unit.locator.symbol is None for unit in units)
    assert all(estimate_tokens(unit.text) <= 5 for unit in units)
    assert all(len(unit.text) <= knowledge_index.MAX_CHUNK_CHARACTERS for unit in units)


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


def test_project_plan_rejects_file_growth_between_discovery_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "gpu"
    root.mkdir()
    source = root / "main.py"
    source.write_text("value = 1\n", encoding="utf-8")
    tree_digest = discover(root).tree_digest
    original_open = Path.open
    opens = 0

    def grow_on_verified_read(path: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal opens
        if path == source:
            opens += 1
            if opens == 2:
                with original_open(source, "ab") as writer:
                    writer.write(b"# source changed\n")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(Path, "open", grow_on_verified_read)
        with pytest.raises(PolicyViolation, match="buyudu"):
            build_project_index_plan(
                project_id=uuid4(),
                project_slug="gpu",
                source_root=root,
                source_revision="abc123",
                expected_tree_digest=tree_digest,
            )
    assert opens == 2


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
    (root / "src" / "sensitive-path-marker").mkdir()
    (root / "src" / "sensitive-path-marker" / "Deep.java").write_text(
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
    assert "sensitive-path-marker" not in message
    assert str(root) not in message
    assert "package app" not in message
