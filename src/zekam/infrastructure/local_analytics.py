"""Rebuildable, authority-free local DuckDB analytics projection."""

from __future__ import annotations

import datetime as dt
import importlib
import json
import math
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, final

from zekam.domain.canonical import canonical_json, digest, digest_of_bytes, parse_digest
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.infrastructure.local_file_security import (
    private_directory as _path_private_directory,
)
from zekam.infrastructure.local_file_security import private_regular as _path_private_regular
from zekam.infrastructure.local_file_security import restrict_private_tree

MAX_SEGMENT_BYTES = 2_097_152
MAX_EVENTS = 4096
MAX_REPORT_PROJECTS = 128
MAX_METRIC_VALUE = 9_007_199_254_740_991.0
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}$")
_FILE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SECRET = re.compile(
    r"(?i)(?:bearer\s+|password|credential|api[-_]?key|\b(?:sk|pk)-[A-Za-z0-9]{8,}|://)"
)
_EVENT_TYPES = frozenset(
    {
        "model.availability",
        "benchmark.trial",
        "benchmark.aggregate",
        "routing.decision",
        "routing.outcome",
        "rag.quality",
        "embedding.health",
        "runtime.outcome",
        "memory.effectiveness",
        "context.freshness",
        "resource.soak",
    }
)
_EVENT_CONTRACTS = {
    "benchmark.trial": (
        frozenset({"model_ref", "suite_ref", "trial_ref"}),
        frozenset({"correctness", "reliability", "latency_ms"}),
    ),
    "routing.decision": (
        frozenset({"decision_ref", "selected_model_ref"}),
        frozenset({"confidence", "candidate_count"}),
    ),
    "memory.effectiveness": (
        frozenset({"scope"}),
        frozenset({"hit_rate", "latency_ms"}),
    ),
    "context.freshness": (
        frozenset({"scope"}),
        frozenset({"budget_used", "freshness_seconds"}),
    ),
}
_RATE_METRICS = frozenset(
    {
        "availability",
        "citation",
        "citation_precision",
        "confidence",
        "correctness",
        "hit_rate",
        "mrr",
        "ndcg_at_10",
        "no_answer_precision",
        "no_answer_recall",
        "recall_at_1",
        "recall_at_5",
        "recall_at_10",
        "reliability",
        "safety",
        "score",
        "structured_format",
        "success",
        "useful_rate",
    }
)
_COUNT_METRICS = frozenset(
    {
        "budget_limit",
        "budget_used",
        "candidate_count",
        "count",
        "dimension",
        "error_count",
        "failure_count",
        "memory_bytes",
        "recovery_count",
        "success_count",
        "trial_count",
    }
)
_NONNEGATIVE_METRICS = frozenset(
    {
        "cpu_percent",
        "duration_seconds",
        "freshness_seconds",
        "latency_ms",
        "latency_p50_ms",
        "latency_p95_ms",
    }
)
_KNOWN_METRICS = _RATE_METRICS | _COUNT_METRICS | _NONNEGATIVE_METRICS
_EVENT_KEYS = frozenset(
    {
        "schema",
        "event_id",
        "event_type",
        "occurred_at",
        "project_ref",
        "work_ref",
        "run_ref",
        "session_ref",
        "component",
        "component_version",
        "adapter_version",
        "dimensions",
        "metrics",
        "source_digest",
        "contains_raw_prompt",
        "contains_secret",
        "grants_authority",
    }
)
_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "segment_id",
        "segment_digest",
        "event_count",
        "event_ids",
        "first_occurred_at",
        "last_occurred_at",
        "source_digests",
        "immutable",
    }
)
_DASHBOARDS = (
    "model_dashboard",
    "rag_dashboard",
    "runtime_dashboard",
    "context_dashboard",
    "memory_dashboard",
)
_DASHBOARD_ORDER = {
    "model_dashboard": "occurred_at,event_type,event_id",
    "rag_dashboard": "occurred_at,event_type,event_id",
    "runtime_dashboard": "occurred_at,event_type,event_id",
    "context_dashboard": "occurred_at,event_id",
    "memory_dashboard": "occurred_at,event_id",
}
_SUMMARY_COLUMNS = {
    "model": (
        "event_type",
        "event_count",
        "first_occurred_at",
        "refreshed_at",
        "availability_avg",
        "correctness_avg",
        "citation_avg",
        "structured_format_avg",
        "safety_avg",
        "reliability_avg",
        "latency_ms_avg",
        "latency_p50_ms_max",
        "latency_p95_ms_max",
        "confidence_avg",
        "success_avg",
        "trial_count",
    ),
    "rag": (
        "event_type",
        "event_count",
        "first_occurred_at",
        "refreshed_at",
        "recall_at_10_avg",
        "mrr_avg",
        "ndcg_at_10_avg",
        "citation_precision_avg",
        "no_answer_precision_avg",
        "no_answer_recall_avg",
        "latency_ms_avg",
        "error_count",
    ),
    "runtime": (
        "event_type",
        "event_count",
        "first_occurred_at",
        "refreshed_at",
        "latency_ms_avg",
        "failure_count",
        "recovery_count",
        "success_count",
        "cpu_percent_avg",
        "memory_bytes_max",
        "duration_seconds_max",
    ),
    "context": (
        "event_type",
        "event_count",
        "first_occurred_at",
        "refreshed_at",
        "budget_used_avg",
        "budget_limit_avg",
        "freshness_seconds_avg",
    ),
    "memory": (
        "event_type",
        "event_count",
        "first_occurred_at",
        "refreshed_at",
        "hit_rate_avg",
        "useful_rate_avg",
        "latency_ms_avg",
    ),
}
_SUMMARY_QUERIES = {
    "model": (
        "select event_type,count(*) event_count,min(occurred_at),max(occurred_at),"
        "avg(availability),avg(correctness),avg(citation),avg(structured_format),avg(safety),"
        "avg(reliability),avg(latency_ms),max(latency_p50_ms),max(latency_p95_ms),"
        "avg(confidence),avg(success),sum(trial_count) from model_dashboard "
        "group by event_type order by event_type"
    ),
    "rag": (
        "select event_type,count(*) event_count,min(occurred_at),max(occurred_at),"
        "avg(recall_at_10),avg(mrr),avg(ndcg_at_10),avg(citation_precision),"
        "avg(no_answer_precision),avg(no_answer_recall),avg(latency_ms),sum(error_count) "
        "from rag_dashboard group by event_type order by event_type"
    ),
    "runtime": (
        "select event_type,count(*) event_count,min(occurred_at),max(occurred_at),"
        "avg(latency_ms),sum(failure_count),sum(recovery_count),sum(success_count),"
        "avg(cpu_percent),max(memory_bytes),max(duration_seconds) from runtime_dashboard "
        "group by event_type order by event_type"
    ),
    "context": (
        "select event_type,count(*) event_count,min(occurred_at),max(occurred_at),"
        "avg(budget_used),avg(budget_limit),avg(freshness_seconds) from context_dashboard "
        "group by event_type order by event_type"
    ),
    "memory": (
        "select event_type,count(*) event_count,min(occurred_at),max(occurred_at),"
        "avg(hit_rate),avg(useful_rate),avg(latency_ms) from memory_dashboard "
        "group by event_type order by event_type"
    ),
}
_POINTER_KEYS = frozenset(
    {
        "schema",
        "generation_digest",
        "database_file",
        "source_manifest_digest",
        "raw_rows_digest",
        "aggregate_digest",
        "dashboard_digests",
        "morning_report_digest",
        "project_report_digest",
        "morning_report_file",
        "project_report_file",
        "refreshed_at",
        "fresh",
        "grants_authority",
        "rebuild_receipt_digest",
    }
)
_MORNING_REPORT_KEYS = frozenset(
    {
        "schema",
        "source_manifest_digest",
        "raw_rows_digest",
        "aggregate_digest",
        "dashboard_digests",
        "metric_summaries",
        "refreshed_at",
        "fresh",
        "authority",
    }
)
_PROJECT_REPORT_KEYS = frozenset(
    {
        "schema",
        "source_manifest_digest",
        "raw_rows_digest",
        "aggregate_digest",
        "dashboard_digests",
        "metric_summaries",
        "project_refs",
        "project_count",
        "project_refs_truncated",
        "fresh",
        "authority",
    }
)


def _token(value: object, label: str) -> str:
    if type(value) is not str or not _TOKEN.fullmatch(value) or _SECRET.search(value):
        raise ValidationFailed(f"Local analytics {label} invalid or sensitive")
    return value


def _optional_token(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _token(value, label)


def _file_token(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not _FILE_TOKEN.fullmatch(value)
        or value in {".", ".."}
        or ".." in value
    ):
        raise ValidationFailed(f"Local analytics {label} invalid")
    return value


def _stored_digest(value: object) -> str:
    if type(value) is not str:
        raise PolicyViolation("Local analytics digest type drift")
    try:
        parse_digest(value)
    except ValidationFailed as exc:
        raise PolicyViolation("Local analytics digest drift") from exc
    return value


def _stored_token(value: object, label: str) -> str:
    try:
        return _token(value, label)
    except ValidationFailed as exc:
        raise PolicyViolation(f"Local analytics stored {label} drift") from exc


def _stored_optional_token(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _stored_token(value, label)


def _stored_file_token(value: object, label: str) -> str:
    try:
        return _file_token(value, label)
    except ValidationFailed as exc:
        raise PolicyViolation(f"Local analytics stored {label} drift") from exc


def _instant(value: dt.datetime) -> str:
    if type(value) is not dt.datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationFailed("Local analytics timestamp must be timezone-aware")
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat()


def _parse_time(value: object) -> dt.datetime:
    if type(value) is not str:
        raise PolicyViolation("Local analytics timestamp type drift")
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise PolicyViolation("Local analytics timestamp drift") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PolicyViolation("Local analytics timestamp lacks timezone")
    return parsed.astimezone(dt.UTC)


def _strict_document(raw: bytes, *, maximum: int = MAX_SEGMENT_BYTES) -> dict[str, Any]:
    if type(raw) is not bytes or not 0 < len(raw) <= maximum:
        raise PolicyViolation("Local analytics document size invalid")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PolicyViolation("Local analytics document invalid") from exc
    if type(value) is not dict or canonical_json(value).encode() != raw:
        raise PolicyViolation("Local analytics document not canonical")
    return value


def _metric_summaries(connection: Any) -> dict[str, list[dict[str, object]]]:
    summaries: dict[str, list[dict[str, object]]] = {}
    for name in ("model", "rag", "runtime", "context", "memory"):
        rows = connection.execute(_SUMMARY_QUERIES[name]).fetchall()
        columns = _SUMMARY_COLUMNS[name]
        summaries[name] = [
            {column: row[index] for index, column in enumerate(columns)} for row in rows
        ]
    return summaries


def _private_regular(path: Path, mode: int) -> None:
    if not _path_private_regular(path, mode):
        raise PolicyViolation("Local analytics file identity invalid")


def _private_directory(path: Path, mode: int) -> None:
    if not _path_private_directory(path, mode):
        raise PolicyViolation("Local analytics private directory required")


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path: Path, payload: bytes, *, mode: int) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_file(temporary, path, mode=mode)
        _sync_directory(path.parent)
    finally:
        with suppress(FileNotFoundError):
            if os.name == "nt":
                os.chmod(temporary, 0o600)
            temporary.unlink()


def _replace_file(temporary: Path, target: Path, *, mode: int) -> None:
    """Publish a file without leaving Windows read-only attributes on replacements."""
    if os.name != "nt":
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        return
    target_existed = target.exists()
    if target_existed:
        _private_regular(target, mode)
        os.chmod(target, 0o600)
    try:
        os.replace(temporary, target)
    except BaseException:
        if target_existed and target.exists():
            os.chmod(target, mode)
        raise
    os.chmod(target, mode)


@dataclass(frozen=True, slots=True)
class RawAnalyticsEvent:
    event_id: str
    event_type: str
    occurred_at: dt.datetime
    project_ref: str
    component: str
    component_version: str
    adapter_version: str
    dimensions: tuple[tuple[str, str], ...]
    metrics: tuple[tuple[str, float], ...]
    source_digest: str
    work_ref: str | None = None
    run_ref: str | None = None
    session_ref: str | None = None

    def __post_init__(self) -> None:
        _token(self.event_id, "event id")
        if type(self.event_type) is not str or self.event_type not in _EVENT_TYPES:
            raise ValidationFailed("Local analytics event type invalid")
        _instant(self.occurred_at)
        for value, label in (
            (self.project_ref, "project ref"),
            (self.component, "component"),
            (self.component_version, "component version"),
            (self.adapter_version, "adapter version"),
        ):
            _token(value, label)
        for optional_value, label in (
            (self.work_ref, "work ref"),
            (self.run_ref, "run ref"),
            (self.session_ref, "session ref"),
        ):
            _optional_token(optional_value, label)
        parse_digest(self.source_digest)
        if type(self.dimensions) is not tuple or not 1 <= len(self.dimensions) <= 32:
            raise ValidationFailed("Local analytics dimensions invalid")
        if any(type(item) is not tuple or len(item) != 2 for item in self.dimensions):
            raise ValidationFailed("Local analytics dimensions invalid")
        for key, value in self.dimensions:
            _token(key, "dimension key")
            _token(value, "dimension value")
        if tuple(sorted(self.dimensions)) != self.dimensions or len(
            {item[0] for item in self.dimensions}
        ) != len(self.dimensions):
            raise ValidationFailed("Local analytics dimensions invalid")
        if type(self.metrics) is not tuple or not 1 <= len(self.metrics) <= 32:
            raise ValidationFailed("Local analytics metrics invalid")
        if any(type(item) is not tuple or len(item) != 2 for item in self.metrics):
            raise ValidationFailed("Local analytics metrics invalid")
        for key, metric_value in self.metrics:
            _token(key, "metric key")
            if type(metric_value) is not float or not math.isfinite(metric_value):
                raise ValidationFailed("Local analytics metric must be a finite float")
            if abs(metric_value) > MAX_METRIC_VALUE:
                raise ValidationFailed("Local analytics metric outside exact bound")
            if key not in _KNOWN_METRICS:
                raise ValidationFailed("Local analytics metric key unknown")
            if key in _RATE_METRICS and not 0.0 <= metric_value <= 1.0:
                raise ValidationFailed("Local analytics rate metric outside unit interval")
            if key in _COUNT_METRICS and (metric_value < 0.0 or not metric_value.is_integer()):
                raise ValidationFailed("Local analytics count metric invalid")
            if key in _NONNEGATIVE_METRICS and metric_value < 0.0:
                raise ValidationFailed("Local analytics nonnegative metric invalid")
            if key == "cpu_percent" and metric_value > 100.0:
                raise ValidationFailed("Local analytics cpu metric invalid")
        if tuple(sorted(self.metrics)) != self.metrics or len(
            {item[0] for item in self.metrics}
        ) != len(self.metrics):
            raise ValidationFailed("Local analytics metrics invalid")
        contract = _EVENT_CONTRACTS.get(self.event_type)
        if contract is not None:
            dimension_keys = {item[0] for item in self.dimensions}
            metric_keys = {item[0] for item in self.metrics}
            if not contract[0] <= dimension_keys or not contract[1] <= metric_keys:
                raise ValidationFailed("Local analytics event contract incomplete")

    def body(self) -> dict[str, object]:
        return {
            "schema": "zekam-local-analytics-event/v1",
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at": _instant(self.occurred_at),
            "project_ref": self.project_ref,
            "work_ref": self.work_ref,
            "run_ref": self.run_ref,
            "session_ref": self.session_ref,
            "component": self.component,
            "component_version": self.component_version,
            "adapter_version": self.adapter_version,
            "dimensions": dict(self.dimensions),
            "metrics": dict(self.metrics),
            "source_digest": self.source_digest,
            "contains_raw_prompt": False,
            "contains_secret": False,
            "grants_authority": False,
        }


@dataclass(frozen=True, slots=True)
class AnalyticsRebuildResult:
    generation_digest: str
    source_manifest_digest: str
    raw_rows_digest: str
    aggregate_digest: str
    model_dashboard_digest: str
    rag_dashboard_digest: str
    runtime_dashboard_digest: str
    context_dashboard_digest: str
    memory_dashboard_digest: str
    morning_report_digest: str
    project_report_digest: str
    rebuild_receipt_digest: str
    fresh: bool


@final
class LocalAnalyticsStore:
    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or root.is_symlink():
            raise ValidationFailed("Local analytics root invalid")
        self.root = root
        self.raw = root / "raw"
        self.manifests = root / "manifests"
        self.generations = root / "generations"
        self.quarantine = root / "quarantine"
        self.reports = root / "reports"
        self.receipts = root / "receipts"
        self.current = root / "current.json"
        self.lock = root / ".writer.lock"

    def bootstrap(self) -> None:
        created = not self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if created:
            restrict_private_tree(self.root)
        _private_directory(self.root, 0o700)
        for path in (
            self.raw,
            self.manifests,
            self.generations,
            self.quarantine,
            self.reports,
            self.receipts,
        ):
            path.mkdir(exist_ok=True, mode=0o700)
            _private_directory(path, 0o700)
        descriptor = os.open(
            self.lock,
            os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
            os.fsync(descriptor)
        os.close(descriptor)
        os.chmod(self.lock, 0o600)
        _private_regular(self.lock, 0o600)

    @contextmanager
    def _writer(self) -> Iterator[None]:
        _private_regular(self.lock, 0o600)
        descriptor = os.open(self.lock, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise ConcurrencyConflict("Local analytics writer already active") from exc
            else:
                fcntl: Any = importlib.import_module("fcntl")

                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise ConcurrencyConflict("Local analytics writer already active") from exc
            acquired = True
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl = importlib.import_module("fcntl")

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _immutable_artifact(path: Path, payload: bytes) -> None:
        if path.exists():
            _private_regular(path, 0o400)
            if path.read_bytes() != payload:
                raise ConcurrencyConflict("Local analytics derived artifact replay drift")
            return
        _atomic_bytes(path, payload, mode=0o400)

    def _quarantine_sources(self) -> None:
        moved = False
        for directory in (self.manifests, self.raw):
            for path in sorted(directory.iterdir()):
                if path.is_symlink() or not path.is_file():
                    continue
                suffix = digest_of_bytes(path.read_bytes())[7:23]
                target = self.quarantine / f"{path.name}.{suffix}.rejected"
                if target.exists():
                    raise ConcurrencyConflict("Local analytics quarantine collision")
                os.replace(path, target)
                moved = True
            _sync_directory(directory)
        if moved:
            _sync_directory(self.quarantine)

    def append_segment(self, segment_id: str, events: tuple[RawAnalyticsEvent, ...]) -> str:
        _file_token(segment_id, "segment id")
        if (
            type(events) is not tuple
            or not 1 <= len(events) <= MAX_EVENTS
            or any(type(item) is not RawAnalyticsEvent for item in events)
        ):
            raise ValidationFailed("Local analytics segment events invalid")
        bodies = [item.body() for item in events]
        event_ids = [str(item["event_id"]) for item in bodies]
        if len(set(event_ids)) != len(event_ids):
            raise ValidationFailed("Local analytics segment event ids duplicate")
        payload = b"".join(canonical_json(item).encode() + b"\n" for item in bodies)
        if len(payload) > MAX_SEGMENT_BYTES:
            raise ValidationFailed("Local analytics segment exceeds size cap")
        segment_digest = digest_of_bytes(payload)
        manifest = {
            "schema": "zekam-local-analytics-segment-manifest/v1",
            "segment_id": segment_id,
            "segment_digest": segment_digest,
            "event_count": len(events),
            "event_ids": event_ids,
            "first_occurred_at": bodies[0]["occurred_at"],
            "last_occurred_at": bodies[-1]["occurred_at"],
            "source_digests": sorted({str(item["source_digest"]) for item in bodies}),
            "immutable": True,
        }
        raw_manifest = canonical_json(manifest).encode()
        segment = self.raw / f"{segment_id}.jsonl"
        manifest_path = self.manifests / f"{segment_id}.json"
        with self._writer():
            if segment.exists() or manifest_path.exists():
                if (
                    segment.is_file()
                    and manifest_path.is_file()
                    and segment.read_bytes() == payload
                    and manifest_path.read_bytes() == raw_manifest
                ):
                    return digest(manifest)
                raise ConcurrencyConflict("Local analytics segment replay drift")
            _atomic_bytes(segment, payload, mode=0o400)
            try:
                _atomic_bytes(manifest_path, raw_manifest, mode=0o400)
            except BaseException:
                segment.unlink(missing_ok=True)
                _sync_directory(self.raw)
                raise
        return digest(manifest)

    def _segments(self) -> tuple[tuple[dict[str, Any], tuple[dict[str, Any], ...]], ...]:
        result: list[tuple[dict[str, Any], tuple[dict[str, Any], ...]]] = []
        all_ids: set[str] = set()
        manifest_paths = sorted(self.manifests.iterdir())
        raw_paths = sorted(self.raw.iterdir())
        if (
            len(manifest_paths) > MAX_EVENTS
            or len(raw_paths) > MAX_EVENTS
            or any(path.suffix != ".json" for path in manifest_paths)
            or any(path.suffix != ".jsonl" for path in raw_paths)
        ):
            raise PolicyViolation("Local analytics source directory drift")
        if {path.stem for path in manifest_paths} != {path.stem for path in raw_paths}:
            raise PolicyViolation("Local analytics source census drift")
        for manifest_path in manifest_paths:
            if manifest_path.is_symlink():
                raise PolicyViolation("Local analytics manifest identity invalid")
            _private_regular(manifest_path, 0o400)
            manifest = _strict_document(manifest_path.read_bytes(), maximum=65_536)
            if (
                frozenset(manifest) != _MANIFEST_KEYS
                or manifest.get("schema") != "zekam-local-analytics-segment-manifest/v1"
                or manifest.get("immutable") is not True
                or type(manifest.get("event_count")) is not int
                or not 1 <= manifest["event_count"] <= MAX_EVENTS
                or type(manifest.get("event_ids")) is not list
                or type(manifest.get("source_digests")) is not list
            ):
                raise PolicyViolation("Local analytics manifest schema drift")
            segment_id = _stored_file_token(manifest.get("segment_id"), "segment id")
            if manifest_path.name != f"{segment_id}.json":
                raise PolicyViolation("Local analytics manifest path drift")
            segment = self.raw / f"{segment_id}.jsonl"
            _private_regular(segment, 0o400)
            payload = segment.read_bytes()
            if len(payload) > MAX_SEGMENT_BYTES or digest_of_bytes(payload) != manifest.get(
                "segment_digest"
            ):
                raise PolicyViolation("Local analytics segment digest drift")
            events: list[dict[str, Any]] = []
            for raw in payload.splitlines():
                event = _strict_document(raw, maximum=65_536)
                if (
                    frozenset(event) != _EVENT_KEYS
                    or event.get("schema") != "zekam-local-analytics-event/v1"
                ):
                    raise PolicyViolation("Local analytics event schema drift")
                event_id = _stored_token(event.get("event_id"), "event id")
                if event_id in all_ids:
                    raise PolicyViolation("Local analytics cross-segment event duplicate")
                all_ids.add(event_id)
                if len(all_ids) > MAX_EVENTS:
                    raise PolicyViolation("Local analytics total event cap exceeded")
                if (
                    type(event.get("event_type")) is not str
                    or event.get("event_type") not in _EVENT_TYPES
                    or event.get("contains_raw_prompt") is not False
                    or event.get("contains_secret") is not False
                    or event.get("grants_authority") is not False
                    or type(event.get("dimensions")) is not dict
                    or type(event.get("metrics")) is not dict
                    or not 1 <= len(event["dimensions"]) <= 32
                    or not 1 <= len(event["metrics"]) <= 32
                    or any(
                        type(value) is not float or not math.isfinite(value)
                        for value in event["metrics"].values()
                    )
                ):
                    raise PolicyViolation("Local analytics event semantic drift")
                for key, value in event["dimensions"].items():
                    _stored_token(key, "dimension key")
                    _stored_token(value, "dimension value")
                for key in event["metrics"]:
                    _stored_token(key, "metric key")
                try:
                    RawAnalyticsEvent(
                        event_id,
                        event["event_type"],
                        _parse_time(event["occurred_at"]),
                        event["project_ref"],
                        event["component"],
                        event["component_version"],
                        event["adapter_version"],
                        tuple(sorted(event["dimensions"].items())),
                        tuple(sorted(event["metrics"].items())),
                        event["source_digest"],
                        work_ref=event["work_ref"],
                        run_ref=event["run_ref"],
                        session_ref=event["session_ref"],
                    )
                except ValidationFailed as exc:
                    raise PolicyViolation("Local analytics stored event contract drift") from exc
                for value, label in (
                    (event.get("project_ref"), "stored project ref"),
                    (event.get("component"), "stored component"),
                    (event.get("component_version"), "stored component version"),
                    (event.get("adapter_version"), "stored adapter version"),
                ):
                    _stored_token(value, label)
                for value, label in (
                    (event.get("work_ref"), "stored work ref"),
                    (event.get("run_ref"), "stored run ref"),
                    (event.get("session_ref"), "stored session ref"),
                ):
                    _stored_optional_token(value, label)
                events.append(event)
            if len(events) != manifest.get("event_count") or [
                item["event_id"] for item in events
            ] != manifest.get("event_ids"):
                raise PolicyViolation("Local analytics event census drift")
            if (
                not events
                or manifest.get("first_occurred_at") != events[0]["occurred_at"]
                or manifest.get("last_occurred_at") != events[-1]["occurred_at"]
                or manifest.get("source_digests")
                != sorted({item["source_digest"] for item in events})
                or any(type(item) is not str for item in manifest["event_ids"])
                or any(type(item) is not str for item in manifest["source_digests"])
            ):
                raise PolicyViolation("Local analytics manifest relation drift")
            result.append((manifest, tuple(events)))
        if not result:
            raise PolicyViolation("Local analytics has no source manifests")
        return tuple(result)

    def rebuild(
        self, *, now: dt.datetime, fail_before_publish: bool = False
    ) -> AnalyticsRebuildResult:
        _instant(now)
        if type(fail_before_publish) is not bool:
            raise ValidationFailed("Local analytics failure injection must be bool")
        with self._writer():
            try:
                segments = self._segments()
            except PolicyViolation:
                self._quarantine_sources()
                raise
            manifests = [item[0] for item in segments]
            events = [event for _, rows in segments for event in rows]
            source_manifest_digest = digest(manifests)
            generation_digest = digest(
                {
                    "schema": "zekam-local-analytics-generation/v1",
                    "source_manifest_digest": source_manifest_digest,
                    "event_count": len(events),
                }
            )
            newest = max(_parse_time(item["occurred_at"]) for item in events)
            temporary = self.generations / f".{generation_digest[7:]}.{os.getpid()}.duckdb"
            target = self.generations / f"{generation_digest[7:]}.duckdb"
            temporary.unlink(missing_ok=True)
            duckdb = importlib.import_module("duckdb")
            connection = duckdb.connect(str(temporary))
            try:
                connection.execute(
                    "create table raw_event("
                    "event_id varchar primary key,event_type varchar,occurred_at varchar,"
                    "project_ref varchar,work_ref varchar,run_ref varchar,session_ref varchar,"
                    "component varchar,component_version varchar,adapter_version varchar,"
                    "dimensions_json varchar,metrics_json varchar,source_digest varchar)"
                )
                connection.executemany(
                    "insert into raw_event values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            item["event_id"],
                            item["event_type"],
                            item["occurred_at"],
                            item["project_ref"],
                            item["work_ref"],
                            item["run_ref"],
                            item["session_ref"],
                            item["component"],
                            item["component_version"],
                            item["adapter_version"],
                            canonical_json(item["dimensions"]),
                            canonical_json(item["metrics"]),
                            item["source_digest"],
                        )
                        for item in events
                    ],
                )
                connection.execute(
                    "create table projection_metadata(source_manifest_digest varchar primary key,"
                    "refreshed_at varchar,grants_authority boolean check(not grants_authority))"
                )
                connection.execute(
                    "insert into projection_metadata values(?,?,false)",
                    [source_manifest_digest, newest.isoformat()],
                )
                connection.execute(
                    "create view model_dashboard as select event_type,event_id,occurred_at,"
                    "project_ref,json_extract_string(dimensions_json,'$.model_ref') model_ref,"
                    "json_extract_string(dimensions_json,'$.suite_ref') suite_ref,"
                    "json_extract_string(dimensions_json,'$.trial_ref') trial_ref,"
                    "json_extract_string(dimensions_json,'$.decision_ref') decision_ref,"
                    "json_extract_string(dimensions_json,'$.selected_model_ref') "
                    "selected_model_ref,"
                    "cast(json_extract(metrics_json,'$.availability') as double) availability,"
                    "cast(json_extract(metrics_json,'$.trial_count') as double) trial_count,"
                    "cast(json_extract(metrics_json,'$.correctness') as double) correctness,"
                    "cast(json_extract(metrics_json,'$.citation') as double) citation,"
                    "cast(json_extract(metrics_json,'$.structured_format') as double) "
                    "structured_format,"
                    "cast(json_extract(metrics_json,'$.safety') as double) safety,"
                    "cast(json_extract(metrics_json,'$.reliability') as double) reliability,"
                    "cast(json_extract(metrics_json,'$.latency_ms') as double) latency_ms,"
                    "cast(json_extract(metrics_json,'$.latency_p50_ms') as double) "
                    "latency_p50_ms,"
                    "cast(json_extract(metrics_json,'$.latency_p95_ms') as double) "
                    "latency_p95_ms,"
                    "cast(json_extract(metrics_json,'$.confidence') as double) confidence,"
                    "cast(json_extract(metrics_json,'$.candidate_count') as double) "
                    "candidate_count,"
                    "cast(json_extract(metrics_json,'$.success') as double) success,source_digest,"
                    "(select source_manifest_digest from projection_metadata) "
                    "source_manifest_digest,"
                    "(select refreshed_at from projection_metadata) refreshed_at,"
                    "(select grants_authority from projection_metadata) grants_authority "
                    "from raw_event where event_type in "
                    "('model.availability','benchmark.trial','benchmark.aggregate',"
                    "'routing.decision','routing.outcome')"
                )
                connection.execute(
                    "create view rag_dashboard as select event_type,event_id,occurred_at,"
                    "project_ref,"
                    "cast(json_extract(metrics_json,'$.recall_at_10') as double) recall_at_10,"
                    "cast(json_extract(metrics_json,'$.mrr') as double) mrr,"
                    "cast(json_extract(metrics_json,'$.ndcg_at_10') as double) ndcg_at_10,"
                    "cast(json_extract(metrics_json,'$.citation_precision') as double) "
                    "citation_precision,"
                    "cast(json_extract(metrics_json,'$.no_answer_precision') as double) "
                    "no_answer_precision,"
                    "cast(json_extract(metrics_json,'$.no_answer_recall') as double) "
                    "no_answer_recall,"
                    "cast(json_extract(metrics_json,'$.latency_ms') as double) latency_ms,"
                    "cast(json_extract(metrics_json,'$.error_count') as double) error_count,"
                    "cast(json_extract(metrics_json,'$.dimension') as double) dimension,"
                    "source_digest,"
                    "(select source_manifest_digest from projection_metadata) "
                    "source_manifest_digest,"
                    "(select refreshed_at from projection_metadata) refreshed_at,"
                    "(select grants_authority from projection_metadata) grants_authority "
                    "from raw_event where event_type in "
                    "('rag.quality','embedding.health','context.freshness')"
                )
                connection.execute(
                    "create view runtime_dashboard as select event_type,event_id,occurred_at,"
                    "project_ref,run_ref,"
                    "json_extract_string(dimensions_json,'$.state') state,"
                    "json_extract_string(dimensions_json,'$.resource') resource,"
                    "cast(json_extract(metrics_json,'$.latency_ms') as double) latency_ms,"
                    "cast(json_extract(metrics_json,'$.failure_count') as double) "
                    "failure_count,"
                    "cast(json_extract(metrics_json,'$.recovery_count') as double) "
                    "recovery_count,"
                    "cast(json_extract(metrics_json,'$.success_count') as double) "
                    "success_count,"
                    "cast(json_extract(metrics_json,'$.cpu_percent') as double) cpu_percent,"
                    "cast(json_extract(metrics_json,'$.memory_bytes') as double) memory_bytes,"
                    "cast(json_extract(metrics_json,'$.duration_seconds') as double) "
                    "duration_seconds,source_digest,"
                    "(select source_manifest_digest from projection_metadata) "
                    "source_manifest_digest,"
                    "(select refreshed_at from projection_metadata) refreshed_at,"
                    "(select grants_authority from projection_metadata) grants_authority "
                    "from raw_event where event_type in "
                    "('runtime.outcome','memory.effectiveness','resource.soak')"
                )
                connection.execute(
                    "create view context_dashboard as select event_type,event_id,occurred_at,"
                    "project_ref,session_ref,"
                    "json_extract_string(dimensions_json,'$.scope') scope_name,"
                    "cast(json_extract(metrics_json,'$.budget_used') as double) budget_used,"
                    "cast(json_extract(metrics_json,'$.budget_limit') as double) budget_limit,"
                    "cast(json_extract(metrics_json,'$.freshness_seconds') as double) "
                    "freshness_seconds,source_digest,"
                    "(select source_manifest_digest from projection_metadata) "
                    "source_manifest_digest,"
                    "(select refreshed_at from projection_metadata) refreshed_at,"
                    "(select grants_authority from projection_metadata) grants_authority "
                    "from raw_event where event_type='context.freshness'"
                )
                connection.execute(
                    "create view memory_dashboard as select event_type,event_id,occurred_at,"
                    "project_ref,work_ref,"
                    "json_extract_string(dimensions_json,'$.scope') scope_name,"
                    "cast(json_extract(metrics_json,'$.hit_rate') as double) hit_rate,"
                    "cast(json_extract(metrics_json,'$.useful_rate') as double) useful_rate,"
                    "cast(json_extract(metrics_json,'$.latency_ms') as double) latency_ms,"
                    "source_digest,"
                    "(select source_manifest_digest from projection_metadata) "
                    "source_manifest_digest,"
                    "(select refreshed_at from projection_metadata) refreshed_at,"
                    "(select grants_authority from projection_metadata) grants_authority "
                    "from raw_event where event_type='memory.effectiveness'"
                )
                connection.checkpoint()
                aggregate = connection.execute(
                    "select event_type,count(*),min(occurred_at),max(occurred_at),"
                    "count(distinct source_digest) from raw_event "
                    "group by event_type order by event_type"
                ).fetchall()
                raw_rows = connection.execute(
                    "select * from raw_event order by event_id"
                ).fetchall()
                dashboards = {
                    name: [
                        list(row)
                        for row in connection.execute(
                            f"select * from {name} order by {_DASHBOARD_ORDER[name]}"
                        ).fetchall()
                    ]
                    for name in _DASHBOARDS
                }
                metric_summaries = _metric_summaries(connection)
            finally:
                connection.close()
            fresh = newest <= now.astimezone(dt.UTC) < newest + dt.timedelta(hours=24)
            aggregate_digest = digest([list(row) for row in aggregate])
            raw_rows_digest = digest([list(row) for row in raw_rows])
            dashboard_digests = {key: digest(value) for key, value in dashboards.items()}
            morning = {
                "schema": "zekam-local-analytics-morning-report/v1",
                "source_manifest_digest": source_manifest_digest,
                "raw_rows_digest": raw_rows_digest,
                "aggregate_digest": aggregate_digest,
                "dashboard_digests": dashboard_digests,
                "metric_summaries": metric_summaries,
                "refreshed_at": newest.isoformat(),
                "fresh": fresh,
                "authority": False,
            }
            all_projects = sorted({str(item["project_ref"]) for item in events})
            projects = all_projects[:MAX_REPORT_PROJECTS]
            project = {
                "schema": "zekam-local-analytics-project-report/v1",
                "source_manifest_digest": source_manifest_digest,
                "raw_rows_digest": raw_rows_digest,
                "aggregate_digest": aggregate_digest,
                "dashboard_digests": dashboard_digests,
                "metric_summaries": metric_summaries,
                "project_refs": projects,
                "project_count": len(all_projects),
                "project_refs_truncated": len(projects) != len(all_projects),
                "fresh": fresh,
                "authority": False,
            }
            morning_report_file = f"{generation_digest[7:]}-morning.json"
            project_report_file = f"{generation_digest[7:]}-project.json"
            pointer = {
                "schema": "zekam-local-analytics-current/v1",
                "generation_digest": generation_digest,
                "database_file": target.name,
                "source_manifest_digest": source_manifest_digest,
                "raw_rows_digest": raw_rows_digest,
                "aggregate_digest": aggregate_digest,
                "dashboard_digests": dashboard_digests,
                "morning_report_digest": digest(morning),
                "project_report_digest": digest(project),
                "morning_report_file": morning_report_file,
                "project_report_file": project_report_file,
                "refreshed_at": newest.isoformat(),
                "fresh": fresh,
                "grants_authority": False,
            }
            if fail_before_publish:
                temporary.unlink(missing_ok=True)
                raise RuntimeError("injected analytics pre-publish failure")
            _replace_file(temporary, target, mode=0o400)
            _sync_directory(self.generations)
            self._immutable_artifact(
                self.reports / morning_report_file, canonical_json(morning).encode()
            )
            self._immutable_artifact(
                self.reports / project_report_file, canonical_json(project).encode()
            )
            receipt = {
                "schema": "zekam-local-analytics-rebuild-receipt/v1",
                "generation_digest": generation_digest,
                "source_manifest_digest": source_manifest_digest,
                "raw_rows_digest": raw_rows_digest,
                "aggregate_digest": aggregate_digest,
                "event_count": len(events),
                "segment_count": len(segments),
                "pointer_digest": digest(pointer),
                "status": "completed",
                "grants_authority": False,
            }
            receipt_digest = digest(receipt)
            self._immutable_artifact(
                self.receipts / f"{generation_digest[7:]}.json", canonical_json(receipt).encode()
            )
            pointer["rebuild_receipt_digest"] = receipt_digest
            _atomic_bytes(self.current, canonical_json(pointer).encode(), mode=0o400)
            return AnalyticsRebuildResult(
                generation_digest,
                source_manifest_digest,
                raw_rows_digest,
                aggregate_digest,
                dashboard_digests["model_dashboard"],
                dashboard_digests["rag_dashboard"],
                dashboard_digests["runtime_dashboard"],
                dashboard_digests["context_dashboard"],
                dashboard_digests["memory_dashboard"],
                digest(morning),
                digest(project),
                receipt_digest,
                fresh,
            )

    def current_projection(self) -> dict[str, Any]:
        _private_regular(self.current, 0o400)
        pointer = _strict_document(self.current.read_bytes(), maximum=65_536)
        if (
            frozenset(pointer) != _POINTER_KEYS
            or pointer.get("schema") != "zekam-local-analytics-current/v1"
            or type(pointer.get("database_file")) is not str
            or type(pointer.get("morning_report_file")) is not str
            or type(pointer.get("project_report_file")) is not str
            or type(pointer.get("fresh")) is not bool
            or type(pointer.get("dashboard_digests")) is not dict
            or frozenset(pointer["dashboard_digests"]) != frozenset(_DASHBOARDS)
        ):
            raise PolicyViolation("Local analytics current pointer schema drift")
        _stored_digest(pointer.get("generation_digest"))
        _stored_digest(pointer.get("source_manifest_digest"))
        _stored_digest(pointer.get("raw_rows_digest"))
        _stored_digest(pointer.get("aggregate_digest"))
        receipt_digest = _stored_digest(pointer.get("rebuild_receipt_digest"))
        _stored_digest(pointer.get("morning_report_digest"))
        _stored_digest(pointer.get("project_report_digest"))
        for value in pointer["dashboard_digests"].values():
            _stored_digest(value)
        _parse_time(pointer.get("refreshed_at"))
        if pointer.get("grants_authority") is not False:
            raise PolicyViolation("Local analytics projection cannot grant authority")
        manifests = [item[0] for item in self._segments()]
        if digest(manifests) != pointer["source_manifest_digest"]:
            raise PolicyViolation("Local analytics source manifest is stale")
        if pointer["database_file"] != f"{pointer['generation_digest'][7:]}.duckdb":
            raise PolicyViolation("Local analytics generation path drift")
        database = self.generations / pointer["database_file"]
        _private_regular(database, 0o400)
        receipt_path = self.receipts / f"{pointer['generation_digest'][7:]}.json"
        _private_regular(receipt_path, 0o400)
        receipt = _strict_document(receipt_path.read_bytes(), maximum=65_536)
        if digest(receipt) != receipt_digest or receipt.get("grants_authority") is not False:
            raise PolicyViolation("Local analytics rebuild receipt drift")
        pointer_without_receipt = dict(pointer)
        del pointer_without_receipt["rebuild_receipt_digest"]
        if (
            frozenset(receipt)
            != {
                "schema",
                "generation_digest",
                "source_manifest_digest",
                "raw_rows_digest",
                "aggregate_digest",
                "event_count",
                "segment_count",
                "pointer_digest",
                "status",
                "grants_authority",
            }
            or receipt.get("schema") != "zekam-local-analytics-rebuild-receipt/v1"
            or receipt.get("status") != "completed"
            or receipt.get("generation_digest") != pointer["generation_digest"]
            or receipt.get("source_manifest_digest") != pointer["source_manifest_digest"]
            or receipt.get("raw_rows_digest") != pointer["raw_rows_digest"]
            or receipt.get("aggregate_digest") != pointer["aggregate_digest"]
            or receipt.get("pointer_digest") != digest(pointer_without_receipt)
        ):
            raise PolicyViolation("Local analytics rebuild receipt relation drift")
        report_documents: dict[str, dict[str, Any]] = {}
        for label in ("morning", "project"):
            expected_name = f"{pointer['generation_digest'][7:]}-{label}.json"
            if pointer[f"{label}_report_file"] != expected_name:
                raise PolicyViolation("Local analytics report path drift")
            report_path = self.reports / expected_name
            _private_regular(report_path, 0o400)
            report = _strict_document(report_path.read_bytes(), maximum=65_536)
            expected_keys = _MORNING_REPORT_KEYS if label == "morning" else _PROJECT_REPORT_KEYS
            if (
                frozenset(report) != expected_keys
                or report.get("schema") != f"zekam-local-analytics-{label}-report/v1"
                or digest(report) != pointer.get(f"{label}_report_digest")
                or report.get("source_manifest_digest") != pointer["source_manifest_digest"]
                or report.get("raw_rows_digest") != pointer["raw_rows_digest"]
                or report.get("aggregate_digest") != pointer["aggregate_digest"]
                or report.get("dashboard_digests") != pointer["dashboard_digests"]
                or report.get("fresh") != pointer["fresh"]
                or report.get("authority") is not False
            ):
                raise PolicyViolation("Local analytics report relation drift")
            report_documents[label] = report
        duckdb = importlib.import_module("duckdb")
        connection = duckdb.connect(str(database), read_only=True)
        try:
            aggregate = connection.execute(
                "select event_type,count(*),min(occurred_at),max(occurred_at),"
                "count(distinct source_digest) from raw_event "
                "group by event_type order by event_type"
            ).fetchall()
            raw_rows = connection.execute("select * from raw_event order by event_id").fetchall()
            dashboards = {
                name: [
                    list(row)
                    for row in connection.execute(
                        f"select * from {name} order by {_DASHBOARD_ORDER[name]}"
                    ).fetchall()
                ]
                for name in _DASHBOARDS
            }
            metric_summaries = _metric_summaries(connection)
        finally:
            connection.close()
        all_projects = sorted({str(row[3]) for row in raw_rows})
        expected_projects = all_projects[:MAX_REPORT_PROJECTS]
        if (
            report_documents["morning"].get("metric_summaries") != metric_summaries
            or report_documents["morning"].get("refreshed_at") != pointer["refreshed_at"]
            or report_documents["project"].get("metric_summaries") != metric_summaries
            or report_documents["project"].get("project_refs") != expected_projects
            or report_documents["project"].get("project_count") != len(all_projects)
            or report_documents["project"].get("project_refs_truncated")
            is not (len(expected_projects) != len(all_projects))
        ):
            raise PolicyViolation("Local analytics report reconciliation drift")
        if (
            digest([list(row) for row in raw_rows]) != pointer["raw_rows_digest"]
            or digest([list(row) for row in aggregate]) != pointer["aggregate_digest"]
            or {key: digest(value) for key, value in dashboards.items()}
            != pointer.get("dashboard_digests")
        ):
            raise PolicyViolation("Local analytics projection reconciliation drift")
        return pointer
