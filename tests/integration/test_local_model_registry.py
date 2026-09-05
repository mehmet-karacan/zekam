from __future__ import annotations

import datetime as dt
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from zekam.domain.canonical import digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.infrastructure.sqlite.local_model_registry import (
    LocalDiscoverySnapshot,
    LocalModelIdentity,
    SQLiteLocalModelRegistry,
    parse_opencode_models,
)

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.UTC)


def _registry(tmp_path: Path) -> SQLiteLocalModelRegistry:
    store = SQLiteLocalModelRegistry((tmp_path / "models.db").resolve())
    store.bootstrap()
    return store


def _snapshot(*models: LocalModelIdentity, at: dt.datetime = NOW) -> LocalDiscoverySnapshot:
    return LocalDiscoverySnapshot(
        "mac-device",
        "opencode",
        "1.2.3",
        digest("opencode-artifact"),
        True,
        tuple(models),
        at,
        at + dt.timedelta(hours=24),
    )


def test_exact_discovery_reconcile_health_profile_restart_and_stale(tmp_path: Path) -> None:
    store = _registry(tmp_path)
    alpha = LocalModelIdentity("openai", "alpha", "rev-1")
    beta = LocalModelIdentity("local", "beta", "rev-1")
    first = _snapshot(alpha, beta)
    assert store.reconcile(first) == {"new": 2}
    assert store.reconcile(first) == {"new": 2}
    assert store.routable(device_id="mac-device", client_id="opencode", now=NOW) == ()
    for model in (alpha, beta):
        store.record_health(
            first.snapshot_digest,
            model.exact_id,
            model.revision_fingerprint,
            passed=True,
            evidence_digest=digest(model.exact_id),
            now=NOW,
        )
    assert store.routable(device_id="mac-device", client_id="opencode", now=NOW) == (
        "local/beta",
        "openai/alpha",
    )
    changed = LocalModelIdentity("openai", "alpha", "rev-2")
    second = _snapshot(changed, at=NOW + dt.timedelta(hours=1))
    assert store.reconcile(second) == {"changed": 1, "removed": 1}
    assert (
        store.routable(
            device_id="mac-device", client_id="opencode", now=NOW + dt.timedelta(hours=1)
        )
        == ()
    )
    store.record_health(
        second.snapshot_digest,
        changed.exact_id,
        changed.revision_fingerprint,
        passed=True,
        evidence_digest=digest("changed"),
        now=NOW + dt.timedelta(hours=1),
    )
    restarted = SQLiteLocalModelRegistry(store.path)
    assert restarted.routable(
        device_id="mac-device", client_id="opencode", now=NOW + dt.timedelta(hours=1)
    ) == ("openai/alpha",)
    assert (
        restarted.routable(
            device_id="mac-device", client_id="opencode", now=NOW + dt.timedelta(days=2)
        )
        == ()
    )
    assert restarted.profile(second.snapshot_digest, now=NOW + dt.timedelta(hours=1)).startswith(
        "sha256:"
    )


def test_ambiguous_and_prefix_ids_are_never_merged_or_routed(tmp_path: Path) -> None:
    store = _registry(tmp_path)
    short = LocalModelIdentity("openai", "gpt-5", "a")
    long = LocalModelIdentity("openai", "gpt-5-mini", "a")
    duplicate_changed = LocalModelIdentity("openai", "gpt-5", "b")
    snapshot = _snapshot(short, long, duplicate_changed)
    assert store.reconcile(snapshot) == {"ambiguous": 1, "new": 1}
    store.record_health(
        snapshot.snapshot_digest,
        long.exact_id,
        long.revision_fingerprint,
        passed=True,
        evidence_digest=digest("long"),
        now=NOW,
    )
    assert store.routable(device_id="mac-device", client_id="opencode", now=NOW) == (
        "openai/gpt-5-mini",
    )
    with pytest.raises(PolicyViolation):
        store.record_health(
            snapshot.snapshot_digest,
            short.exact_id,
            short.revision_fingerprint,
            passed=True,
            evidence_digest=digest("short"),
            now=NOW,
        )


@pytest.mark.parametrize(
    "payload",
    [b"", b"openai", b" openai/gpt\n", b"openai/gpt\nopenai/gpt\n", b"sk-secret123456/openai\n"],
)
def test_parser_rejects_empty_malformed_duplicate_or_sensitive(payload: bytes) -> None:
    with pytest.raises((ValidationFailed, PolicyViolation)):
        models = parse_opencode_models(payload, revision="1")
        if len({item.exact_id for item in models}) != len(models):
            raise ValidationFailed("duplicate")


def test_codex_cannot_carry_guessed_models_and_corruption_is_append_only(tmp_path: Path) -> None:
    model = LocalModelIdentity("openai", "guessed", "1")
    with pytest.raises(ValidationFailed):
        LocalDiscoverySnapshot(
            "mac-device",
            "codex",
            "0.151.0",
            digest("codex-artifact"),
            False,
            (model,),
            NOW,
            NOW + dt.timedelta(hours=1),
        )
    store = _registry(tmp_path)
    snapshot = _snapshot(model)
    store.reconcile(snapshot)
    with sqlite3.connect(store.path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("delete from discovery_snapshot")


def test_concurrent_exact_snapshot_keeps_one_append_only_graph(tmp_path: Path) -> None:
    store = _registry(tmp_path)
    snapshot = _snapshot(LocalModelIdentity("openai", "alpha", "1"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: store.reconcile(snapshot), range(2)))
    assert results == ({"new": 1}, {"new": 1})
    with sqlite3.connect(store.path) as db:
        assert db.execute("select count(*) from discovery_snapshot").fetchone() == (1,)
        assert db.execute("select count(*) from reconcile_event").fetchone() == (1,)


def test_quarantine_cooldown_and_schema_drift_are_fail_closed(tmp_path: Path) -> None:
    store = _registry(tmp_path)
    model = LocalModelIdentity("openai", "alpha", "1")
    snapshot = _snapshot(model)
    store.reconcile(snapshot)
    for index in range(2):
        store.record_health(
            snapshot.snapshot_digest,
            model.exact_id,
            model.revision_fingerprint,
            passed=False,
            evidence_digest=digest(f"failure-{index}"),
            now=NOW + dt.timedelta(seconds=index),
        )
    store.record_health(
        snapshot.snapshot_digest,
        model.exact_id,
        model.revision_fingerprint,
        passed=True,
        evidence_digest=digest("recovered"),
        now=NOW + dt.timedelta(seconds=2),
    )
    assert (
        store.routable(
            device_id="mac-device", client_id="opencode", now=NOW + dt.timedelta(minutes=1)
        )
        == ()
    )
    assert store.routable(
        device_id="mac-device", client_id="opencode", now=NOW + dt.timedelta(minutes=6)
    ) == ("openai/alpha",)
    with sqlite3.connect(store.path) as db:
        db.execute("drop trigger health_observation_no_delete")
    with pytest.raises(PolicyViolation, match="schema drift"):
        SQLiteLocalModelRegistry(store.path).routable(
            device_id="mac-device", client_id="opencode", now=NOW
        )


def test_health_evidence_replay_rejects_payload_drift(tmp_path: Path) -> None:
    store = _registry(tmp_path)
    model = LocalModelIdentity("openai", "alpha", "1")
    snapshot = _snapshot(model)
    store.reconcile(snapshot)
    evidence = digest("same-evidence")
    recorded = store.record_health(
        snapshot.snapshot_digest,
        model.exact_id,
        model.revision_fingerprint,
        passed=True,
        evidence_digest=evidence,
        now=NOW,
    )
    assert (
        store.record_health(
            snapshot.snapshot_digest,
            model.exact_id,
            model.revision_fingerprint,
            passed=True,
            evidence_digest=evidence,
            now=NOW,
        )
        == recorded
    )
    with pytest.raises(PolicyViolation, match="payload drift"):
        store.record_health(
            snapshot.snapshot_digest,
            model.exact_id,
            model.revision_fingerprint,
            passed=False,
            evidence_digest=evidence,
            now=NOW,
        )


def test_health_outside_snapshot_lifetime_never_routes(tmp_path: Path) -> None:
    store = _registry(tmp_path)
    model = LocalModelIdentity("openai", "alpha", "1")
    snapshot = _snapshot(model)
    store.reconcile(snapshot)
    for when in (NOW - dt.timedelta(seconds=1), NOW + dt.timedelta(hours=24)):
        with pytest.raises(PolicyViolation, match="outside discovery lifetime"):
            store.record_health(
                snapshot.snapshot_digest,
                model.exact_id,
                model.revision_fingerprint,
                passed=True,
                evidence_digest=digest(when.isoformat()),
                now=when,
            )
