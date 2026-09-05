from __future__ import annotations

import datetime as dt
import importlib
import multiprocessing
import sqlite3
from pathlib import Path

import pytest

from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import ConcurrencyConflict, PolicyViolation, ValidationFailed
from zekam.infrastructure import local_analytics as analytics_module
from zekam.infrastructure.local_analytics import LocalAnalyticsStore, RawAnalyticsEvent

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)


def _event(index: int, event_type: str) -> RawAnalyticsEvent:
    dimensions: dict[str, tuple[tuple[str, str], ...]] = {
        "model.availability": (("model_ref", "model-a"),),
        "benchmark.trial": (
            ("model_ref", "model-a"),
            ("suite_ref", "suite-a"),
            ("trial_ref", f"trial-{index}"),
        ),
        "benchmark.aggregate": (("model_ref", "model-a"), ("suite_ref", "suite-a")),
        "routing.decision": (
            ("decision_ref", f"decision-{index}"),
            ("selected_model_ref", "model-a"),
        ),
        "routing.outcome": (("decision_ref", f"decision-{index}"),),
        "rag.quality": (("profile", "rag-a"),),
        "embedding.health": (("profile", "embedding-a"),),
        "runtime.outcome": (("state", "completed"),),
        "memory.effectiveness": (("scope", "project"),),
        "context.freshness": (("scope", "session"),),
        "resource.soak": (("resource", "process"),),
    }
    metrics: dict[str, tuple[tuple[str, float], ...]] = {
        "model.availability": (("availability", 1.0),),
        "benchmark.trial": (
            ("correctness", 0.8),
            ("latency_ms", 120.0),
            ("reliability", 0.9),
        ),
        "benchmark.aggregate": (
            ("correctness", 0.8),
            ("latency_p95_ms", 150.0),
            ("reliability", 0.9),
            ("trial_count", 10.0),
        ),
        "routing.decision": (("candidate_count", 3.0), ("confidence", 0.85)),
        "routing.outcome": (("latency_ms", 100.0), ("success", 1.0)),
        "rag.quality": (
            ("citation_precision", 1.0),
            ("latency_ms", 40.0),
            ("mrr", 0.8),
            ("ndcg_at_10", 0.82),
            ("no_answer_precision", 1.0),
            ("no_answer_recall", 0.9),
            ("recall_at_10", 0.86),
        ),
        "embedding.health": (
            ("dimension", 1024.0),
            ("error_count", 0.0),
            ("latency_ms", 20.0),
        ),
        "runtime.outcome": (
            ("failure_count", 0.0),
            ("latency_ms", 50.0),
            ("recovery_count", 0.0),
            ("success_count", 1.0),
        ),
        "memory.effectiveness": (("hit_rate", 0.75), ("latency_ms", 5.0)),
        "context.freshness": (("budget_used", 100.0), ("freshness_seconds", 30.0)),
        "resource.soak": (
            ("cpu_percent", 35.0),
            ("duration_seconds", 600.0),
            ("failure_count", 0.0),
            ("memory_bytes", 1048576.0),
        ),
    }
    return RawAnalyticsEvent(
        f"event-{index}",
        event_type,
        NOW + dt.timedelta(seconds=index),
        "project-a",
        "wp13",
        "1",
        "local-v1",
        tuple(sorted(dimensions[event_type])),
        tuple(sorted(metrics[event_type])),
        digest({"source": event_type, "index": index}),
        work_ref="work-a",
        run_ref="run-a",
        session_ref="session-a",
    )


def _store(tmp_path: Path) -> LocalAnalyticsStore:
    root = (tmp_path / "analytics").resolve()
    store = LocalAnalyticsStore(root)
    store.bootstrap()
    return store


def _events() -> tuple[RawAnalyticsEvent, ...]:
    return tuple(
        _event(index, event_type)
        for index, event_type in enumerate(
            (
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
            )
        )
    )


def test_rebuild_delete_restart_has_exact_aggregate_and_authority_free_reports(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    manifest = store.append_segment("segment-a", _events())
    assert store.append_segment("segment-a", _events()) == manifest
    first = store.rebuild(now=NOW + dt.timedelta(minutes=1))
    current = store.current_projection()
    assert current["aggregate_digest"] == first.aggregate_digest
    assert current["raw_rows_digest"] == first.raw_rows_digest
    assert current["grants_authority"] is False
    assert frozenset(current["dashboard_digests"]) == {
        "model_dashboard",
        "rag_dashboard",
        "runtime_dashboard",
        "context_dashboard",
        "memory_dashboard",
    }
    assert first.fresh
    database = store.generations / current["database_file"]
    with sqlite3.connect(":memory:"):
        database.unlink()
    rebuilt = LocalAnalyticsStore(store.root).rebuild(now=NOW + dt.timedelta(minutes=1))
    assert rebuilt == first
    assert LocalAnalyticsStore(store.root).current_projection() == current


def test_schema_bounds_duplicates_and_replay_drift_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _event(1, "routing.outcome")
    with pytest.raises(ValidationFailed):
        RawAnalyticsEvent(
            event.event_id,
            event.event_type,
            event.occurred_at,
            event.project_ref,
            event.component,
            event.component_version,
            event.adapter_version,
            event.dimensions,
            (("score", True),),
            event.source_digest,
        )
    with pytest.raises(ValidationFailed, match="dimensions"):
        RawAnalyticsEvent(
            event.event_id,
            event.event_type,
            event.occurred_at,
            event.project_ref,
            event.component,
            event.component_version,
            event.adapter_version,
            (),
            event.metrics,
            event.source_digest,
        )
    with pytest.raises(ValidationFailed, match="finite float"):
        RawAnalyticsEvent(
            event.event_id,
            event.event_type,
            event.occurred_at,
            event.project_ref,
            event.component,
            event.component_version,
            event.adapter_version,
            event.dimensions,
            (("score", float("nan")),),
            event.source_digest,
        )
    with pytest.raises(ValidationFailed, match="sensitive"):
        RawAnalyticsEvent(
            event.event_id,
            event.event_type,
            event.occurred_at,
            event.project_ref,
            event.component,
            event.component_version,
            event.adapter_version,
            (("api_key", "sk-secretmaterial"),),
            event.metrics,
            event.source_digest,
        )
    with pytest.raises(ValidationFailed, match="duplicate"):
        store.append_segment("duplicates", (event, event))
    store.append_segment("stable", (event,))
    with pytest.raises(ConcurrencyConflict, match="drift"):
        store.append_segment("stable", (_event(2, "routing.outcome"),))


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema":"wrong"}',
        b'{"schema":"x","schema":"y"}',
        b'{"schema":"x","value":NaN}',
        b"\xff",
        b"",
    ],
)
def test_corrupt_noncanonical_truncated_and_wrong_schema_reject(
    tmp_path: Path, payload: bytes
) -> None:
    store = _store(tmp_path)
    store.append_segment("segment", (_event(1, "runtime.outcome"),))
    manifest = store.manifests / "segment.json"
    manifest.chmod(0o600)
    manifest.write_bytes(payload)
    manifest.chmod(0o400)
    with pytest.raises(PolicyViolation):
        store.rebuild(now=NOW)
    assert not store.current.exists()
    assert not tuple(store.raw.iterdir())
    assert not tuple(store.manifests.iterdir())
    assert len(tuple(store.quarantine.iterdir())) == 2


def test_cross_segment_duplicate_and_orphan_source_are_quarantined(tmp_path: Path) -> None:
    store = _store(tmp_path)
    event = _event(1, "runtime.outcome")
    store.append_segment("one", (event,))
    store.append_segment("two", (event,))
    with pytest.raises(PolicyViolation, match="duplicate"):
        store.rebuild(now=NOW)
    assert len(tuple(store.quarantine.iterdir())) == 4

    other = _store(tmp_path / "other")
    other.append_segment("orphan", (event,))
    (other.manifests / "orphan.json").unlink()
    with pytest.raises(PolicyViolation, match="census"):
        other.rebuild(now=NOW)
    assert len(tuple(other.quarantine.iterdir())) == 1


def test_projection_reconciles_database_reports_receipt_and_pointer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_segment("segment", _events())
    store.rebuild(now=NOW)
    pointer = store.current_projection()
    database = store.generations / pointer["database_file"]
    database.chmod(0o600)
    duckdb = importlib.import_module("duckdb")
    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            "update raw_event set source_digest=? where event_id='event-0'",
            [digest({"changed": True})],
        )
    finally:
        connection.close()
    database.chmod(0o400)
    with pytest.raises(PolicyViolation, match="reconciliation"):
        store.current_projection()

    store = _store(tmp_path / "report")
    store.append_segment("segment", _events())
    store.rebuild(now=NOW)
    pointer = store.current_projection()
    report = store.reports / pointer["morning_report_file"]
    report.chmod(0o600)
    report.write_bytes(canonical_json({"authority": False}).encode())
    report.chmod(0o400)
    with pytest.raises(PolicyViolation, match="report"):
        store.current_projection()


def test_forged_metric_report_is_rejected_even_when_hash_chain_is_rebound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_segment("segment", _events())
    store.rebuild(now=NOW)
    pointer = analytics_module._strict_document(store.current.read_bytes())
    report_path = store.reports / pointer["morning_report_file"]
    report = analytics_module._strict_document(report_path.read_bytes())
    rag_row = next(
        row for row in report["metric_summaries"]["rag"] if row["event_type"] == "rag.quality"
    )
    rag_row["citation_precision_avg"] = 0.0
    report_path.chmod(0o600)
    report_path.write_bytes(canonical_json(report).encode())
    report_path.chmod(0o400)

    pointer["morning_report_digest"] = digest(report)
    receipt_path = store.receipts / f"{pointer['generation_digest'][7:]}.json"
    receipt = analytics_module._strict_document(receipt_path.read_bytes())
    pointer_without_receipt = dict(pointer)
    del pointer_without_receipt["rebuild_receipt_digest"]
    receipt["pointer_digest"] = digest(pointer_without_receipt)
    receipt_path.chmod(0o600)
    receipt_path.write_bytes(canonical_json(receipt).encode())
    receipt_path.chmod(0o400)
    pointer["rebuild_receipt_digest"] = digest(receipt)
    store.current.chmod(0o600)
    store.current.write_bytes(canonical_json(pointer).encode())
    store.current.chmod(0o400)
    with pytest.raises(PolicyViolation, match="report reconciliation"):
        store.current_projection()


def test_noncanonical_pointer_and_oversized_segment_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_segment("segment", (_event(1, "context.freshness"),))
    store.rebuild(now=NOW)
    store.current.chmod(0o600)
    store.current.write_bytes(b'{"schema": "zekam-local-analytics-current/v1"}')
    store.current.chmod(0o400)
    with pytest.raises(PolicyViolation, match="canonical"):
        store.current_projection()

    other = _store(tmp_path / "oversized")
    other.append_segment("segment", (_event(1, "context.freshness"),))
    segment = other.raw / "segment.jsonl"
    segment.chmod(0o600)
    segment.write_bytes(b"x" * (2_097_152 + 1))
    segment.chmod(0o400)
    with pytest.raises(PolicyViolation, match="digest"):
        other.rebuild(now=NOW)


def test_crash_before_swap_preserves_generation_and_stale_manifest_is_visible(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.append_segment("segment", _events())
    first = store.rebuild(now=NOW)
    before = store.current.read_bytes()
    with pytest.raises(RuntimeError, match="injected"):
        store.rebuild(now=NOW, fail_before_publish=True)
    assert store.current.read_bytes() == before
    stale = store.rebuild(now=NOW + dt.timedelta(days=2))
    assert stale.aggregate_digest == first.aggregate_digest
    assert stale.fresh is False
    assert store.current_projection()["fresh"] is False


def test_new_source_manifest_marks_current_generation_stale(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_segment("first", (_event(1, "routing.outcome"),))
    store.rebuild(now=NOW + dt.timedelta(minutes=1))
    store.append_segment("second", (_event(2, "runtime.outcome"),))
    with pytest.raises(PolicyViolation, match="manifest is stale"):
        store.current_projection()
    store.rebuild(now=NOW + dt.timedelta(minutes=1))
    assert store.current_projection()["grants_authority"] is False


def _hold_writer(
    root: str, ready: multiprocessing.Queue[bool], release: multiprocessing.Queue[bool]
) -> None:
    store = LocalAnalyticsStore(Path(root))
    with store._writer():
        ready.put(True)
        release.get(timeout=5)


def test_multi_process_second_writer_rejected_and_reader_generation_remains_valid(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.append_segment("segment", _events())
    store.rebuild(now=NOW)
    context = multiprocessing.get_context("spawn")
    ready: multiprocessing.Queue[bool] = context.Queue()
    release: multiprocessing.Queue[bool] = context.Queue()
    process = context.Process(target=_hold_writer, args=(str(store.root), ready, release))
    process.start()
    assert ready.get(timeout=5) is True
    try:
        with pytest.raises(ConcurrencyConflict, match="writer"):
            store.rebuild(now=NOW)
        assert store.current_projection()["grants_authority"] is False
    finally:
        release.put(True)
        process.join(timeout=5)
        assert process.exitcode == 0


def test_path_and_file_identity_drift_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValidationFailed, match="segment id"):
        store.append_segment("nested/../../escape", (_event(1, "benchmark.aggregate"),))
    store.append_segment("segment", (_event(1, "benchmark.aggregate"),))
    segment = store.raw / "segment.jsonl"
    segment.chmod(0o600)
    with pytest.raises(PolicyViolation, match="identity"):
        store.rebuild(now=NOW)
    with pytest.raises(ValidationFailed):
        LocalAnalyticsStore(Path("relative"))


def test_event_dimension_and_metric_exact_count_boundaries() -> None:
    dimensions = tuple((f"key-{index:02d}", "value") for index in range(32))
    metrics = tuple((key, 1.0) for key in sorted(analytics_module._KNOWN_METRICS)[:32])
    event = RawAnalyticsEvent(
        "boundary-event",
        "runtime.outcome",
        NOW,
        "project-a",
        "wp13",
        "1",
        "local-v1",
        dimensions,
        metrics,
        digest("boundary"),
    )
    assert len(event.dimensions) == len(event.metrics) == 32
    with pytest.raises(ValidationFailed, match="dimensions"):
        RawAnalyticsEvent(
            "too-many-dimensions",
            "runtime.outcome",
            NOW,
            "project-a",
            "wp13",
            "1",
            "local-v1",
            (*dimensions, ("overflow", "value")),
            metrics,
            digest("boundary"),
        )
    with pytest.raises(ValidationFailed, match="metrics"):
        RawAnalyticsEvent(
            "too-many-metrics",
            "runtime.outcome",
            NOW,
            "project-a",
            "wp13",
            "1",
            "local-v1",
            dimensions,
            (*metrics, ("overflow", 1.0)),
            digest("boundary"),
        )


def test_wp13_typed_histories_trends_and_bounded_reports(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_segment("wp13", _events())
    first = store.rebuild(now=NOW + dt.timedelta(minutes=1))
    pointer = store.current_projection()
    database = store.generations / pointer["database_file"]
    duckdb = importlib.import_module("duckdb")
    connection = duckdb.connect(str(database), read_only=True)
    try:
        model_types = connection.execute(
            "select event_type from model_dashboard order by event_type"
        ).fetchall()
        assert model_types == [
            ("benchmark.aggregate",),
            ("benchmark.trial",),
            ("model.availability",),
            ("routing.decision",),
            ("routing.outcome",),
        ]
        assert connection.execute(
            "select availability from model_dashboard where event_type='model.availability'"
        ).fetchone() == (1.0,)
        assert connection.execute(
            "select model_ref,suite_ref,trial_ref,correctness,reliability,latency_ms "
            "from model_dashboard where event_type='benchmark.trial'"
        ).fetchone() == ("model-a", "suite-a", "trial-1", 0.8, 0.9, 120.0)
        assert connection.execute(
            "select source_manifest_digest,refreshed_at,grants_authority "
            "from model_dashboard where event_type='benchmark.trial'"
        ).fetchone() == (pointer["source_manifest_digest"], pointer["refreshed_at"], False)
        assert connection.execute(
            "select decision_ref,selected_model_ref,confidence,candidate_count "
            "from model_dashboard where event_type='routing.decision'"
        ).fetchone() == ("decision-3", "model-a", 0.85, 3.0)
        assert connection.execute(
            "select recall_at_10,mrr,ndcg_at_10,citation_precision,"
            "no_answer_precision,no_answer_recall,latency_ms from rag_dashboard "
            "where event_type='rag.quality'"
        ).fetchone() == (0.86, 0.8, 0.82, 1.0, 1.0, 0.9, 40.0)
        assert connection.execute(
            "select latency_ms,failure_count,recovery_count,success_count "
            "from runtime_dashboard where event_type='runtime.outcome'"
        ).fetchone() == (50.0, 0.0, 0.0, 1.0)
        assert connection.execute(
            "select cpu_percent,memory_bytes,duration_seconds from runtime_dashboard "
            "where event_type='resource.soak'"
        ).fetchone() == (35.0, 1048576.0, 600.0)
        assert connection.execute(
            "select scope_name,budget_used,freshness_seconds,grants_authority "
            "from context_dashboard"
        ).fetchone() == ("session", 100.0, 30.0, False)
        assert connection.execute(
            "select scope_name,hit_rate,latency_ms,grants_authority from memory_dashboard"
        ).fetchone() == ("project", 0.75, 5.0, False)
        view_types = dict(
            connection.execute(
                "select column_name,data_type from information_schema.columns "
                "where table_name='rag_dashboard'"
            ).fetchall()
        )
        assert view_types["recall_at_10"] == "DOUBLE"
        assert view_types["citation_precision"] == "DOUBLE"
    finally:
        connection.close()

    morning = analytics_module._strict_document(
        (store.reports / pointer["morning_report_file"]).read_bytes()
    )
    project = analytics_module._strict_document(
        (store.reports / pointer["project_report_file"]).read_bytes()
    )
    assert morning["dashboard_digests"] == pointer["dashboard_digests"]
    assert set(morning["metric_summaries"]) == {
        "model",
        "rag",
        "runtime",
        "context",
        "memory",
    }
    rag_summary = next(
        row for row in morning["metric_summaries"]["rag"] if row["event_type"] == "rag.quality"
    )
    assert rag_summary["citation_precision_avg"] == 1.0
    assert rag_summary["no_answer_recall_avg"] == 0.9
    assert morning["metric_summaries"]["context"][0]["freshness_seconds_avg"] == 30.0
    assert morning["metric_summaries"]["memory"][0]["hit_rate_avg"] == 0.75
    assert project["metric_summaries"] == morning["metric_summaries"]
    assert project["project_count"] == 1
    assert project["project_refs_truncated"] is False

    database.chmod(0o600)
    database.unlink()
    rebuilt = LocalAnalyticsStore(store.root).rebuild(now=NOW + dt.timedelta(minutes=1))
    assert rebuilt == first
    assert LocalAnalyticsStore(store.root).current_projection() == pointer


@pytest.mark.parametrize(
    ("dimensions", "metrics", "error"),
    [
        (
            (("model_ref", "model-a"), ("suite_ref", "suite-a")),
            (("correctness", 1.0), ("latency_ms", 1.0), ("reliability", 1.0)),
            "contract incomplete",
        ),
        (
            (("model_ref", "model-a"), ("suite_ref", "suite-a"), ("trial_ref", "one")),
            (("correctness", 1.0), ("latency_ms", 1.0), ("unknown", 1.0)),
            "unknown",
        ),
        (
            (("model_ref", "model-a"), ("suite_ref", "suite-a"), ("trial_ref", "one")),
            (("correctness", 1.1), ("latency_ms", 1.0), ("reliability", 1.0)),
            "unit interval",
        ),
        (
            (("decision_ref", "one"), ("selected_model_ref", "model-a")),
            (("candidate_count", 1.5), ("confidence", 0.5)),
            "count metric",
        ),
        (
            (("decision_ref", "one"), ("selected_model_ref", "model-a")),
            (("candidate_count", 1.0e20), ("confidence", 0.5)),
            "exact bound",
        ),
        (
            (("decision_ref", "one"), ("decision_ref", "two")),
            (("candidate_count", 2.0), ("confidence", 0.5)),
            "dimensions",
        ),
        (
            (("decision_ref", "one"), ("selected_model_ref", "model-a")),
            (("confidence", 0.5), ("confidence", 0.6)),
            "metrics",
        ),
    ],
)
def test_wp13_event_contract_rejects_incomplete_unknown_out_of_range_and_duplicate(
    dimensions: tuple[tuple[str, str], ...],
    metrics: tuple[tuple[str, float], ...],
    error: str,
) -> None:
    event_type = "routing.decision" if dimensions[0][0] == "decision_ref" else "benchmark.trial"
    with pytest.raises(ValidationFailed, match=error):
        RawAnalyticsEvent(
            "adversarial",
            event_type,
            NOW,
            "project-a",
            "wp13",
            "1",
            "local-v1",
            dimensions,
            metrics,
            digest("adversarial"),
        )


def test_exact_segment_byte_cap_and_immutable_replay_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = _event(1, "runtime.outcome")
    exact_payload = canonical_json(event.body()).encode() + b"\n"
    monkeypatch.setattr(analytics_module, "MAX_SEGMENT_BYTES", len(exact_payload))
    store = _store(tmp_path)
    store.append_segment("exact", (event,))
    assert store.rebuild(now=NOW + dt.timedelta(minutes=1)).fresh is True

    artifact = tmp_path / "immutable"
    LocalAnalyticsStore._immutable_artifact(artifact, b"stable")
    LocalAnalyticsStore._immutable_artifact(artifact, b"stable")
    with pytest.raises(ConcurrencyConflict, match="replay drift"):
        LocalAnalyticsStore._immutable_artifact(artifact, b"changed")


def test_exact_source_and_total_event_cap_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analytics_module, "MAX_EVENTS", 2)
    store = _store(tmp_path)
    store.append_segment("one", (_event(1, "runtime.outcome"),))
    store.append_segment("two", (_event(2, "routing.outcome"),))
    assert store.rebuild(now=NOW + dt.timedelta(minutes=1)).fresh is True


@pytest.mark.parametrize(
    "operation,error",
    [
        (lambda: analytics_module._stored_digest(1), "digest type"),
        (lambda: analytics_module._instant("not-a-time"), "timezone-aware"),  # type: ignore[arg-type]
        (lambda: analytics_module._parse_time(1), "timestamp type"),
        (lambda: analytics_module._parse_time("2026-09-04T12:00:00"), "lacks timezone"),
        (
            lambda: RawAnalyticsEvent(
                "event-invalid-type",
                "unknown.event",
                NOW,
                "project-a",
                "wp13",
                "1",
                "local-v1",
                (("source", "test"),),
                (("score", 1.0),),
                digest("invalid-type"),
            ),
            "event type",
        ),
    ],
)
def test_stored_scalar_and_event_type_boundaries_fail_closed(operation: object, error: str) -> None:
    with pytest.raises((PolicyViolation, ValidationFailed), match=error):
        operation()  # type: ignore[operator]
    assert analytics_module._stored_optional_token(None, "optional") is None


def test_private_directory_and_quarantine_identity_boundaries(tmp_path: Path) -> None:
    public = (tmp_path / "public").resolve()
    public.mkdir(mode=0o755)
    with pytest.raises(PolicyViolation, match="private root"):
        LocalAnalyticsStore(public).bootstrap()

    store = _store(tmp_path / "directory")
    store.raw.chmod(0o755)
    with pytest.raises(PolicyViolation, match="private directory"):
        store.bootstrap()

    store = _store(tmp_path / "quarantine")
    (store.raw / "directory").mkdir()
    store._quarantine_sources()
    assert (store.raw / "directory").is_dir()

    store.append_segment("segment", (_event(1, "runtime.outcome"),))
    source = store.raw / "segment.jsonl"
    suffix = digest_of_bytes(source.read_bytes())[7:23]
    target = store.quarantine / f"{source.name}.{suffix}.rejected"
    target.write_bytes(b"collision")
    with pytest.raises(ConcurrencyConflict, match="quarantine collision"):
        store._quarantine_sources()


def test_append_and_rebuild_input_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValidationFailed, match="segment events"):
        store.append_segment("empty", ())
    with pytest.raises(ValidationFailed, match="failure injection"):
        store.rebuild(now=NOW, fail_before_publish=1)  # type: ignore[arg-type]

    event = _event(1, "runtime.outcome")
    monkeypatch.setattr(analytics_module, "MAX_SEGMENT_BYTES", 1)
    with pytest.raises(ValidationFailed, match="size cap"):
        store.append_segment("oversize", (event,))


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        ("manifest-schema", "manifest schema"),
        ("manifest-path", "manifest path"),
        ("event-semantics", "event semantic"),
        ("event-census", "event census"),
        ("manifest-relation", "manifest relation"),
    ],
)
def test_segment_manifest_and_event_relations_reject_canonical_drift(
    tmp_path: Path, mutation: str, error: str
) -> None:
    store = _store(tmp_path)
    store.append_segment("segment", (_event(1, "runtime.outcome"),))
    manifest_path = store.manifests / "segment.json"
    raw_path = store.raw / "segment.jsonl"
    manifest = analytics_module._strict_document(manifest_path.read_bytes())
    event = analytics_module._strict_document(raw_path.read_bytes().rstrip(b"\n"))

    if mutation == "manifest-schema":
        manifest["immutable"] = False
    elif mutation == "manifest-path":
        manifest["segment_id"] = "different"
    elif mutation == "event-semantics":
        event["grants_authority"] = True
        payload = canonical_json(event).encode() + b"\n"
        raw_path.chmod(0o600)
        raw_path.write_bytes(payload)
        raw_path.chmod(0o400)
        manifest["segment_digest"] = digest_of_bytes(payload)
    elif mutation == "event-census":
        manifest["event_count"] = 2
    else:
        manifest["source_digests"] = [digest("unrelated")]
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(canonical_json(manifest).encode())
    manifest_path.chmod(0o400)
    with pytest.raises(PolicyViolation, match=error):
        store.rebuild(now=NOW)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("grants_authority", True, "cannot grant authority"),
        ("database_file", "other.duckdb", "generation path"),
        ("morning_report_file", "other.json", "report path"),
    ],
)
def test_current_pointer_authority_and_path_relations_reject(
    tmp_path: Path, field: str, value: object, error: str
) -> None:
    store = _store(tmp_path)
    store.append_segment("segment", (_event(1, "runtime.outcome"),))
    store.rebuild(now=NOW)
    pointer = analytics_module._strict_document(store.current.read_bytes())
    pointer[field] = value
    if field == "morning_report_file":
        receipt_path = store.receipts / f"{pointer['generation_digest'][7:]}.json"
        receipt = analytics_module._strict_document(receipt_path.read_bytes())
        pointer_without_receipt = dict(pointer)
        del pointer_without_receipt["rebuild_receipt_digest"]
        receipt["pointer_digest"] = digest(pointer_without_receipt)
        receipt_path.chmod(0o600)
        receipt_path.write_bytes(canonical_json(receipt).encode())
        receipt_path.chmod(0o400)
        pointer["rebuild_receipt_digest"] = digest(receipt)
    store.current.chmod(0o600)
    store.current.write_bytes(canonical_json(pointer).encode())
    store.current.chmod(0o400)
    with pytest.raises(PolicyViolation, match=error):
        store.current_projection()
