"""Strict loader for the persistent 14-family benchmark task catalog."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from zekam.domain.canonical import digest, digest_of_bytes, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.model_benchmark import (
    BENCHMARK_TASK_FAMILIES,
    REQUIRED_SCORE_DIMENSIONS,
    BenchmarkTaskFamily,
)

_TASK_KEYS = frozenset(
    {
        "schema",
        "task_id",
        "version",
        "family",
        "evaluation_class",
        "workload",
        "modality",
        "prompt_ref",
        "fixture_refs",
        "hidden_key_ref",
        "grader_ref",
        "required_tools",
        "forbidden_effects",
        "data_classification",
        "repetitions",
        "timeout_seconds",
        "max_input_tokens",
        "max_output_tokens",
        "scoring_dimensions",
        "pass_thresholds",
        "resource_digests",
    }
)
_GRADER_KEYS = frozenset(
    {
        "schema",
        "grader_id",
        "version",
        "family",
        "evaluation_class",
        "dimensions",
        "hidden_key_model_visible",
    }
)
_MAX_RESOURCE_BYTES = 1_048_576


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: _UniqueLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValidationFailed("Benchmark catalog duplicate YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def default_catalog_root() -> Path:
    return Path(__file__).resolve().parents[1] / "benchmarks"


def _yaml_document(path: Path) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PolicyViolation("Benchmark catalog YAML unreadable") from exc
    if type(value) is not dict:
        raise ValidationFailed("Benchmark catalog YAML object required")
    return value


def _resource(root: Path, ref: object, expected_digest: object) -> tuple[Path, bytes]:
    if type(ref) is not str or type(expected_digest) is not str:
        raise ValidationFailed("Benchmark resource ref/digest exact text required")
    parse_digest(expected_digest)
    logical = PurePosixPath(ref)
    if logical.is_absolute() or ".." in logical.parts or logical.as_posix() != ref:
        raise PolicyViolation("Benchmark resource ref must be relative POSIX")
    candidate = root / ref
    if any(part.is_symlink() for part in (candidate, *candidate.parents) if part != root.parent):
        raise PolicyViolation("Benchmark catalog resource symlink forbidden")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        payload = resolved.read_bytes()
    except (OSError, ValueError) as exc:
        raise PolicyViolation("Benchmark catalog resource missing or escaped") from exc
    if not resolved.is_file() or not 0 < len(payload) <= _MAX_RESOURCE_BYTES:
        raise PolicyViolation("Benchmark catalog resource type/size invalid")
    if digest_of_bytes(payload) != expected_digest:
        raise PolicyViolation("Benchmark catalog resource digest drift")
    return resolved, payload


def _text_tuple(value: object, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if type(value) is not list or (not value and not allow_empty):
        raise ValidationFailed(f"Benchmark task {label} list invalid")
    result = tuple(value)
    if any(type(item) is not str or not item for item in result) or len(set(result)) != len(result):
        raise ValidationFailed(f"Benchmark task {label} values invalid")
    return result


@dataclass(frozen=True, slots=True)
class BenchmarkCatalogTask:
    family: BenchmarkTaskFamily
    evaluation_class: str
    task_id: str
    version: int
    repetitions: int
    scoring_dimensions: tuple[str, ...]
    pass_thresholds: tuple[tuple[str, float], ...]
    resource_digests: tuple[tuple[str, str], ...]
    task_digest: str


@dataclass(frozen=True, slots=True)
class BenchmarkCatalog:
    tasks: tuple[BenchmarkCatalogTask, ...]
    catalog_digest: str


@dataclass(frozen=True, slots=True)
class BenchmarkCatalogDryRun:
    catalog_digest: str
    task_digest: str
    family: BenchmarkTaskFamily
    trial_count: int
    call_count: int
    max_calls: int

    @property
    def report_digest(self) -> str:
        return digest(
            {
                "schema": "zekam-benchmark-catalog-dry-run/v1",
                "catalog_digest": self.catalog_digest,
                "task_digest": self.task_digest,
                "family": self.family.value,
                "trial_count": self.trial_count,
                "call_count": self.call_count,
                "max_calls": self.max_calls,
                "provider_calls_performed": 0,
            }
        )


def catalog_dry_run(
    catalog: BenchmarkCatalog, family: BenchmarkTaskFamily, *, max_calls: int
) -> BenchmarkCatalogDryRun:
    if type(catalog) is not BenchmarkCatalog or type(family) is not BenchmarkTaskFamily:
        raise ValidationFailed("Exact benchmark catalog/family required")
    if type(max_calls) is not int or max_calls < 0:
        raise ValidationFailed("Benchmark catalog max calls invalid")
    selected = tuple(task for task in catalog.tasks if task.family is family)
    if len(selected) != 1:
        raise PolicyViolation("Benchmark catalog exact family task missing or duplicate")
    repetitions = selected[0].repetitions
    call_count = repetitions * 2
    if call_count > max_calls:
        raise PolicyViolation("Benchmark catalog dry-run call budget exceeded")
    return BenchmarkCatalogDryRun(
        catalog.catalog_digest,
        selected[0].task_digest,
        family,
        repetitions,
        call_count,
        max_calls,
    )


def load_benchmark_catalog(root: Path | None = None) -> BenchmarkCatalog:
    target = (root or default_catalog_root()).resolve(strict=True)
    if target.is_symlink() or not target.is_dir():
        raise PolicyViolation("Benchmark catalog root invalid")
    tasks: list[BenchmarkCatalogTask] = []
    for family in BENCHMARK_TASK_FAMILIES:
        path = target / "suites" / family.value / "task.yaml"
        document = _yaml_document(path)
        if set(document) != _TASK_KEYS or document.get("schema") != "zekam-benchmark-task/v1":
            raise ValidationFailed("Benchmark task exact v1 schema required")
        if document.get("family") != family.value:
            raise PolicyViolation("Benchmark task directory/family drift")
        evaluation_class = document.get("evaluation_class")
        expected_class = (
            "creative" if family is BenchmarkTaskFamily.CREATIVE_TOURNAMENT else "technical"
        )
        if evaluation_class != expected_class:
            raise PolicyViolation("Benchmark technical/creative class drift")
        if (
            type(document.get("task_id")) is not str
            or not document["task_id"]
            or type(document.get("version")) is not int
            or document["version"] < 1
            or type(document.get("workload")) is not str
            or not document["workload"]
            or type(document.get("modality")) is not str
            or not document["modality"]
            or document.get("data_classification") != "public"
            or type(document.get("repetitions")) is not int
            or document["repetitions"] < 5
            or type(document.get("timeout_seconds")) is not int
            or not 1 <= document["timeout_seconds"] <= 600
        ):
            raise ValidationFailed("Benchmark task scalar contract invalid")
        for optional_limit in ("max_input_tokens", "max_output_tokens"):
            value = document[optional_limit]
            if value is not None and (type(value) is not int or value < 1):
                raise ValidationFailed("Benchmark task token limit invalid")
        _text_tuple(document["required_tools"], "required tools", allow_empty=True)
        _text_tuple(document["forbidden_effects"], "forbidden effects")
        dimensions = _text_tuple(document["scoring_dimensions"], "scoring dimensions")
        if any(item not in REQUIRED_SCORE_DIMENSIONS for item in dimensions):
            raise ValidationFailed("Benchmark task scoring dimension unknown")
        thresholds = document["pass_thresholds"]
        if type(thresholds) is not dict or set(thresholds) != set(dimensions):
            raise ValidationFailed("Benchmark task pass thresholds drift")
        if any(
            type(value) is not float or not 0.0 <= value <= 1.0 for value in thresholds.values()
        ):
            raise ValidationFailed("Benchmark task threshold exact float invalid")
        fixture_refs = _text_tuple(document["fixture_refs"], "fixture refs")
        refs = (
            document["prompt_ref"],
            *fixture_refs,
            document["hidden_key_ref"],
            document["grader_ref"],
        )
        resource_digests = document["resource_digests"]
        if type(resource_digests) is not dict or set(resource_digests) != set(refs):
            raise ValidationFailed("Benchmark task resource digest coverage invalid")
        resources = {ref: _resource(target, ref, resource_digests[ref]) for ref in refs}
        grader = _yaml_document(resources[document["grader_ref"]][0])
        if (
            set(grader) != _GRADER_KEYS
            or grader.get("schema") != "zekam-benchmark-grader/v1"
            or grader.get("family") != family.value
            or grader.get("evaluation_class") != expected_class
            or grader.get("dimensions") != list(dimensions)
            or grader.get("hidden_key_model_visible") is not False
        ):
            raise PolicyViolation("Benchmark task-specific grader drift")
        for fixture_ref in fixture_refs:
            try:
                fixture = json.loads(resources[fixture_ref][1])
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PolicyViolation("Benchmark fixture JSON invalid") from exc
            if (
                type(fixture) is not dict
                or fixture.get("schema") != "zekam-benchmark-fixture/v1"
                or fixture.get("family") != family.value
            ):
                raise PolicyViolation("Benchmark fixture family/schema drift")
        task_digest = digest(document)
        tasks.append(
            BenchmarkCatalogTask(
                family=family,
                evaluation_class=expected_class,
                task_id=document["task_id"],
                version=document["version"],
                repetitions=document["repetitions"],
                scoring_dimensions=dimensions,
                pass_thresholds=tuple(sorted(thresholds.items())),
                resource_digests=tuple(sorted(resource_digests.items())),
                task_digest=task_digest,
            )
        )
    if len({task.task_id for task in tasks}) != len(tasks):
        raise ValidationFailed("Benchmark task identity duplicate")
    body = [
        {
            "family": task.family.value,
            "evaluation_class": task.evaluation_class,
            "task_id": task.task_id,
            "version": task.version,
            "repetitions": task.repetitions,
            "scoring_dimensions": list(task.scoring_dimensions),
            "pass_thresholds": dict(task.pass_thresholds),
            "resource_digests": dict(task.resource_digests),
            "task_digest": task.task_digest,
        }
        for task in tasks
    ]
    return BenchmarkCatalog(tuple(tasks), digest(body))
