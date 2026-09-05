"""Read-only environment admission, with the actual task and Akilli Kasa source."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from zekam.application.active_task_contract import ActiveTaskContract
from zekam.application.config import core_root, default_config_file, load_settings
from zekam.application.fresh_bootstrap import MAX_CONFIG_BYTES
from zekam.application.home import HOME_ENTRIES, HomeLayout
from zekam.application.local_continuity import ContinuityBinding
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.infrastructure import local_continuity_environment as module
from zekam.infrastructure.local_continuity_environment import LocalContinuityEnvironment
from zekam.infrastructure.sqlite import operational_schema as schema_module
from zekam.infrastructure.sqlite.local_continuity import SQLiteContinuityStore
from zekam.infrastructure.sqlite.operational_schema import SCHEMA_VERSION, bootstrap
from zekam.infrastructure.sqlite.operational_store import SQLiteOperationalStore

AKILLI_SOURCE = Path("/Users/mkaracan/Projeler/akilli-kasa/src/akilli_kasa/api/saglik.py")
type Fixture = tuple[LocalContinuityEnvironment, ContinuityBinding]


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    for key in module._ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    if not AKILLI_SOURCE.is_file():
        pytest.skip("Read-only real Akilli Kasa fixture unavailable")
    source_bytes = AKILLI_SOURCE.read_bytes()
    home = tmp_path / "home"
    HomeLayout(home).ensure()
    (home / "config.yaml").write_text(
        "schema: zekam-config/v1\ndatabase:\n  backend: sqlite\n"
        "  sqlite_relative_path: state/operational.db\n",
        encoding="utf-8",
    )
    task_path = core_root() / "AKTIF_GOREV.md"
    task = ActiveTaskContract.load(task_path)
    settings = load_settings(home=home, environ={})
    operational_path = home / "state/operational.db"
    bootstrap(operational_path)
    with SQLiteOperationalStore(operational_path).unit_of_work() as uow:
        config = uow.activate_config(
            config_digest=digest(settings.sanitized()),
            task_digest=task.source_digest,
            sanitized_config=settings.sanitized(),
        )
        project = uow.create_project(slug="akilli-kasa", display_name="Akilli Kasa")
        source = uow.bind_source(
            project_id=project.id, portable_ref="project/akilli-kasa", source_kind="directory"
        )
        snapshot = uow.capture_source_snapshot(
            source_binding_id=source.id,
            revision_ref="bounded-saglik-fixture",
            tree_digest=digest_of_bytes(source_bytes),
            content_digest=digest_of_bytes(source_bytes),
            config_digest=digest("one-real-file-read-only"),
        )
        work = uow.create_work(
            project_id=project.id,
            kind="task",
            title="Environment validation",
            state="ready",
            payload_digest=digest_of_bytes(source_bytes),
        )
        run = uow.create_run(
            work_item_id=work.id,
            config_revision_id=config.id,
            source_snapshot_id=snapshot.id,
            plan_digest=digest("environment-gate-test"),
            budget={"max_seconds": 60},
        )
        uow.commit()
    realm = str(uuid4())
    with sqlite3.connect(operational_path) as connection:
        connection.execute(
            "insert into project_knowledge_realm values(?,?,?)",
            (project.id, realm, "2026-09-02T18:00:00+00:00"),
        )
    binding = ContinuityBinding(
        session_id=str(uuid4()),
        external_session_id="session/environment-fixture",
        project_id=project.id,
        realm_id=realm,
        client_id="codex",
        device_id="test-device",
        source_snapshot_id=snapshot.id,
        task_digest=task.source_digest,
        plan_digest=run.plan_digest,
        policy_digest=config.config_digest,
        work_item_id=work.id,
        run_id=run.id,
    )
    SQLiteContinuityStore(operational_path).bind_session(binding)
    return LocalContinuityEnvironment(home, core_root(), task_path, operational_path), binding


def _tree(root: Path) -> dict[str, tuple[Any, ...]]:
    return {
        str(path.relative_to(root)): (
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
            path.read_bytes() if stat.S_ISREG(info.st_mode) else None,
        )
        for path in (root, *sorted(root.rglob("*")))
        for info in (path.lstat(),)
    }


def _logical_database(path: Path) -> str:
    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
        return "\n".join(connection.iterdump())


def test_real_task_and_akilli_work_validate_without_filesystem_mutation(
    environment: Fixture,
) -> None:
    gate, binding = environment
    before = _tree(gate.home)
    task_before = gate.task_path.read_bytes()
    source_before = AKILLI_SOURCE.read_bytes()
    result = gate.validate(binding)
    assert result["status"] == "validated"
    assert result["operational_schema_version"] == SCHEMA_VERSION
    assert result["task_digest"] == digest_of_bytes(task_before) == binding.task_digest
    assert result["policy_digest"] == binding.policy_digest
    assert result["read_only"] is result["authority_snapshot_only"] is True
    assert result["grants_authority"] is False
    assert result["provider_calls"] == result["network_calls"] == 0
    evidence = dict(result)
    evidence_digest = evidence.pop("evidence_digest")
    assert digest(evidence) == evidence_digest
    assert gate.validate(binding) == result
    assert _tree(gate.home) == before
    assert gate.task_path.read_bytes() == task_before
    assert AKILLI_SOURCE.read_bytes() == source_before
    assert str(gate.home) not in canonical_json(result)
    assert "sanitized_json" not in result


def test_no_historical_bootstrap_receipt_is_required(environment: Fixture) -> None:
    gate, binding = environment
    assert not (gate.home / "state/manifests/bootstrap-receipt.json").exists()
    assert gate.validate(binding)["status"] == "validated"


def test_no_write_or_provider_entry_points_are_called(
    environment: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, binding = environment
    statements: list[str] = []
    original = schema_module._connect

    def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
        assert read_only
        connection = original(path, read_only=True)
        connection.set_trace_callback(statements.append)
        return connection

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Mutation or process execution was attempted")

    monkeypatch.setattr(module, "_connect", connect)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "chmod", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    assert gate.validate(binding)["status"] == "validated"
    assert "pragma query_only=on" in statements
    assert all(
        not query.lower().startswith(("insert", "update", "delete", "create", "alter", "drop"))
        for query in statements
    )


@pytest.mark.parametrize("name", ["config.yaml", "layout.json", "state/operational.db", "global"])
def test_missing_required_inputs_are_not_created(environment: Fixture, name: str) -> None:
    gate, binding = environment
    target = gate.home / name
    target.rename(target.with_name(target.name + ".saved"))
    before = _tree(gate.home)
    with pytest.raises(ConfigurationError):
        gate.validate(binding)
    assert _tree(gate.home) == before
    assert not target.exists()


@pytest.mark.parametrize("field", ["home", "core_root", "task_path", "operational_path"])
def test_wrong_explicit_paths_rejected(environment: Fixture, tmp_path: Path, field: str) -> None:
    gate, binding = environment
    candidate = replace(gate, **{field: tmp_path / "missing"})
    with pytest.raises(ConfigurationError):
        candidate.validate(binding)
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("name", ["config.yaml", "layout.json", "state/operational.db", "global"])
def test_symlink_inputs_rejected_without_following(environment: Fixture, name: str) -> None:
    gate, binding = environment
    target = gate.home / name
    saved = target.with_name(target.name + ".saved")
    target.rename(saved)
    target.symlink_to(saved, target_is_directory=saved.is_dir())
    before = _tree(gate.home)
    with pytest.raises(ConfigurationError):
        gate.validate(binding)
    assert _tree(gate.home) == before


def test_home_alias_rejected(environment: Fixture, tmp_path: Path) -> None:
    gate, binding = environment
    alias = tmp_path / "alias"
    alias.symlink_to(gate.home, target_is_directory=True)
    with pytest.raises(ConfigurationError):
        replace(gate, home=alias, operational_path=alias / "state/operational.db").validate(binding)


def test_core_home_overlap_rejected(environment: Fixture) -> None:
    gate, binding = environment
    with pytest.raises(ConfigurationError):
        replace(gate, home=gate.core_root).validate(binding)


@pytest.mark.parametrize("field", ["task_digest", "policy_digest"])
def test_binding_task_policy_drift_rejected(environment: Fixture, field: str) -> None:
    gate, binding = environment
    with pytest.raises(ConfigurationError, match="digest drift"):
        gate.validate(replace(binding, **{field: digest("other")}))


@pytest.mark.parametrize(
    "payload",
    [
        "schema: zekam-config/v1\nschema: zekam-config/v1\n",
        "schema: zekam-config/v1\nruntime: {log_level: INFO, log_level: DEBUG}\n",
        "schema: zekam-config/v1\nruntime: &x {log_level: INFO}\nx: *x\n",
        "schema: zekam-config/v1\nx: &x [*x]\n",
        "schema: zekam-config/v1\n1: value\n",
        "schema: zekam-config/v1\n? [bad, key]\n: value\n",
        "schema: zekam-config/v1\nx: !!python/object:danger {}\n",
        "schema: zekam-config/v1\nx: .nan\n",
        "schema: zekam-config/v1\nx: 2026-09-02\n",
        "schema: zekam-config/v1\n---\nschema: zekam-config/v1\n",
        "schema: zekam-config/v1\ndatabase: []\n",
        "schema: zekam-config/v1\ndatabase: {port: true}\n",
        "schema: zekam-config/v1\nknowledge: {embedding_dimension: '1024'}\n",
        "schema: zekam-config/v1\nruntime: {log_level: [INFO]}\n",
        "schema: zekam-config/v1\nknowledge: {embedding_model_ref: false}\n",
        "schema: zekam-config/v1\nstorage: {object_store_relative: []}\n",
        "schema: zekam-config/v1\ndatabase: {required_extensions: vector}\n",
        "schema: zekam-config/v1\ndatabase: {required_extensions: [1]}\n",
        "schema: zekam-config/v1\ndiagnostic_trace: {enabled: 'false'}\n",
        "schema: zekam-config/v1\ndiagnostic_trace: {encryption_key_ref: []}\n",
        "schema: zekam-config/v1\nclients: {}\n",
        "schema: unsupported\n",
        "{}",
        "[]",
        "null",
        "",
        "x: [" + "[" * 20 + "0" + "]" * 21,
        "x: [" + ",".join("0" for _ in range(5000)) + "]",
        "#" * (MAX_CONFIG_BYTES + 1),
    ],
)
def test_bounded_strict_config_rejections(environment: Fixture, payload: str) -> None:
    gate, binding = environment
    (gate.home / "config.yaml").write_text(payload, encoding="utf-8")
    before = _tree(gate.home)
    with pytest.raises(ConfigurationError):
        gate.validate(binding)
    assert _tree(gate.home) == before


@pytest.mark.parametrize(
    "database",
    [
        {"backend": "postgresql"},
        {"backend": "sqlite", "host": "localhost"},
        {"port": 5433},
        {"name": "legacy"},
        {"user": "legacy"},
        {"sslmode": "require"},
        {"sqlite_relative_path": "state/other.db"},
        {"sqlite_relative_path": "../outside.db"},
    ],
)
def test_legacy_and_alternate_database_config_rejected(
    environment: Fixture, database: dict[str, Any]
) -> None:
    gate, binding = environment
    (gate.home / "config.yaml").write_text(
        yaml.safe_dump({"schema": "zekam-config/v1", "database": database})
    )
    with pytest.raises(ConfigurationError):
        gate.validate(binding)
    assert not (gate.home / "state/other.db").exists()


@pytest.mark.parametrize("key", module._ENV_KEYS[:-1])
def test_database_environment_overrides_rejected(
    environment: Fixture, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    gate, binding = environment
    monkeypatch.setenv(key, "forbidden-sensitive-value")
    with pytest.raises(ConfigurationError) as error:
        gate.validate(binding)
    assert "forbidden-sensitive-value" not in str(error.value)


def test_actual_environment_provenance_must_be_admitted(
    environment: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, binding = environment
    monkeypatch.setenv("ZEKAM_LOG_LEVEL", "DEBUG")
    with pytest.raises(ConfigurationError, match="actual settings digest drift"):
        gate.validate(binding)
    settings = load_settings(home=gate.home, environ={"ZEKAM_LOG_LEVEL": "DEBUG"})
    with SQLiteOperationalStore(gate.operational_path).unit_of_work() as uow:
        admitted = uow.activate_config(
            config_digest=digest(settings.sanitized()),
            task_digest=binding.task_digest,
            sanitized_config=settings.sanitized(),
        )
        uow.commit()
    result = gate.validate(replace(binding, policy_digest=admitted.config_digest))
    assert result["config_revision_id"] == admitted.id


def test_secret_input_and_key_are_not_exposed_in_error(environment: Fixture) -> None:
    gate, binding = environment
    (gate.home / "config.yaml").write_text(
        "schema: zekam-config/v1\nprivate-label-sensitive:\n  token: secret-value-sensitive\n"
    )
    with pytest.raises(ConfigurationError) as error:
        gate.validate(binding)
    assert "sensitive" not in str(error.value)
    assert str(gate.home) not in str(error.value)


@pytest.mark.parametrize("mode", ["legacy", "duplicate", "wrong-entry", "malformed", "wrong-type"])
def test_layout_must_be_exact_unambiguous_v2(environment: Fixture, mode: str) -> None:
    gate, binding = environment
    path = gate.home / "layout.json"
    value = json.loads(path.read_text())
    if mode == "legacy":
        value["schema"] = "zekam-home-layout/v1"
    elif mode == "wrong-entry":
        value["entries"][0]["ownership"] = "secret"
    text = json.dumps(value)
    if mode == "duplicate":
        text = text[:-1] + ',"schema":"zekam-home-layout/v2"}'
    elif mode == "malformed":
        text = "{"
    elif mode == "wrong-type":
        text = "[]"
    path.write_text(text)
    with pytest.raises(ConfigurationError):
        gate.validate(binding)


@pytest.mark.parametrize("field", ["active", "task_digest", "config_digest", "sanitized_json"])
def test_admitted_config_is_mandatory_and_exact(environment: Fixture, field: str) -> None:
    gate, binding = environment
    value: Any = 0 if field == "active" else "{}" if field == "sanitized_json" else digest("other")
    with sqlite3.connect(gate.operational_path) as connection:
        connection.execute(f"update config_revision set {field}=?", (value,))
    with pytest.raises(ConfigurationError):
        gate.validate(binding)


def test_duplicate_json_in_active_config_rejected(environment: Fixture) -> None:
    gate, binding = environment
    with sqlite3.connect(gate.operational_path) as connection:
        body = connection.execute("select sanitized_json from config_revision").fetchone()[0]
        body = body[:-1] + ',"home":' + json.dumps(str(gate.home)) + "}"
        connection.execute("update config_revision set sanitized_json=?", (body,))
    with pytest.raises(ConfigurationError, match="duplicate"):
        gate.validate(binding)


@pytest.mark.parametrize(
    "body",
    ["{", "[]", '{"value": NaN}', '{"value": Infinity}', '"text"', " " * (MAX_CONFIG_BYTES + 1)],
)
def test_active_config_json_is_bounded_and_strict(environment: Fixture, body: str) -> None:
    gate, binding = environment
    with sqlite3.connect(gate.operational_path) as connection:
        connection.execute("update config_revision set sanitized_json=?", (body,))
    before = _logical_database(gate.operational_path)
    with pytest.raises(ConfigurationError):
        gate.validate(binding)
    assert _logical_database(gate.operational_path) == before


def test_fifo_input_rejected_without_opening(environment: Fixture) -> None:
    gate, binding = environment
    path = gate.home / "config.yaml"
    path.rename(path.with_suffix(".saved"))
    os.mkfifo(path)
    with pytest.raises(ConfigurationError):
        gate.validate(binding)


def test_entire_gate_uses_captured_documents_not_unbounded_path_reads(
    environment: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, binding = environment

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Unbounded Path read was attempted")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    assert gate.validate(binding)["status"] == "validated"


def test_environment_change_during_snapshot_rejected(
    environment: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, binding = environment
    original = schema_module._validate_connection

    def validate(connection: sqlite3.Connection) -> int:
        result = original(connection)
        monkeypatch.setenv("ZEKAM_LOG_LEVEL", "DEBUG")
        return result

    monkeypatch.setattr(module, "_validate_connection", validate)
    with pytest.raises(ConfigurationError, match="environment changed"):
        gate.validate(binding)


@pytest.mark.parametrize("version", [1, 2])
def test_historical_schema_requires_explicit_upgrade(environment: Fixture, version: int) -> None:
    gate, binding = environment
    gate.operational_path.rename(gate.operational_path.with_suffix(".saved"))
    bootstrap(gate.operational_path, target_version=version)
    before = _tree(gate.home)
    with pytest.raises(ConfigurationError, match="current operational schema"):
        gate.validate(binding)
    assert _tree(gate.home) == before


@pytest.mark.parametrize("mode", ["corrupt", "unknown", "drift"])
def test_corrupt_unknown_and_drifted_databases_fail(environment: Fixture, mode: str) -> None:
    gate, binding = environment
    if mode == "corrupt":
        gate.operational_path.write_bytes(b"not a database")
    else:
        with sqlite3.connect(gate.operational_path) as connection:
            if mode == "unknown":
                connection.execute("update zekam_meta set value='999' where key='schema_version'")
            else:
                connection.execute("create table unexpected (id text)")
    before = _tree(gate.home)
    with pytest.raises(ConfigurationError):
        gate.validate(binding)
    assert _tree(gate.home) == before


def test_live_wal_consistent_snapshot_without_logical_writes(environment: Fixture) -> None:
    gate, binding = environment
    with sqlite3.connect(gate.operational_path) as writer:
        writer.execute("pragma journal_mode=wal")
        writer.execute("pragma wal_autocheckpoint=0")
        writer.execute("update config_revision set active=1")
        writer.commit()
        before = _logical_database(gate.operational_path)
        wal = Path(str(gate.operational_path) + "-wal")
        assert wal.stat().st_size > 0
        wal_before = wal.read_bytes()
        assert gate.validate(binding)["status"] == "validated"
        assert _logical_database(gate.operational_path) == before
        assert wal.read_bytes() == wal_before


def test_concurrent_writer_snapshot_then_revalidation_rejects_stale_admission(
    environment: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, binding = environment
    original = schema_module._validate_connection
    with sqlite3.connect(gate.operational_path) as writer:
        writer.execute("pragma journal_mode=wal")
        writer.execute("pragma wal_autocheckpoint=0")
        fired = False

        def validate(connection: sqlite3.Connection) -> int:
            nonlocal fired
            result = original(connection)
            if not fired:
                writer.execute("update config_revision set active=0")
                writer.commit()
                fired = True
            return result

        monkeypatch.setattr(module, "_validate_connection", validate)
        assert gate.validate(binding)["authority_snapshot_only"] is True
        with pytest.raises(ConfigurationError, match="admitted active config"):
            gate.validate(binding)


def test_source_config_changes_during_db_read_are_rejected(
    environment: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, binding = environment
    original = schema_module._validate_connection

    def validate(connection: sqlite3.Connection) -> int:
        result = original(connection)
        path = gate.home / "config.yaml"
        path.write_bytes(path.read_bytes() + b"# concurrent change\n")
        return result

    monkeypatch.setattr(module, "_validate_connection", validate)
    with pytest.raises(ConfigurationError, match="source document changed"):
        gate.validate(binding)


def test_captured_config_loader_never_reopens_files_and_preserves_digest(
    environment: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, _binding = environment
    expected = load_settings(home=gate.home, environ={}).sanitized()
    documents = {
        path: module._document(path.read_bytes())
        for path in (default_config_file(), gate.home / "config.yaml")
    }

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Injected config loader reopened filesystem input")

    monkeypatch.setattr(Path, "read_text", forbidden)
    actual = load_settings(
        home=gate.home, environ={}, document_loader=documents.__getitem__
    ).sanitized()
    assert actual == expected
    assert digest(actual) == digest(expected)


@pytest.mark.parametrize("document", [{"schema": "bad"}, {"secret": "not-allowed"}, []])
def test_injected_config_cannot_bypass_schema_and_secret_validation(
    environment: Fixture, document: Any
) -> None:
    gate, _binding = environment
    with pytest.raises(ConfigurationError):
        load_settings(home=gate.home, environ={}, document_loader=lambda _: document)


def test_task_from_bytes_is_exact_existing_parser(environment: Fixture) -> None:
    gate, _binding = environment
    payload = gate.task_path.read_bytes()
    assert ActiveTaskContract.from_bytes(payload) == ActiveTaskContract.load(gate.task_path)
    with pytest.raises(ValidationFailed):
        ActiveTaskContract.from_bytes(payload.decode())  # type: ignore[arg-type]


@pytest.mark.parametrize("kind", ["task", "projection", "default"])
def test_bounded_captured_authority_inputs_reject_drift_without_project_writes(
    environment: Fixture, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    gate, binding = environment
    original = module._capture
    target = {
        "task": gate.task_path,
        "projection": gate.task_path.with_suffix(".yaml"),
        "default": default_config_file(),
    }[kind]

    def capture(path: Path, maximum: int) -> tuple[tuple[int, ...], bytes]:
        identity, payload = original(path, maximum)
        if path == target:
            if kind == "task":
                payload += b"\nmodified task\n"
            elif kind == "projection":
                payload += b"grants_authority: false\n"
            else:
                payload += b"schema: zekam-config/v1\n"
        return identity, payload

    monkeypatch.setattr(module, "_capture", capture)
    with pytest.raises(ConfigurationError):
        gate.validate(binding)


def test_wrong_binding_type_rejected(environment: Fixture) -> None:
    gate, _binding = environment
    with pytest.raises(ValidationFailed):
        gate.validate(None)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["home", "core_root", "task_path", "operational_path"])
@pytest.mark.parametrize("value", [None, "/not/a/typed/path", 123])
def test_wrong_path_type_rejected(environment: Fixture, field: str, value: Any) -> None:
    gate, binding = environment
    with pytest.raises(ValidationFailed):
        replace(gate, **{field: value}).validate(binding)


def test_all_layout_directories_are_validated(environment: Fixture) -> None:
    gate, binding = environment
    target = gate.home / HOME_ENTRIES[-1].relative
    target.rmdir()
    target.write_text("not a directory")
    with pytest.raises(ConfigurationError):
        gate.validate(binding)
