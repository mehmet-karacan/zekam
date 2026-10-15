"""Project source index ve yerel embedding testleri."""

from __future__ import annotations

import dataclasses
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
    EligibilityDecision,
    EligibilityOutcome,
    FileKind,
    InventoryStatus,
    StopReason,
    discover,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
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
    assert other and [unit.locator.symbol for unit in other] == ["Main"]


@pytest.mark.parametrize(
    ("relative_path", "source", "symbol"),
    [
        (
            "Main.java",
            "package app;\npublic class Main {\n  void run() { return; }\n}\n",
            "Main",
        ),
        (
            "service.ts",
            "export function add(a: number, b: number) { return a + b; }\n",
            "add",
        ),
        (
            "Status.java",
            "package app;\npublic enum Status { READY, DONE; }\n",
            "Status",
        ),
        (
            "Point.java",
            "public record Point(int x, int y) {}\n",
            "Point",
        ),
        (
            "types.ts",
            "type User<T> = {\n  id: T;\n  active: boolean;\n};\n",
            "User",
        ),
    ],
)
def test_structural_code_units_extract_top_level_declarations(
    relative_path: str, source: str, symbol: str
) -> None:
    units = knowledge_index._file_units(relative_path, source, 3)
    symbols = [unit for unit in units if unit.kind is UnitKind.SYMBOL]
    assert [unit.locator.symbol for unit in symbols] == [symbol]
    expected = (
        source[source.index("public") :].strip()
        if relative_path.endswith(".java")
        else source.strip()
    )
    assert symbols[0].text == expected
    expected_order = 4 if relative_path.endswith(".java") and "package app;" in source else 3
    assert symbols[0].order == expected_order


def test_structural_code_units_extract_type_aliases_in_source_order() -> None:
    source = (
        "type User = { id: string; };\n"
        "\n"
        "export class Service {\n"
        "  run(): User { return { id: 'x' }; }\n"
        "}\n"
    )
    units = knowledge_index._file_units("service.ts", source, 0)
    symbols = [unit for unit in units if unit.kind is UnitKind.SYMBOL]

    assert [unit.locator.symbol for unit in symbols] == ["User", "Service"]
    assert symbols[0].text == "type User = { id: string; };"
    assert symbols[0].locator.line_start == 1
    assert symbols[0].locator.line_end == 1
    assert symbols[1].locator.line_start == 3


def test_structural_code_units_ignore_type_words_in_literals_and_nested_bodies() -> None:
    source = (
        "function outer() {\n"
        "  const value = 'type Hidden = string;';\n"
        "  return value;\n"
        "}\n"
        "type Visible = string | number;\n"
    )
    units = knowledge_index._file_units("types.ts", source, 0)
    symbols = [unit for unit in units if unit.kind is UnitKind.SYMBOL]

    assert [unit.locator.symbol for unit in symbols] == ["outer", "Visible"]
    assert sum("Hidden" in unit.text for unit in symbols) == 1


def test_structural_code_units_accept_type_alias_at_file_end_without_semicolon() -> None:
    units = knowledge_index._file_units("types.ts", "type Result<T> = T | null\n", 0)
    symbols = [unit for unit in units if unit.kind is UnitKind.SYMBOL]

    assert [unit.locator.symbol for unit in symbols] == ["Result"]
    assert symbols[0].text == "type Result<T> = T | null"


@pytest.mark.parametrize(
    ("xml", "attribute", "limit", "error"),
    [
        ("<root><child/><child/></root>", "MAX_XML_ELEMENTS", 2, "element sayisi"),
        ("<root><child><leaf/></child></root>", "MAX_XML_DEPTH", 2, "derinligi"),
    ],
)
def test_xml_structural_units_reject_bounded_tree_limits(
    monkeypatch: pytest.MonkeyPatch,
    xml: str,
    attribute: str,
    limit: int,
    error: str,
) -> None:
    monkeypatch.setattr(knowledge_index, attribute, limit)

    with pytest.raises(PolicyViolation, match=error):
        knowledge_index._file_units("bounded.xml", xml, 0)


def test_structural_code_units_ignore_braces_in_literals_and_nested_declarations() -> None:
    source = 'class Box {\n  method() { return "}"; }\n}\nfunction tail() { return 1; }\n'
    units = knowledge_index._file_units("box.js", source, 0)
    symbols = [unit for unit in units if unit.kind is UnitKind.SYMBOL]
    assert [unit.locator.symbol for unit in symbols] == ["Box", "tail"]
    assert all("method" in unit.text or "tail" in unit.text for unit in symbols)


def test_structural_code_units_fall_back_on_unbalanced_source() -> None:
    units = knowledge_index._file_units("broken.ts", "class Broken {\n", 0)
    assert units and all(unit.kind is UnitKind.CODE for unit in units)


@pytest.mark.parametrize(
    ("relative_path", "source", "symbols"),
    [
        (
            "schema.sql",
            "-- onceki yorum\nCREATE TABLE app_users (id NUMBER);\n"
            "CREATE OR REPLACE VIEW active_users AS SELECT id FROM app_users;\n",
            ["app_users", "active_users"],
        ),
        (
            "pkg.pkb",
            "CREATE OR REPLACE PACKAGE BODY app_pkg AS\n"
            "  FUNCTION answer RETURN NUMBER IS\n"
            "  BEGIN RETURN 42; END;\nEND app_pkg;\n/\n",
            ["app_pkg"],
        ),
    ],
)
def test_sql_and_plsql_structural_units_extract_create_objects(
    relative_path: str, source: str, symbols: list[str]
) -> None:
    units = knowledge_index._file_units(relative_path, source, 2)
    extracted = [unit for unit in units if unit.kind is UnitKind.SYMBOL]
    assert [unit.locator.symbol for unit in extracted] == symbols
    assert all(unit.locator.relative_path == relative_path for unit in extracted)
    assert all("CREATE" in unit.text.upper() for unit in extracted)


def test_markdown_units_preserve_heading_paths_paragraphs_and_tables() -> None:
    source = (
        "# Overview\n\n"
        "Intro paragraph.\n\n"
        "## Details\n\n"
        "| Name | Value |\n| --- | --- |\n| alpha | one |\n\n"
        "- first\n- second\n"
    )
    units = knowledge_index._file_units("README.md", source, 4)
    assert [unit.kind for unit in units] == [
        UnitKind.HEADING,
        UnitKind.PARAGRAPH,
        UnitKind.HEADING,
        UnitKind.TABLE,
        UnitKind.LIST,
    ]
    assert units[0].locator.heading_path == ("Overview",)
    assert units[2].locator.heading_path == ("Overview", "Details")
    assert units[3].locator.line_start == 7
    assert units[3].locator.line_end == 9
    assert units[4].locator.heading_path == ("Overview", "Details")


def test_markdown_fenced_code_gets_code_locator() -> None:
    units = knowledge_index._file_units("guide.md", "```python\nprint(1)\n```\n", 0)
    assert len(units) == 1
    assert units[0].kind is UnitKind.CODE
    assert units[0].locator.line_start == 1
    assert units[0].locator.line_end == 3


def test_xml_units_use_safe_entry_locators_and_reject_dtd_entities() -> None:
    source = (
        '<?xml version="1.0"?>\n'
        "<project><name>zekam</name><dependency><group>app</group></dependency></project>\n"
    )
    units = knowledge_index._file_units("pom.xml", source, 0)
    assert units
    assert all(unit.kind is UnitKind.METADATA for unit in units)
    assert all(
        unit.locator.entry_path and not unit.locator.entry_path.startswith("/") for unit in units
    )
    assert any("dependency" in (unit.locator.entry_path or "") for unit in units)

    with pytest.raises(PolicyViolation, match=r"DTD|entity"):
        knowledge_index._file_units(
            "unsafe.xml",
            '<!DOCTYPE project [<!ENTITY leak "secret">]><project>&leak;</project>',
            0,
        )


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
    (root / "app.properties").write_text("service.password=short", encoding="utf-8")
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
    assert "app.properties" not in rendered
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


def test_project_plan_selection_is_driven_by_eligibility_decisions(
    tmp_path: Path,
) -> None:
    # Pozitif sozlesme: secim eligibility kararlariyla ayni dosyalari uretir;
    # eligible girdi envanter metadata'si (is_text) ile birlikte tasinir.
    root = tmp_path / "gpu"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Service.java").write_text(
        "package app;\npublic class Service {\n  void run() {}\n}\n", encoding="utf-8"
    )
    (root / "notes.unknown").write_text("not indexed", encoding="utf-8")
    report = discover(root)

    eligible_paths = {
        item.relative_path
        for item in report.eligibility_decisions
        if item.outcome is EligibilityOutcome.ELIGIBLE
    }
    # Eligibility icerik okunabilirligidir: bilinmeyen uzanti da eligible
    # kalir; indeks secimi destekli uzanti filtresiyle daralir.
    assert eligible_paths == {"src/Service.java", "notes.unknown"}

    plan = build_project_index_plan(
        project_id=uuid4(),
        project_slug="gpu",
        source_root=root,
        source_revision="abc123",
        expected_tree_digest=report.tree_digest,
    )

    # Plan yalniz eligible kararlarin dosyalarindan seciyor (karar kumesi
    # kaynagi); `files`'ten bagimsiz olarak ayni sonucu uretir.
    assert plan.selected_file_count == 1
    assert {unit.locator.relative_path for unit in plan.document.units} == {"src/Service.java"}
    inventory_entry = next(
        entry for entry in report.inventory if entry.relative_path == "src/Service.java"
    )
    assert inventory_entry.eligibility is not None
    assert inventory_entry.eligibility.outcome is EligibilityOutcome.ELIGIBLE
    assert inventory_entry.eligibility.reason is None
    assert inventory_entry.is_text is True


def test_project_plan_rejects_eligible_decision_missing_from_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fail-closed: eligible karar var ama kesif dosya kumesinde girdi yoksa
    # (karar `files`'ten turetilmedigi icin yakalanir) plan reddedilir.
    root = tmp_path / "gpu"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Service.java").write_text(
        "package app;\npublic class Service {\n  void run() {}\n}\n", encoding="utf-8"
    )
    report = discover(root)
    report = dataclasses.replace(
        report,
        eligibility_decisions=(
            *report.eligibility_decisions,
            EligibilityDecision.eligible("src/missing.py"),
        ),
    )

    monkeypatch.setattr(knowledge_index, "discover", lambda *_args, **_kwargs: report)
    with pytest.raises(ValidationFailed, match="tutarsiz"):
        build_project_index_plan(
            project_id=uuid4(),
            project_slug="gpu",
            source_root=root,
            source_revision="abc123",
            expected_tree_digest=report.tree_digest,
        )


def test_project_plan_rejects_files_entry_without_eligibility_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fail-closed: kesif dosya kumesindeki girdinin eligibility karari yoksa
    # (secim `files`'tan turetilse gizli kalir) plan reddedilir.
    root = tmp_path / "gpu"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Service.java").write_text(
        "package app;\npublic class Service {\n  void run() {}\n}\n", encoding="utf-8"
    )
    report = discover(root)
    report = dataclasses.replace(
        report,
        eligibility_decisions=tuple(
            item
            for item in report.eligibility_decisions
            if item.relative_path != "src/Service.java"
        ),
    )

    monkeypatch.setattr(knowledge_index, "discover", lambda *_args, **_kwargs: report)
    with pytest.raises(ValidationFailed, match="eligibility karari tasimiyor"):
        build_project_index_plan(
            project_id=uuid4(),
            project_slug="gpu",
            source_root=root,
            source_revision="abc123",
            expected_tree_digest=report.tree_digest,
        )


def test_project_plan_rejects_inventory_eligibility_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail-closed: eligible dosyanin envanter girdisi uyumsuz (skipped, is_text
    # degil) ya da envanter kaydi eksikse plan reddedilir.
    root = tmp_path / "gpu"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Service.java").write_text(
        "package app;\npublic class Service {\n  void run() {}\n}\n", encoding="utf-8"
    )
    report = discover(root)
    path = "src/Service.java"
    mismatched = dataclasses.replace(
        report,
        inventory=tuple(
            dataclasses.replace(entry, is_text=False) if entry.relative_path == path else entry
            for entry in report.inventory
        ),
    )
    monkeypatch.setattr(knowledge_index, "discover", lambda *_args, **_kwargs: mismatched)
    with pytest.raises(ValidationFailed, match="uyumsuz"):
        build_project_index_plan(
            project_id=uuid4(),
            project_slug="gpu",
            source_root=root,
            source_revision="abc123",
            expected_tree_digest=report.tree_digest,
        )
    missing_entry = dataclasses.replace(
        report,
        inventory=tuple(
            entry
            for entry in report.inventory
            if not (entry.relative_path == path and entry.kind is FileKind.FILE)
        ),
    )
    monkeypatch.setattr(knowledge_index, "discover", lambda *_args, **_kwargs: missing_entry)
    with pytest.raises(ValidationFailed, match="envanter kaydi yok"):
        build_project_index_plan(
            project_id=uuid4(),
            project_slug="gpu",
            source_root=root,
            source_revision="abc123",
            expected_tree_digest=report.tree_digest,
        )


def test_project_plan_rejects_duplicate_eligible_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail-closed: ayni yolun iki eligible karari karar kumesini bozar.
    root = tmp_path / "gpu"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Service.java").write_text(
        "package app;\npublic class Service {\n  void run() {}\n}\n", encoding="utf-8"
    )
    report = discover(root)
    report = dataclasses.replace(
        report,
        eligibility_decisions=(
            *report.eligibility_decisions,
            EligibilityDecision.eligible("src/Service.java"),
        ),
    )

    monkeypatch.setattr(knowledge_index, "discover", lambda *_args, **_kwargs: report)
    with pytest.raises(ValidationFailed, match="yineleniyor"):
        build_project_index_plan(
            project_id=uuid4(),
            project_slug="gpu",
            source_root=root,
            source_revision="abc123",
            expected_tree_digest=report.tree_digest,
        )


def test_project_plan_selects_only_allowed_eligible_file(
    tmp_path: Path,
) -> None:
    # Bounded allowlist, diger eligible dosyalarin varliginda da alt kume secer.
    root = tmp_path / "gpu"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Service.java").write_text(
        "package app;\npublic class Service {\n  void run() {}\n}\n", encoding="utf-8"
    )
    (root / "src" / "Other.java").write_text(
        "package app;\npublic class Other {\n  void run() {}\n}\n", encoding="utf-8"
    )
    report = discover(root)
    assert {
        item.relative_path
        for item in report.eligibility_decisions
        if item.outcome is EligibilityOutcome.ELIGIBLE
    } == {"src/Service.java", "src/Other.java"}

    plan = build_project_index_plan(
        project_id=uuid4(),
        project_slug="gpu",
        source_root=root,
        source_revision="abc123",
        expected_tree_digest=report.tree_digest,
        allowed_relative_paths=("src/Service.java",),
    )
    assert plan.selected_file_count == 1
    assert {unit.locator.relative_path for unit in plan.document.units} == {"src/Service.java"}


def test_project_plan_selection_ignores_eligible_binary_file(tmp_path: Path) -> None:
    # Kesifin `is_text` metadata'si (NUL bayt saptamasi) eligibility karariyla
    # birlikte secimi belirler: ikili (NUL'lu) eligible girdi metin filtresi
    # ile secime giremez; decode-edilemeyen ama NUL'suz girdi secime girer ve
    # ikinci okumada sessiz atlanir (geriye uyum).
    root = tmp_path / "gpu"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Service.java").write_text(
        "package app;\npublic class Service {\n  void run() {}\n}\n", encoding="utf-8"
    )
    (root / "src" / "legacy.java").write_bytes(b"class Legacy { // \xff }")
    (root / "src" / "nul.java").write_bytes(b"class Nul { // \x00 }")
    report = discover(root)
    by_path = {item.relative_path: item for item in report.files}
    assert by_path["src/legacy.java"].is_text is True
    assert by_path["src/nul.java"].is_text is False

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
    assert all(unit.locator.relative_path == "src/Service.java" for unit in plan.document.units)


def test_project_plan_rejects_duplicate_files_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail-closed: kesif dosya kumesinde ayni yol iki kez tasiyorsa dogrulama
    # haritasi bozulur ve plan reddedilir.
    root = tmp_path / "gpu"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Service.java").write_text(
        "package app;\npublic class Service {\n  void run() {}\n}\n", encoding="utf-8"
    )
    report = discover(root)
    duplicate = next(item for item in report.files if item.relative_path == "src/Service.java")
    report = dataclasses.replace(report, files=(*report.files, duplicate))

    monkeypatch.setattr(knowledge_index, "discover", lambda *_args, **_kwargs: report)
    with pytest.raises(ValidationFailed, match="tutarsiz"):
        build_project_index_plan(
            project_id=uuid4(),
            project_slug="gpu",
            source_root=root,
            source_revision="abc123",
            expected_tree_digest=report.tree_digest,
        )


def test_project_plan_rejects_files_entry_drifted_from_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fail-closed: eligible dosyanin envanter metadata'si (boyut) ile kesif
    # dosya girdisi uyusmuyorsa plan reddedilir.
    root = tmp_path / "gpu"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Service.java").write_text(
        "package app;\npublic class Service {\n  void run() {}\n}\n", encoding="utf-8"
    )
    report = discover(root)
    report = dataclasses.replace(
        report,
        files=tuple(
            dataclasses.replace(item, size_bytes=item.size_bytes + 1)
            if item.relative_path == "src/Service.java"
            else item
            for item in report.files
        ),
    )

    monkeypatch.setattr(knowledge_index, "discover", lambda *_args, **_kwargs: report)
    with pytest.raises(ValidationFailed, match="uyumsuz"):
        build_project_index_plan(
            project_id=uuid4(),
            project_slug="gpu",
            source_root=root,
            source_revision="abc123",
            expected_tree_digest=report.tree_digest,
        )


def test_project_plan_budget_cancels_between_source_parsers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from zekam.application.provider_adapter import ProviderCallCancelled

    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text("# Source\nbounded content", encoding="utf-8")
    report = discover(tmp_path)
    parsed: list[str] = []
    original = knowledge_index._file_units

    def parse(path: str, text: str, order: int):
        parsed.append(path)
        return original(path, text, order)

    def check_budget() -> None:
        if parsed:
            raise ProviderCallCancelled("cancelled after first source parser")

    monkeypatch.setattr(knowledge_index, "_file_units", parse)
    with pytest.raises(ProviderCallCancelled, match="after first source parser"):
        build_project_index_plan(
            project_id=uuid4(),
            project_slug="budget-source",
            source_root=tmp_path,
            source_revision="test-revision",
            expected_tree_digest=report.tree_digest,
            check_budget=check_budget,
        )
    assert len(parsed) == 1
