"""Persistent benchmark catalog happy-path and adversarial drift acceptance."""

from __future__ import annotations

import shutil
from dataclasses import fields
from pathlib import Path

import pytest

from zekam.application.benchmark_catalog import (
    BenchmarkCatalogTask,
    catalog_dry_run,
    default_catalog_root,
    load_benchmark_catalog,
)
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import REQUIRED_SCORE_DIMENSIONS, BenchmarkTaskFamily

ROOT = Path(__file__).resolve().parents[2]


def _copy(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "catalog"
    shutil.copytree(default_catalog_root(), target)
    return target


def test_persistent_catalog_has_exact_fourteen_bound_families_and_rebuild_parity() -> None:
    packaged = load_benchmark_catalog()
    source = load_benchmark_catalog(ROOT / "benchmarks")
    assert len(packaged.tasks) == 14
    assert packaged.catalog_digest == source.catalog_digest
    assert {task.evaluation_class for task in packaged.tasks} == {"technical", "creative"}
    assert set().union(*(set(task.scoring_dimensions) for task in packaged.tasks)) <= set(
        REQUIRED_SCORE_DIMENSIONS
    )
    assert {field.name for field in fields(BenchmarkCatalogTask)}.isdisjoint(
        {"prompt", "fixture", "hidden_key", "grader_body"}
    )
    report = catalog_dry_run(packaged, BenchmarkTaskFamily.CODE_REPAIR, max_calls=10)
    assert report.trial_count == 5
    assert report.call_count == 10
    assert report.report_digest.startswith("sha256:")
    with pytest.raises(PolicyViolation, match="budget"):
        catalog_dry_run(packaged, BenchmarkTaskFamily.CODE_REPAIR, max_calls=9)


def test_catalog_resource_tamper_and_missing_resource_fail_closed(tmp_path: Path) -> None:
    target = _copy(tmp_path)
    fixture = target / "resources" / "fixtures" / "code-repair.json"
    fixture.write_text("{}\n", encoding="utf-8")
    with pytest.raises(PolicyViolation, match="digest drift"):
        load_benchmark_catalog(target)
    target = _copy(tmp_path / "missing")
    (target / "resources" / "prompts" / "architecture.txt").unlink()
    with pytest.raises(PolicyViolation, match="missing"):
        load_benchmark_catalog(target)


def test_catalog_duplicate_yaml_key_and_wrong_threshold_type_fail_closed(tmp_path: Path) -> None:
    target = _copy(tmp_path)
    task = target / "suites" / "sql-plsql" / "task.yaml"
    task.write_text(task.read_text(encoding="utf-8") + "task_id: duplicate\n", encoding="utf-8")
    with pytest.raises(ValidationFailed, match="duplicate YAML key"):
        load_benchmark_catalog(target)
    target = _copy(tmp_path / "wrong-type")
    task = target / "suites" / "sql-plsql" / "task.yaml"
    task.write_text(
        task.read_text(encoding="utf-8").replace("correctness: 0.8", "correctness: '0.8'"),
        encoding="utf-8",
    )
    with pytest.raises(ValidationFailed, match="threshold exact float"):
        load_benchmark_catalog(target)


def test_catalog_symlink_resource_and_creative_class_drift_fail_closed(tmp_path: Path) -> None:
    target = _copy(tmp_path)
    prompt = target / "resources" / "prompts" / "tool-use.txt"
    prompt.unlink()
    prompt.symlink_to(target / "resources" / "prompts" / "code-repair.txt")
    with pytest.raises(PolicyViolation, match="symlink"):
        load_benchmark_catalog(target)
    target = _copy(tmp_path / "creative")
    task = target / "suites" / "creative-tournament" / "task.yaml"
    task.write_text(
        task.read_text(encoding="utf-8").replace(
            "evaluation_class: creative", "evaluation_class: technical"
        ),
        encoding="utf-8",
    )
    with pytest.raises(PolicyViolation, match="technical/creative"):
        load_benchmark_catalog(target)
