from __future__ import annotations

import json
from pathlib import Path

import pytest

from zekam.application.portable_benchmark import inspect_portable_benchmark
from zekam.domain.errors import PolicyViolation


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _fixture(root: Path) -> None:
    _write(
        root / "config" / "benchmark.json",
        {
            "benchmark_version": "1",
            "execution": {
                "max_requests": 5,
                "max_estimated_cost_usd": 0.1,
                "timeout_seconds": 10,
                "max_retries": 1,
                "concurrency": 2,
            },
            "storage": {"immutable_run_directories": True, "store_raw_outputs": False},
            "capabilities": {
                "default_enabled": ["chat"],
                "opt_in": ["audio"],
                "high_cost": ["audio"],
            },
        },
    )
    _write(
        root / "config" / "models.json",
        {
            "catalog_version": "1",
            "models": [
                {
                    "alias": "mock",
                    "endpoint_type": "mock",
                    "capabilities": ["chat"],
                    "real_model": False,
                    "enabled": True,
                }
            ],
        },
    )
    _write(root / "config" / "suites.json", {"suites": [{"id": "smoke"}]})
    _write(root / "config" / "release-gates.json", {"gates": [{"id": "chat"}]})
    dataset = root / "datasets" / "smoke.jsonl"
    dataset.parent.mkdir(parents=True)
    dataset.write_text(json.dumps({"id": "one", "capability": "chat"}) + "\n")


def test_portable_benchmark_inspection_is_read_only_and_capability_based(tmp_path: Path) -> None:
    _fixture(tmp_path)

    document = inspect_portable_benchmark(tmp_path)

    assert document["models"] == {
        "total": 1,
        "enabled": 1,
        "real": 0,
        "mock": 1,
        "endpoint_type_counts": {"mock": 1},
        "capability_counts": {"chat": 1},
    }
    assert document["tasks"] == {"total": 1, "enabled": 1}
    assert document["policy"]["opt_in_capabilities"] == ["audio"]
    assert document["provider_calls"] == 0
    assert document["foreign_code_executed"] is False
    assert document["grants_authority"] is False


def test_portable_benchmark_inspection_rejects_secret_like_dataset(tmp_path: Path) -> None:
    _fixture(tmp_path)
    unsafe_prompt = "password" + "='not-safe-value-123'"
    (tmp_path / "datasets" / "smoke.jsonl").write_text(
        json.dumps({"capability": "chat", "prompt": unsafe_prompt}) + "\n"
    )

    with pytest.raises(PolicyViolation, match="secret-benzeri"):
        inspect_portable_benchmark(tmp_path)
