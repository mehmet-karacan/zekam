"""Safe, provider-free inspection of a portable model benchmark package."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zekam.application.secret_detection import scan_text
from zekam.domain.canonical import digest
from zekam.domain.errors import ConfigurationError, PolicyViolation, ValidationFailed

PORTABLE_BENCHMARK_SCHEMA = "zekam-portable-benchmark-inspection/v1"
_MAX_CONFIG_BYTES = 2 * 1024 * 1024
_MAX_DATASET_BYTES = 8 * 1024 * 1024
_MAX_DATASETS = 100


def _strict_object(raw: bytes, *, label: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value

    try:
        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationFailed(f"Portable benchmark {label} exact JSON olmali") from exc
    if not isinstance(document, dict):
        raise ValidationFailed(f"Portable benchmark {label} JSON object olmali")
    return document


def _read_bounded(root: Path, relative: str, *, maximum: int) -> bytes:
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
        size = resolved.stat().st_size
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f"Portable benchmark gerekli dosyasi okunamadi: {relative}"
        ) from exc
    if path.is_symlink() or not resolved.is_file() or not 0 < size <= maximum:
        raise PolicyViolation(f"Portable benchmark dosya kimligi/boyutu gecersiz: {relative}")
    try:
        raw = resolved.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"Portable benchmark dosyasi okunamadi: {relative}") from exc
    if scan_text(raw.decode("utf-8", "replace"), relative_path=relative):
        raise PolicyViolation(f"Portable benchmark secret-benzeri icerik tasiyor: {relative}")
    return raw


def _string_list(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValidationFailed(f"Portable benchmark {label} string list olmali")
    return tuple(dict.fromkeys(item.strip() for item in value))


def inspect_portable_benchmark(root: Path) -> dict[str, Any]:
    """Inspect catalog/config/datasets without importing or executing foreign code."""

    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise ConfigurationError("Portable benchmark root bulunamadi") from exc
    if not resolved_root.is_dir() or resolved_root.is_symlink():
        raise PolicyViolation("Portable benchmark root real directory olmali")

    benchmark_raw = _read_bounded(resolved_root, "config/benchmark.json", maximum=_MAX_CONFIG_BYTES)
    models_raw = _read_bounded(resolved_root, "config/models.json", maximum=_MAX_CONFIG_BYTES)
    suites_raw = _read_bounded(resolved_root, "config/suites.json", maximum=_MAX_CONFIG_BYTES)
    gates_raw = _read_bounded(resolved_root, "config/release-gates.json", maximum=_MAX_CONFIG_BYTES)
    benchmark = _strict_object(benchmark_raw, label="benchmark config")
    catalog = _strict_object(models_raw, label="model catalog")
    suites = _strict_object(suites_raw, label="suite catalog")
    gates = _strict_object(gates_raw, label="release gates")

    models = catalog.get("models")
    if not isinstance(models, list) or not models:
        raise ValidationFailed("Portable benchmark model catalog bos veya gecersiz")
    aliases: set[str] = set()
    endpoint_counts: dict[str, int] = {}
    capability_counts: dict[str, int] = {}
    real_models = 0
    enabled_models = 0
    for model in models:
        if not isinstance(model, dict):
            raise ValidationFailed("Portable benchmark model girdisi object olmali")
        alias = str(model.get("alias", "")).strip()
        endpoint = str(model.get("endpoint_type", "")).strip()
        capabilities = _string_list(model.get("capabilities"), label="model capabilities")
        if not alias or alias in aliases or not endpoint:
            raise ValidationFailed("Portable benchmark model kimligi tekrarli/gecersiz")
        aliases.add(alias)
        endpoint_counts[endpoint] = endpoint_counts.get(endpoint, 0) + 1
        for capability in capabilities:
            capability_counts[capability] = capability_counts.get(capability, 0) + 1
        real_models += int(model.get("real_model") is True)
        enabled_models += int(model.get("enabled") is True)

    capabilities_document = benchmark.get("capabilities")
    execution = benchmark.get("execution")
    storage = benchmark.get("storage")
    if not isinstance(capabilities_document, dict):
        raise ValidationFailed("Portable benchmark capabilities policy eksik")
    if not isinstance(execution, dict):
        raise ValidationFailed("Portable benchmark execution policy eksik")
    if not isinstance(storage, dict):
        raise ValidationFailed("Portable benchmark policy bolumleri eksik")
    default_enabled = _string_list(
        capabilities_document.get("default_enabled"), label="default capabilities"
    )
    opt_in = _string_list(capabilities_document.get("opt_in"), label="opt-in capabilities")
    high_cost = _string_list(capabilities_document.get("high_cost"), label="high-cost capabilities")

    dataset_root = resolved_root / "datasets"
    try:
        datasets = tuple(sorted(dataset_root.rglob("*.jsonl")))
    except OSError as exc:
        raise ConfigurationError("Portable benchmark datasetleri listelenemedi") from exc
    if not datasets or len(datasets) > _MAX_DATASETS:
        raise PolicyViolation("Portable benchmark dataset sayisi bounded olmali")
    task_count = 0
    enabled_task_count = 0
    dataset_digests: list[str] = []
    for path in datasets:
        relative = path.relative_to(resolved_root).as_posix()
        raw = _read_bounded(resolved_root, relative, maximum=_MAX_DATASET_BYTES)
        dataset_digests.append(digest(raw.decode("utf-8")))
        for line_number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            task = _strict_object(line.encode("utf-8"), label=f"{relative}:{line_number}")
            task_capability = task.get("capability")
            if not isinstance(task_capability, str) or not task_capability.strip():
                raise ValidationFailed("Portable benchmark task capability eksik")
            task_count += 1
            enabled_task_count += int(task.get("enabled", True) is True)

    suite_entries = suites.get("suites")
    if not isinstance(suite_entries, (list, dict)):
        raise ValidationFailed("Portable benchmark suite catalog gecersiz")
    release_gate_entries = gates.get("gates")
    if not isinstance(release_gate_entries, (list, dict)):
        raise ValidationFailed("Portable benchmark release gate catalog gecersiz")

    body = {
        "schema": PORTABLE_BENCHMARK_SCHEMA,
        "benchmark_version": str(benchmark.get("benchmark_version", "unknown")),
        "catalog_version": str(catalog.get("catalog_version", "unknown")),
        "models": {
            "total": len(models),
            "enabled": enabled_models,
            "real": real_models,
            "mock": len(models) - real_models,
            "endpoint_type_counts": dict(sorted(endpoint_counts.items())),
            "capability_counts": dict(sorted(capability_counts.items())),
        },
        "tasks": {"total": task_count, "enabled": enabled_task_count},
        "policy": {
            "default_enabled_capabilities": list(default_enabled),
            "opt_in_capabilities": list(opt_in),
            "high_cost_capabilities": list(high_cost),
            "immutable_run_directories": storage.get("immutable_run_directories") is True,
            "raw_outputs_default": storage.get("store_raw_outputs") is True,
            "max_requests": execution.get("max_requests"),
            "max_estimated_cost_usd": execution.get("max_estimated_cost_usd"),
            "timeout_seconds": execution.get("timeout_seconds"),
            "retries": execution.get("max_retries"),
            "concurrency": execution.get("concurrency"),
        },
        "suite_count": len(suite_entries),
        "release_gate_count": len(release_gate_entries),
        "source_digest": digest(
            {
                "benchmark": digest(benchmark),
                "models": digest(catalog),
                "suites": digest(suites),
                "release_gates": digest(gates),
                "datasets": dataset_digests,
            }
        ),
        "provider_calls": 0,
        "foreign_code_executed": False,
        "read_only": True,
        "grants_authority": False,
    }
    return body | {"inspection_digest": digest(body)}
