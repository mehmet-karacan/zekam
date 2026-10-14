from __future__ import annotations

from dataclasses import replace

import pytest

from zekam.application.model_qualification import build_qualification_report
from zekam.application.model_registry import load_inventory
from zekam.domain.canonical import digest
from zekam.domain.model_inventory import BenchmarkState, HealthState

pytestmark = pytest.mark.unit


def test_inventory_qualification_report_keeps_unmeasured_signals_unknown() -> None:
    report = build_qualification_report(load_inventory().records)

    assert report["schema"] == "zekam-model-qualification/v1"
    assert report["catalog_revision"] is None
    assert report["quota"]["status"] == "unknown"
    assert report["qualification_counts"] == {"unknown": 20}
    for row in report["models"]:
        assert row["provider_protocol"] in {"openai", "hosted_vllm", "anthropic", "custom_openai"}
        assert row["qualification"] == "unknown"
        assert row["signals"]["quality"] == "unknown"
        assert row["signals"]["latency"] == "unknown"
        assert row["signals"]["failure"] == "unknown"
        assert row["signals"]["cost"] in {"observed", "unknown"}


def test_qualification_report_does_not_infer_catalog_or_quota_from_model_rows() -> None:
    report = build_qualification_report(load_inventory().records)

    assert report["catalog"]["status"] == "unknown"
    assert report["catalog"]["model_count"] is None
    assert report["quota"] == {"status": "unknown", "remaining_ratio": None}


def test_qualification_uses_only_explicit_health_benchmark_and_capability_evidence() -> None:
    record = replace(
        load_inventory().records[0],
        health_state=HealthState.PROJECT_QUALIFIED,
        benchmark_state=BenchmarkState.PASSED,
        capabilities_verified=("context-limit", "json-schema", "tool-call"),
    )
    report = build_qualification_report(
        (record,), catalog_model_ids=(record.model_id,), catalog_revision=digest("catalog")
    )
    row = report["models"][0]
    assert row["configured"] == "configured"
    assert row["qualification"] == "qualified"
    assert row["signals"] == {
        "quality": "observed",
        "tools": "verified",
        "structured_output": "verified",
        "context": "verified",
        "latency": "unknown",
        "failure": "unknown",
        "cost": "observed",
    }\n