"""`zekam backup` uctan uca akisi: manifest uret, dogrula, bozulmayi yakala."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from zekam.application.backup import BACKUP_MANIFEST_SCHEMA
from zekam.application.composition import build_context
from zekam.application.home import HomeLayout
from zekam.application.local_continuity_source_authority import BACKUP_RESTORE_READY
from zekam.domain.canonical import canonical_json, digest
from zekam.infrastructure import local_backup
from zekam.infrastructure.local_core_services import LocalCoreServices
from zekam.infrastructure.sqlite.local_continuity_source_authority import (
    DDL_DIGEST,
    SCHEMA_FINGERPRINT,
    SIDE_CAR_DDL,
)
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore
from zekam.interfaces.cli import backup as backup_commands
from zekam.interfaces.cli.main import app

pytestmark = pytest.mark.e2e

runner = CliRunner()


def _prepare(home: Path) -> LocalContentAddressedStore:
    runner.invoke(app, ["init", "--home", str(home)])
    return LocalContentAddressedStore(home / "artifacts" / "sha256").ensure()


def test_project_sqlite_wal_is_snapshotted_and_legacy_bundle_is_explicit(
    home_root: Path,
    tmp_path: Path,
) -> None:
    store = _prepare(home_root)
    opaque = b"SQLite format 3\x00opaque-artifact-fixture"
    artifact = store.put(opaque)
    project_db = home_root / "projeler" / "wal-fixture" / "runtime" / "provider-ledger.sqlite3"
    project_db.parent.mkdir(parents=True, mode=0o700)
    writer = sqlite3.connect(project_db)
    try:
        writer.execute("pragma journal_mode=wal")
        writer.execute("pragma wal_autocheckpoint=0")
        from zekam.infrastructure.opencode_provider_ledger import SCHEMA

        writer.executescript(SCHEMA)
        writer.execute("create table fixture(value text not null)")
        writer.commit()
        writer.execute("pragma wal_checkpoint(truncate)")
        writer.execute("insert into fixture values('committed-in-wal')")
        writer.commit()
        project_db.chmod(0o600)
        assert Path(str(project_db) + "-wal").stat().st_size > 0
        trust = home_root / "config" / "git-publication-trust.json"
        trust.parent.mkdir(exist_ok=True, mode=0o700)
        trust.write_text('{"fixture":"public-keys-only"}', encoding="utf-8")
        trust.chmod(0o400)
        services = LocalCoreServices.from_context(build_context(home=str(home_root)))
        bundle = (tmp_path / "wal-bundle").resolve()
        document = local_backup.create_bundle(services, home_root, bundle)
        relative = project_db.relative_to(home_root).as_posix()
        entry = next(item for item in document["entries"] if item["path"] == relative)
        assert document["schema"] == local_backup.BUNDLE_SCHEMA
        assert entry["kind"] == "sqlite"
        assert "config/git-publication-trust.json" in {item["path"] for item in document["entries"]}
        assert not any(item["path"].endswith(("-wal", "-shm")) for item in document["entries"])
        with sqlite3.connect(
            f"{(bundle / relative).as_uri()}?mode=ro&immutable=1", uri=True
        ) as snapshot:
            assert snapshot.execute("select value from fixture").fetchall() == [
                ("committed-in-wal",)
            ]
        assert store.get(artifact.digest) == opaque
        local_backup.verify_bundle(bundle)
        restored = (tmp_path / "restored-v2").resolve()
        receipt = local_backup.restore_bundle(bundle, restored)
        assert receipt["project_sqlite_snapshot_contract"] is True
        with sqlite3.connect(
            f"{(restored / relative).as_uri()}?mode=ro&immutable=1", uri=True
        ) as snapshot:
            assert snapshot.execute("select value from fixture").fetchall() == [
                ("committed-in-wal",)
            ]

        # A v1 wire envelope remains readable; it is never called v2 snapshot evidence.
        document["schema"] = local_backup.LEGACY_BUNDLE_SCHEMA
        document.pop("capture_contract")
        entry["kind"] = "file"
        document["manifest_digest"] = digest(
            {k: v for k, v in document.items() if k != "manifest_digest"}
        )
        manifest = bundle / "MANIFEST.json"
        manifest.chmod(0o600)
        manifest.write_text(canonical_json(document), encoding="utf-8")
        manifest.chmod(0o400)
        local_backup.verify_bundle(bundle)
        legacy = local_backup.restore_bundle(bundle, (tmp_path / "restored-v1").resolve())
        assert legacy["project_sqlite_snapshot_contract"] is False
        assert legacy["bundle_schema"] == local_backup.LEGACY_BUNDLE_SCHEMA
    finally:
        writer.close()


@pytest.mark.parametrize(
    "path",
    [
        "projeler/a\\..\\..\\escape",
        "C:/escape",
        "projeler/a:stream",
        "projeler/a/../escape",
        ".",
        "a\x00b",
    ],
)
def test_bundle_paths_reject_windows_escape_and_alternate_stream(path: str) -> None:
    from zekam.domain.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        local_backup._safe_relative(path)


def test_bundle_fences_all_databases_then_releases_them(
    home_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(home_root)
    services = LocalCoreServices.from_context(build_context(home=str(home_root)))
    databases = [path for _name, path, kind in local_backup._sources(home_root) if kind == "sqlite"]
    original = local_backup._snapshot_database
    checked = []

    def snapshot(source, target, mode, **kwargs):
        if not checked:
            for database in databases:
                connection = sqlite3.connect(database, timeout=0, isolation_level=None)
                try:
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        connection.execute("begin immediate")
                finally:
                    connection.close()
                checked.append(database)
        return original(source, target, mode, **kwargs)

    monkeypatch.setattr(local_backup, "_snapshot_database", snapshot)
    document = local_backup.create_bundle(
        services, home_root, (tmp_path / "cohort-bundle").resolve()
    )
    assert len(checked) == len(databases)
    assert document["capture_contract"]["grants_authority"] is False
    for database in databases:
        connection = sqlite3.connect(database, timeout=0, isolation_level=None)
        try:
            connection.execute("begin immediate")
            connection.rollback()
        finally:
            connection.close()


def test_bundle_rejects_active_work_without_expiring_its_owner(
    home_root: Path, tmp_path: Path
) -> None:
    from zekam.domain.errors import ZekamError

    _prepare(home_root)
    services = LocalCoreServices.from_context(build_context(home=str(home_root)))
    job, _ = services.runtime.enqueue(
        idempotency_key="backup-active-fixture", payload={"operation": "fixture", "effect": {}}
    )
    work = services.runtime.claim_next(
        owner_id="fixture-owner",
        owner_pid=os.getpid(),
        owner_token="fixture-owner-token",
        lease_seconds=60,
        supported_operations=("fixture",),
        job_id=job.id,
    )
    assert work is not None
    before = services.runtime.job_snapshot(job.id)
    destination = (tmp_path / "active-rejected").resolve()
    with pytest.raises(ZekamError, match="quiescent"):
        local_backup.create_bundle(services, home_root, destination)
    assert services.runtime.job_snapshot(job.id) == before
    assert not destination.exists()
    assert not list(destination.parent.glob(".active-rejected.*"))


def test_bundle_rejects_regular_file_drift_after_snapshot_boundary(
    home_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zekam.domain.errors import PolicyViolation

    _prepare(home_root)
    services = LocalCoreServices.from_context(build_context(home=str(home_root)))
    original = local_backup._copy_regular

    def mutate_then_copy(source, target, mode):
        if source == home_root / "config.yaml":
            source.write_bytes(source.read_bytes() + b"\n# fixture mutation after snapshot\n")
        return original(source, target, mode)

    monkeypatch.setattr(local_backup, "_copy_regular", mutate_then_copy)
    destination = (tmp_path / "drift-rejected").resolve()
    with pytest.raises(PolicyViolation, match="snapshot boundary"):
        local_backup.create_bundle(services, home_root, destination)
    assert not destination.exists()


def test_bundle_snapshot_window_expiry_leaves_no_partial_target(
    home_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from zekam.domain.errors import PolicyViolation

    _prepare(home_root)
    services = LocalCoreServices.from_context(build_context(home=str(home_root)))
    monkeypatch.setattr(local_backup, "MAX_SNAPSHOT_WINDOW_SECONDS", 0.0)
    destination = (tmp_path / "expired-window").resolve()
    with pytest.raises(PolicyViolation, match="snapshot window"):
        local_backup.create_bundle(services, home_root, destination)
    assert not destination.exists()
    assert not list(destination.parent.glob(".expired-window.*"))


def test_manifest_is_created_and_verified(home_root: Path) -> None:
    store = _prepare(home_root)
    store.put(b"rapor icerigi", media_type="text/plain")
    manifest_path = home_root / "yedek.json"

    created = runner.invoke(
        app, ["backup", "create", "--home", str(home_root), "--cikti", str(manifest_path)]
    )
    assert created.exit_code == 0, created.stdout

    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert document["schema"] == BACKUP_MANIFEST_SCHEMA
    assert len(document["artifacts"]) == 1

    verified = runner.invoke(
        app, ["backup", "verify", str(manifest_path), "--home", str(home_root)]
    )
    assert verified.exit_code == 0
    assert '"valid"' in verified.stdout


def test_verification_fails_after_artifact_is_removed(home_root: Path) -> None:
    store = _prepare(home_root)
    info = store.put(b"silinecek")
    manifest_path = home_root / "yedek.json"
    runner.invoke(
        app, ["backup", "create", "--home", str(home_root), "--cikti", str(manifest_path)]
    )

    store.delete(info.digest)
    result = runner.invoke(app, ["backup", "verify", str(manifest_path), "--home", str(home_root)])
    assert result.exit_code == 2
    assert "incomplete" in result.stdout


def test_verification_fails_after_manifest_is_edited(home_root: Path) -> None:
    _prepare(home_root)
    manifest_path = home_root / "yedek.json"
    runner.invoke(
        app, ["backup", "create", "--home", str(home_root), "--cikti", str(manifest_path)]
    )

    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["product_version"] = "99.0.0"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")

    result = runner.invoke(app, ["backup", "verify", str(manifest_path), "--home", str(home_root)])
    assert result.exit_code == 2
    assert "altered" in result.stdout


def test_manifest_to_stdout_is_json(home_root: Path) -> None:
    _prepare(home_root)
    result = runner.invoke(app, ["backup", "create", "--home", str(home_root)])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["product"] == "Zekam"


def test_sqlite_backup_manifest_has_no_remote_connector(home_root: Path) -> None:
    _prepare(home_root)
    result = runner.invoke(app, ["backup", "create", "--home", str(home_root)])

    assert result.exit_code == 0
    assert not hasattr(backup_commands, "connect")


def test_source_authority_backup_boundary_is_honestly_not_ready(home_root: Path) -> None:
    _prepare(home_root)
    project = "22222222-2222-4222-8222-222222222222"
    HomeLayout(home_root).ensure_project(project)
    portable = home_root / "projeler" / project / "baglantilar" / ("a" * 64 + ".json")
    portable.write_text('{"portable":true}', encoding="utf-8")
    local = home_root / "yerel" / "source-authority.sqlite3"
    local.write_bytes(b"local-root-canary")
    result = runner.invoke(app, ["backup", "create", "--home", str(home_root)])
    assert result.exit_code == 0
    document = json.loads(result.stdout)
    rendered = json.dumps(document, sort_keys=True)
    assert BACKUP_RESTORE_READY is False
    assert portable.name not in rendered
    assert local.name not in rendered
    assert "local-root-canary" not in rendered


def test_complete_local_bundle_verifies_and_restores_without_overwrite(
    home_root: Path, tmp_path: Path
) -> None:
    _prepare(home_root).put(b"immutable-local-artifact")
    for relative in (
        "analytics/raw/segment.jsonl",
        "analytics/manifests/segment.json",
        "analytics/generations/generation.duckdb",
        "analytics/reports/morning.json",
        "analytics/receipts/rebuild.json",
        "analytics/CURRENT",
        "benchmarklar/artifacts/result.json",
    ):
        path = home_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(relative.encode())
        path.chmod(0o400)
    authority = home_root / "yerel" / "source-authority.sqlite3"
    authority.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(authority) as connection:
        connection.execute("pragma page_size=4096")
        connection.executescript(SIDE_CAR_DDL)
        timestamp = "2026-09-04T12:00:00.000000Z"
        connection.execute(
            "insert into local_source_authority_meta values(1,1,?,?,?)",
            (SCHEMA_FINGERPRINT, "11111111-1111-4111-8111-111111111111", timestamp),
        )
        connection.execute(
            "insert into local_source_authority_migration values(1,'source-authority-v1',?,?)",
            (DDL_DIGEST, timestamp),
        )
    os.chmod(authority, 0o600)
    (home_root / "knowledge-index" / "manifests" / "current.json").write_text(
        '{"generation":1}', encoding="utf-8"
    )
    bundle = (tmp_path / "bundle").absolute()
    manifest = tmp_path / "bundle-manifest.json"
    created = runner.invoke(
        app,
        [
            "backup",
            "create",
            "--home",
            str(home_root),
            "--bundle",
            str(bundle),
            "--cikti",
            str(manifest),
        ],
    )
    assert created.exit_code == 0, created.stdout
    document = json.loads(manifest.read_text(encoding="utf-8"))
    paths = {entry["path"] for entry in document["entries"]}
    assert {
        "state/operational.db",
        "state/learning.db",
        "state/improvement.db",
        "yerel/source-authority.sqlite3",
        "modeller/registry/models.db",
        "benchmarklar/benchmark.db",
        "modeller/routing/routing.db",
        "knowledge-index/manifests/current.json",
        "analytics/raw/segment.jsonl",
        "analytics/manifests/segment.json",
        "analytics/generations/generation.duckdb",
        "analytics/reports/morning.json",
        "analytics/CURRENT",
        "benchmarklar/artifacts/result.json",
    } <= paths
    verified = runner.invoke(app, ["backup", "verify", str(manifest), "--bundle", str(bundle)])
    assert verified.exit_code == 0, verified.stdout
    target = (tmp_path / "restored").absolute()
    restored = runner.invoke(app, ["backup", "restore", str(bundle), str(target)])
    assert restored.exit_code == 0, restored.stdout
    assert (target / "state" / "operational.db").read_bytes()
    assert LocalCoreServices.from_context(build_context(home=str(target))).status()["all_ready"]
    restored_status = LocalCoreServices.from_context(build_context(home=str(target))).status()
    restored_databases = restored_status["databases"]
    assert isinstance(restored_databases, dict)
    source_authority = restored_databases["source_authority"]
    assert isinstance(source_authority, dict) and source_authority["schema_ok"]
    replay = runner.invoke(app, ["backup", "restore", str(bundle), str(target)])
    assert replay.exit_code == 70


def test_complete_local_bundle_rejects_missing_and_corrupt_entries(
    home_root: Path, tmp_path: Path
) -> None:
    _prepare(home_root)
    bundle = (tmp_path / "bundle-corrupt").absolute()
    created = runner.invoke(
        app, ["backup", "create", "--home", str(home_root), "--bundle", str(bundle)]
    )
    assert created.exit_code == 0, created.stdout
    document = json.loads((bundle / "MANIFEST.json").read_text(encoding="utf-8"))
    victim = bundle / document["entries"][0]["path"]
    victim.chmod(0o600)
    victim.write_bytes(b"corrupt")
    verified = runner.invoke(
        app, ["backup", "verify", str(bundle / "MANIFEST.json"), "--bundle", str(bundle)]
    )
    assert verified.exit_code == 70


def test_valid_empty_sqlite_fails_status_create_verify_and_atomic_restore(
    home_root: Path, tmp_path: Path
) -> None:
    _prepare(home_root)
    bundle = (tmp_path / "semantic-bundle").absolute()
    assert (
        runner.invoke(
            app, ["backup", "create", "--home", str(home_root), "--bundle", str(bundle)]
        ).exit_code
        == 0
    )

    learning = home_root / "state" / "learning.db"
    learning.unlink()
    sqlite3.connect(learning).close()
    learning.chmod(0o600)
    status = LocalCoreServices.from_context(build_context(home=str(home_root))).status()
    assert status["all_ready"] is False
    databases = status["databases"]
    assert isinstance(databases, dict)
    learning_status = databases["learning"]
    assert isinstance(learning_status, dict) and learning_status["schema_ok"] is False
    rejected = (tmp_path / "rejected-bundle").absolute()
    assert (
        runner.invoke(
            app, ["backup", "create", "--home", str(home_root), "--bundle", str(rejected)]
        ).exit_code
        == 70
    )
    assert not rejected.exists()

    bundled_learning = bundle / "state" / "learning.db"
    bundled_learning.chmod(0o600)
    bundled_learning.unlink()
    sqlite3.connect(bundled_learning).close()
    bundled_learning.chmod(0o600)
    manifest_path = bundle / "MANIFEST.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in document["entries"]:
        if entry["path"] == "state/learning.db":
            raw = bundled_learning.read_bytes()
            entry["size_bytes"] = len(raw)
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            break
    document["total_bytes"] = sum(entry["size_bytes"] for entry in document["entries"])
    body = dict(document)
    body.pop("manifest_digest")
    document["manifest_digest"] = digest(body)
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(canonical_json(document).encode())
    manifest_path.chmod(0o400)

    verified = runner.invoke(app, ["backup", "verify", str(manifest_path), "--bundle", str(bundle)])
    assert verified.exit_code == 70
    target = (tmp_path / "must-not-exist").absolute()
    restored = runner.invoke(app, ["backup", "restore", str(bundle), str(target)])
    assert restored.exit_code == 70
    assert not target.exists()


def test_bundle_rejects_source_census_drift_before_publish(
    home_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(home_root)
    late = home_root / "analytics" / "raw" / "late.jsonl"
    original = local_backup._copy_regular
    mutated = False

    def copy_and_add_source(source: Path, target: Path, mode: int) -> None:
        nonlocal mutated
        original(source, target, mode)
        if not mutated:
            late.parent.mkdir(parents=True, exist_ok=True)
            late.write_bytes(b"added-during-backup")
            late.chmod(0o600)
            mutated = True

    monkeypatch.setattr(local_backup, "_copy_regular", copy_and_add_source)
    bundle = (tmp_path / "census-drift").absolute()
    result = runner.invoke(
        app, ["backup", "create", "--home", str(home_root), "--bundle", str(bundle)]
    )
    assert result.exit_code == 70
    assert not bundle.exists()


def test_bundle_rechecks_source_census_before_publish(
    home_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(home_root)
    late = home_root / "analytics" / "raw" / "late-after-census.jsonl"
    bundle = (tmp_path / "census-final-drift").absolute()
    original = local_backup._sync_directory
    mutated = False

    def sync_and_add_source(path: Path) -> None:
        nonlocal mutated
        original(path)
        if (
            not mutated
            and path.parent == bundle.parent
            and path.name.startswith(f".{bundle.name}.")
        ):
            late.parent.mkdir(parents=True, exist_ok=True)
            late.write_bytes(b"added-after-census")
            late.chmod(0o600)
            mutated = True

    monkeypatch.setattr(local_backup, "_sync_directory", sync_and_add_source)
    result = runner.invoke(
        app, ["backup", "create", "--home", str(home_root), "--bundle", str(bundle)]
    )
    assert result.exit_code == 70
    assert mutated
    assert not bundle.exists()


@pytest.mark.parametrize("mutation", ["missing", "extra", "wrong-kind"])
def test_self_consistent_manifest_cannot_redefine_mandatory_store_contract(
    home_root: Path, tmp_path: Path, mutation: str
) -> None:
    _prepare(home_root)
    bundle = (tmp_path / f"contract-{mutation}").absolute()
    assert (
        runner.invoke(
            app, ["backup", "create", "--home", str(home_root), "--bundle", str(bundle)]
        ).exit_code
        == 0
    )
    manifest_path = bundle / "MANIFEST.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if mutation == "missing":
        (bundle / "state" / "learning.db").unlink()
        document["entries"] = [
            entry for entry in document["entries"] if entry["path"] != "state/learning.db"
        ]
    elif mutation == "wrong-kind":
        next(entry for entry in document["entries"] if entry["path"] == "state/learning.db")[
            "kind"
        ] = "file"
    else:
        extra = bundle / "state" / "extra.db"
        sqlite3.connect(extra).close()
        extra.chmod(0o600)
        raw = extra.read_bytes()
        document["entries"].append(
            {
                "path": "state/extra.db",
                "kind": "sqlite",
                "mode": 0o600,
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
        document["entries"].sort(key=lambda entry: entry["path"])
    document["file_count"] = len(document["entries"])
    document["total_bytes"] = sum(entry["size_bytes"] for entry in document["entries"])
    body = dict(document)
    body.pop("manifest_digest")
    document["manifest_digest"] = digest(body)
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(canonical_json(document).encode())
    manifest_path.chmod(0o400)

    assert (
        runner.invoke(
            app, ["backup", "verify", str(manifest_path), "--bundle", str(bundle)]
        ).exit_code
        == 70
    )
    target = (tmp_path / f"target-{mutation}").absolute()
    assert runner.invoke(app, ["backup", "restore", str(bundle), str(target)]).exit_code == 70
    assert not target.exists()
