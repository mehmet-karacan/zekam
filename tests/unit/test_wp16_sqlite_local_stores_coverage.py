from __future__ import annotations

import datetime as dt
import os
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from zekam.domain.canonical import canonical_json, digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite import local_learning as learning
from zekam.infrastructure.sqlite import local_model_benchmark as benchmark
from zekam.infrastructure.sqlite import local_model_registry as registry
from zekam.infrastructure.sqlite import local_runtime as runtime

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)


def _private(path: Path) -> Path:
    path.mkdir(mode=0o700)
    return path


def _registry(tmp_path: Path) -> registry.SQLiteLocalModelRegistry:
    store = registry.SQLiteLocalModelRegistry(
        (_private(tmp_path / "registry") / "models.db").resolve()
    )
    store.bootstrap()
    return store


def _snapshot(*models: registry.LocalModelIdentity) -> registry.LocalDiscoverySnapshot:
    return registry.LocalDiscoverySnapshot(
        "device-one",
        "opencode",
        "1.0.0",
        digest("client"),
        True,
        models,
        NOW,
        NOW + dt.timedelta(hours=1),
    )


@pytest.mark.parametrize("value", [None, 1, True, "", " padded ", "secret"])
def test_registry_text_and_time_validators_fail_closed(value: object) -> None:
    with pytest.raises(ValidationFailed):
        registry._text(value, "field")
    with pytest.raises(ValidationFailed):
        registry._instant(value)  # type: ignore[arg-type]
    with pytest.raises(PolicyViolation):
        registry._parse_instant(value)


def test_registry_identity_snapshot_and_profile_boundary_matrix() -> None:
    identity = registry.LocalModelIdentity("openai", "gpt-5", "revision-1")
    assert identity.exact_id == "openai/gpt-5"
    identity_changes: tuple[dict[str, object], ...] = (
        {"provider_id": "bad/provider"},
        {"model_id": ""},
        {"revision": " token=secret"},
    )
    for changes in identity_changes:
        with pytest.raises(ValidationFailed):
            replace(identity, **cast(Any, changes))
    snapshot = _snapshot(identity)
    snapshot_changes: tuple[dict[str, object], ...] = (
        {"client_id": "other"},
        {"listing_supported": 1},
        {"expires_at": NOW},
        {"expires_at": NOW + dt.timedelta(days=8)},
        {"models": [identity]},
        {"models": (object(),)},
        {"listing_supported": False},
    )
    for changes in snapshot_changes:
        with pytest.raises(ValidationFailed):
            replace(snapshot, **cast(Any, changes))
    profile = registry.LocalModelCapabilityProfile(
        snapshot.snapshot_digest,
        identity.exact_id,
        identity.revision_fingerprint,
        "gpt-family",
        "local-execution",
        ("chat",),
        ("text",),
        ("internal",),
        ("stream",),
    )
    assert profile.profile_digest == digest(profile.body())
    profile_changes: tuple[dict[str, object], ...] = (
        {"workloads": ()},
        {"modalities": ("text", "text")},
        {"capabilities": ["stream"]},
        {"data_classifications": ("bad/value",)},
    )
    for changes in profile_changes:
        with pytest.raises(ValidationFailed):
            replace(profile, **cast(Any, changes))


def test_registry_parser_reconcile_replay_stale_and_storage_identity(tmp_path: Path) -> None:
    assert [
        item.exact_id for item in registry.parse_opencode_models(b"openai/gpt-5\n", revision="1")
    ] == ["openai/gpt-5"]
    for payload in (b"", b"\xff", b" openai/gpt\n", b"provider-only\n"):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            registry.parse_opencode_models(payload, revision="1")
    store = _registry(tmp_path)
    model = registry.LocalModelIdentity("openai", "gpt-5", "1")
    snapshot = _snapshot(model)
    assert store.reconcile(snapshot) == {"new": 1}
    assert store.reconcile(snapshot) == {"new": 1}
    with pytest.raises(PolicyViolation, match="stale"):
        store.reconcile(replace(snapshot, observed_at=NOW - dt.timedelta(seconds=1)))
    with pytest.raises(PolicyViolation, match="snapshot missing"):
        store.profile(digest("absent"), now=NOW)
    hardlink = store.path.with_name("models-link.db")
    os.link(store.path, hardlink)
    with pytest.raises(PolicyViolation, match="identity invalid"):
        store.profile(snapshot.snapshot_digest, now=NOW)


def test_registry_health_and_capability_profile_fail_closed_matrix(tmp_path: Path) -> None:
    store = _registry(tmp_path)
    model = registry.LocalModelIdentity("openai", "gpt-5", "1")
    snapshot = _snapshot(model)
    store.reconcile(snapshot)
    with pytest.raises(ValidationFailed, match="bool"):
        store.record_health(
            snapshot.snapshot_digest,
            model.exact_id,
            model.revision_fingerprint,
            passed=1,  # type: ignore[arg-type]
            evidence_digest=digest("bad"),
            now=NOW,
        )
    with pytest.raises(PolicyViolation, match="current model"):
        store.record_health(
            snapshot.snapshot_digest,
            "openai/absent",
            model.revision_fingerprint,
            passed=True,
            evidence_digest=digest("absent"),
            now=NOW,
        )
    profile = registry.LocalModelCapabilityProfile(
        snapshot.snapshot_digest,
        model.exact_id,
        model.revision_fingerprint,
        "gpt-family",
        "local-execution",
        ("chat",),
        ("text",),
        ("internal",),
        ("stream",),
    )
    recorded = store.record_capability_profile(profile, now=NOW)
    assert store.record_capability_profile(profile, now=NOW) == recorded
    with pytest.raises(ValidationFailed, match="Exact"):
        store.record_capability_profile(object(), now=NOW)  # type: ignore[arg-type]
    with pytest.raises(PolicyViolation, match="discovered"):
        store.record_capability_profile(
            replace(profile, exact_id="openai/absent"),
            now=NOW,
        )


@pytest.mark.parametrize("raw", ["[]", '{"a":1,"a":2}', '{"value":NaN}', '{"b":1,"a":2}'])
def test_benchmark_canonical_parser_rejects_noncanonical_documents(raw: str) -> None:
    with pytest.raises(PolicyViolation):
        benchmark._canonical_document(raw)
    exact = canonical_json({"a": 1, "b": False})
    assert benchmark._canonical_document(exact) == {"a": 1, "b": False}


def test_benchmark_safe_time_and_constructor_path_guards(tmp_path: Path) -> None:
    for value in (None, 1, "", "space value", "password"):
        with pytest.raises(ValidationFailed):
            benchmark._safe(value, "field")
    for value in (None, True, "2026-09-04T12:00:00"):
        with pytest.raises(ValidationFailed):
            benchmark._instant(value)  # type: ignore[arg-type]
    with pytest.raises(ValidationFailed, match="paths"):
        benchmark.SQLiteLocalBenchmarkLab(Path("relative.db"), tmp_path.resolve())
    if os.name == "nt":
        pytest.skip("unprivileged Windows file symlink creation is unavailable")
    root = _private(tmp_path / "private")
    database = root / "lab.db"
    database.touch()
    symlink = root / "link.db"
    symlink.symlink_to(database)
    with pytest.raises(ValidationFailed, match="paths"):
        benchmark.SQLiteLocalBenchmarkLab(symlink, (root / "artifacts").resolve())


def _task() -> benchmark.LocalBenchmarkTask:
    return benchmark.LocalBenchmarkTask(
        "task-one",
        1,
        digest("fixture"),
        digest("prompt"),
        digest("hidden"),
        digest("grader"),
        5,
        10,
    )


def test_benchmark_value_objects_reject_wrong_types_and_bounds() -> None:
    task = _task()
    task_changes: tuple[dict[str, object], ...] = (
        {"version": True},
        {"repetitions": 4},
        {"repetitions": 101},
        {"timeout_seconds": 0},
        {"timeout_seconds": 601},
    )
    for changes in task_changes:
        with pytest.raises(ValidationFailed):
            replace(task, **cast(Any, changes))
    grader = benchmark.LocalGraderContract("grader", 1, digest("implementation"), ("correctness",))
    grader_changes: tuple[dict[str, object], ...] = (
        {"version": 0},
        {"dimensions": ()},
        {"dimensions": ("quality", "quality")},
        {"dimensions": ["quality"]},
    )
    for changes in grader_changes:
        with pytest.raises(ValidationFailed):
            replace(grader, **cast(Any, changes))
    for value in (None, True, 1, "0.5", float("nan"), float("inf"), -0.1, 1.1):
        with pytest.raises(ValidationFailed):
            benchmark.parse_score(value)
    assert benchmark.parse_score(0.5) == 0.5


def test_benchmark_blind_and_dry_report_guards() -> None:
    packet = benchmark.blind_pair(
        digest("plan"), "model-a", digest("left"), "model-b", digest("right")
    )
    assert {alias for alias, _ in packet.aliases} == {"A", "B"}
    with pytest.raises(PolicyViolation, match="distinct"):
        benchmark.blind_pair(digest("plan"), "same", digest("left"), "same", digest("right"))
    report = benchmark.DryRunReport(digest("plan"), 5, 10, 10)
    assert report.report_digest.startswith("sha256:")
    report_changes: tuple[dict[str, object], ...] = (
        {"trial_count": True},
        {"call_count": -1},
        {"max_calls": benchmark.MAX_CALLS + 1},
        {"call_count": 11},
    )
    for changes in report_changes:
        with pytest.raises((PolicyViolation, ValidationFailed)):
            replace(report, **cast(Any, changes))


def test_benchmark_bootstrap_schema_and_file_identity_fail_closed(tmp_path: Path) -> None:
    root = _private(tmp_path / "benchmark")
    lab = benchmark.SQLiteLocalBenchmarkLab(
        (root / "lab.db").resolve(), (root / "artifacts").resolve()
    )
    lab.bootstrap()
    assert lab.counts()["benchmark_plan"] == 0
    with sqlite3.connect(lab.path) as db:
        db.execute("drop trigger contract_no_delete")
    with pytest.raises(PolicyViolation, match="schema drift"):
        lab.counts()


def test_learning_validators_drafts_empty_audit_and_schema_drift(tmp_path: Path) -> None:
    for value in (None, True, "", " " * 2, "x" * 4097):
        with pytest.raises(ValidationFailed):
            learning._text(value, "field")
    for value in (None, "2026-09-04T12:00:00", "not-time"):
        with pytest.raises((ValidationFailed, PolicyViolation)):
            if value is None:
                learning._time(value)  # type: ignore[arg-type]
            else:
                learning._parse_time(value)
    with pytest.raises(ValidationFailed, match="exceeds"):
        learning._body({"content": "x" * (learning.MAX_BODY_BYTES + 1)})
    card = learning.FailureCardDraft(
        "symptom",
        "environment",
        "root",
        "unsafe",
        "safe",
        "verify",
        ("receipt:a", "receipt:b"),
        "author",
        "reviewer",
    )
    assert card.author_ref != card.reviewed_by
    with pytest.raises(PolicyViolation, match="independent"):
        replace(card, reviewed_by="author")
    with pytest.raises(ValidationFailed, match="source refs"):
        replace(card, source_refs=("one",))

    skill = learning.SkillManifestDraft(
        "skill-one",
        1,
        "purpose",
        ("trigger",),
        ("input",),
        ("output",),
        (),
        ("step",),
        ("check",),
        ("risk",),
        "read-only",
        ("receipt:a",),
        "rollback",
        "deprecate",
        "author",
    )
    assert skill.body()["grants_authority"] is False
    skill_changes: tuple[dict[str, object], ...] = (
        {"version": 0},
        {"triggers": ()},
        {"inputs": ("same", "same")},
        {"required_tools": tuple(str(item) for item in range(17))},
        {"permissions_ceiling": "network"},
    )
    for changes in skill_changes:
        with pytest.raises(ValidationFailed):
            replace(skill, **cast(Any, changes))

    operational = runtime.SQLiteLocalRuntimeStore((tmp_path / "operational.db").resolve())
    store = learning.SQLiteLocalLearning(
        (tmp_path / "learning.db").resolve(), operational_path=operational.path.resolve()
    )
    store.bootstrap()
    assert store.audit()["memory_candidate"] == 0
    with sqlite3.connect(store.path) as db:
        db.execute("drop trigger memory_candidate_no_delete")
    with pytest.raises(PolicyViolation, match="schema drift"):
        store.audit()


def test_runtime_validators_config_replay_and_rollback(tmp_path: Path) -> None:
    assert runtime._moment(None).tzinfo is not None
    moment_values: tuple[object, ...] = ("", "not-time", "2026-09-04T12:00:00")
    for value in moment_values:
        with pytest.raises(ValidationFailed):
            runtime._moment(cast(Any, value))
    required_values: tuple[object, ...] = (None, "", "x" * 513)
    for value in required_values:
        with pytest.raises(ValidationFailed):
            runtime._required(cast(Any, value), "field")
    bounded_values: tuple[object, ...] = (True, "1", 0, 2)
    for value in bounded_values:
        with pytest.raises(ValidationFailed):
            runtime._bounded_int(cast(Any, value), "bounded", minimum=1, maximum=1)
    payloads: tuple[object, ...] = (
        [],
        {"value": float("nan")},
        {"value": "x" * 1_048_577},
    )
    for payload in payloads:
        with pytest.raises(ValidationFailed):
            runtime._payload_json(cast(Any, payload))
    for observed in (1, "", " padded ", "x" * 513):

        def probe(_pid: int, result: object = observed) -> Any:
            return result

        with pytest.raises(ValidationFailed):
            runtime._process_probe_value(probe, 1)

    path = (tmp_path / "runtime.db").resolve()
    store = runtime.SQLiteLocalRuntimeStore(path, max_pending_outbox=2)
    assert store.max_pending_outbox == 2
    reopened = runtime.SQLiteLocalRuntimeStore(path, max_pending_outbox=2, existing_only=True)
    assert reopened.status().pending_outbox == 0
    with pytest.raises(PolicyViolation, match="config drift"):
        runtime.SQLiteLocalRuntimeStore(path, max_pending_outbox=3, existing_only=True)
    assert runtime.SQLiteLocalRuntimeStore(path, existing_only=True).max_pending_outbox == 2
    with pytest.raises(ValidationFailed, match="open_only bool"):
        store.recovery_cases(open_only=1)  # type: ignore[arg-type]
    with sqlite3.connect(path) as db:
        assert db.execute("select max_pending_outbox from local_runtime_config").fetchone() == (2,)


def test_store_path_guards_reject_relative_symlink_and_shared_database(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed):
        registry.SQLiteLocalModelRegistry(Path("relative.db"))
    if os.name == "nt":
        pytest.skip("unprivileged Windows file symlink creation is unavailable")
    target = tmp_path / "target.db"
    target.touch()
    link = tmp_path / "link.db"
    link.symlink_to(target)
    with pytest.raises(ValidationFailed):
        benchmark.SQLiteLocalBenchmarkLab(link, (tmp_path / "artifacts").resolve())
    with pytest.raises(ValidationFailed):
        learning.SQLiteLocalLearning(target.resolve(), operational_path=target.resolve())
