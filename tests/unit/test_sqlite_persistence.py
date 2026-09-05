"""SQLite minimum persistence ve ilk kurulum secimi testleri."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from zekam.application.config import CONFIG_SCHEMA, PersistenceBackend, load_settings
from zekam.application.persistence_setup import (
    PersistenceSetupPlan,
    plan_persistence_setup,
)
from zekam.application.persistence_setup import (
    apply_persistence_setup as _apply_persistence_setup,
)
from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.infrastructure.sqlite.repository import SQLitePersistence, bootstrap, status

pytestmark = pytest.mark.unit


def apply_persistence_setup(plan: PersistenceSetupPlan) -> None:
    _apply_persistence_setup(plan, bootstrap=bootstrap)


def test_sqlite_selection_bootstraps_real_minimum_repository(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = plan_persistence_setup(home=home, requested=PersistenceBackend.SQLITE)

    apply_persistence_setup(plan)

    settings = load_settings(home=home, environ={})
    assert settings.database.backend is PersistenceBackend.SQLITE
    database_path = settings.database.sqlite_path(home)
    assert status(database_path).integrity_ok

    repository = SQLitePersistence(database_path)
    project = repository.create_project(
        slug="gpu-fusion", display_name="GPU Fusion", source_ref="source:gpu-fusion"
    )
    work = repository.create_work(
        project_id=project.id, kind="task", title="SQLite minimum profile"
    )
    first = repository.index_chunk(
        project_id=project.id,
        source_ref="source:gpu-fusion/src/a.py",
        body="alpha",
        metadata={"line": 1},
        model_ref="local/test",
        vector=(1.0, 0.0),
    )
    repository.index_chunk(
        project_id=project.id,
        source_ref="source:gpu-fusion/src/b.py",
        body="beta",
        metadata={"line": 2},
        model_ref="local/test",
        vector=(0.0, 1.0),
    )

    assert repository.list_projects() == (project,)
    assert repository.list_work(project_id=project.id) == (work,)
    hits = repository.search(project_id=project.id, model_ref="local/test", query_vector=(1.0, 0.0))
    assert hits[0].chunk_id == first
    assert hits[0].score == pytest.approx(1.0)
    assert hits[1].score == pytest.approx(0.0)


def test_persistence_selection_is_one_shot_and_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    first = plan_persistence_setup(home=home, requested=PersistenceBackend.SQLITE)
    apply_persistence_setup(first)

    replay = plan_persistence_setup(home=home, requested=PersistenceBackend.SQLITE)
    assert replay.config_exists
    assert not replay.write_config
    apply_persistence_setup(replay)

    assert tuple(PersistenceBackend) == (PersistenceBackend.SQLITE,)


def test_sqlite_project_replay_payload_drift_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = plan_persistence_setup(home=home, requested=PersistenceBackend.SQLITE)
    apply_persistence_setup(plan)
    repository = SQLitePersistence(plan.sqlite_path or Path("missing"))
    repository.create_project(slug="demo", display_name="Demo")

    with pytest.raises(ValidationFailed, match="payload drift"):
        repository.create_project(slug="demo", display_name="Changed")


def test_same_sqlite_setup_plan_is_concurrently_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = plan_persistence_setup(home=home, requested=PersistenceBackend.SQLITE)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(apply_persistence_setup, plan) for _ in range(2)]
        for future in futures:
            future.result()

    assert status(plan.sqlite_path or Path("missing")).integrity_ok
    assert load_settings(home=home, environ={}).database.backend is PersistenceBackend.SQLITE


def test_sqlite_completed_work_requires_evidence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = plan_persistence_setup(home=home, requested=PersistenceBackend.SQLITE)
    apply_persistence_setup(plan)
    repository = SQLitePersistence(plan.sqlite_path or Path("missing"))
    project = repository.create_project(slug="demo", display_name="Demo")

    with pytest.raises(ValidationFailed, match="canonical evidence digest"):
        repository.create_work(
            project_id=project.id, kind="task", title="Kanitsiz", state="completed"
        )


def test_sqlite_vector_search_isolated_by_model_ref(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = plan_persistence_setup(home=home, requested=PersistenceBackend.SQLITE)
    apply_persistence_setup(plan)
    repository = SQLitePersistence(plan.sqlite_path or Path("missing"))
    project = repository.create_project(slug="demo", display_name="Demo")
    expected = repository.index_chunk(
        project_id=project.id,
        source_ref="source:demo/a.py",
        body="expected",
        metadata={},
        model_ref="local/a",
        vector=(1.0, 0.0),
    )
    repository.index_chunk(
        project_id=project.id,
        source_ref="source:demo/b.py",
        body="other-model",
        metadata={},
        model_ref="local/b",
        vector=(1.0, 0.0),
    )

    hits = repository.search(project_id=project.id, model_ref="local/a", query_vector=(1.0, 0.0))

    assert [hit.chunk_id for hit in hits] == [expected]


def test_sqlite_vector_validation_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = plan_persistence_setup(home=home, requested=PersistenceBackend.SQLITE)
    apply_persistence_setup(plan)
    repository = SQLitePersistence(plan.sqlite_path or Path("missing"))
    project = repository.create_project(slug="demo", display_name="Demo")

    with pytest.raises(ValidationFailed, match="Sifir embedding"):
        repository.index_chunk(
            project_id=project.id,
            source_ref="source:demo/a.py",
            body="body",
            metadata={},
            model_ref="local/test",
            vector=(0.0, 0.0),
        )


def test_custom_sqlite_path_is_bootstrapped_and_reported(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        f"schema: {CONFIG_SCHEMA}\ndatabase:\n  backend: sqlite\n"
        "  sqlite_relative_path: private/data.sqlite3\n",
        encoding="utf-8",
    )

    plan = plan_persistence_setup(home=home, requested=None)
    apply_persistence_setup(plan)

    assert plan.sqlite_path == (home / "private" / "data.sqlite3").resolve()
    assert status(plan.sqlite_path).schema_ok
    assert not (home / "state" / "operational.db").exists()


def test_sqlite_bootstrap_failure_does_not_publish_selection(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = plan_persistence_setup(home=home, requested=PersistenceBackend.SQLITE)

    def fail_bootstrap(path: Path) -> object:
        del path
        raise ConfigurationError("bootstrap failed")

    with pytest.raises(ConfigurationError, match="bootstrap failed"):
        _apply_persistence_setup(plan, bootstrap=fail_bootstrap)

    assert not (home / "config.yaml").exists()


def test_legacy_config_can_receive_explicit_one_shot_sqlite_selection(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(f"schema: {CONFIG_SCHEMA}\nruntime:\n  log_level: INFO\n")

    plan = plan_persistence_setup(home=home, requested=PersistenceBackend.SQLITE)
    assert plan.legacy_config and plan.write_config
    apply_persistence_setup(plan)

    assert load_settings(home=home, environ={}).database.backend is PersistenceBackend.SQLITE
    assert tuple(PersistenceBackend) == (PersistenceBackend.SQLITE,)


def test_sqlite_schema_manifest_drift_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = plan_persistence_setup(home=home, requested=PersistenceBackend.SQLITE)
    apply_persistence_setup(plan)
    database_path = plan.sqlite_path or Path("missing")
    with sqlite3.connect(database_path) as connection:
        connection.execute("drop table knowledge_embedding")

    current = status(database_path)
    assert current.integrity_ok
    assert not current.schema_ok
    with pytest.raises(ConfigurationError, match="bootstrap veya migration"):
        SQLitePersistence(database_path)


def test_sqlite_unexpected_trigger_is_schema_drift(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = plan_persistence_setup(home=home, requested=PersistenceBackend.SQLITE)
    apply_persistence_setup(plan)
    database_path = plan.sqlite_path or Path("missing")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "create trigger unexpected_project_delete after insert on project "
            "begin delete from project where id = new.id; end"
        )

    current = status(database_path)
    assert current.integrity_ok
    assert not current.schema_ok


def test_sqlite_non_integer_schema_version_is_structured_drift(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = plan_persistence_setup(home=home, requested=PersistenceBackend.SQLITE)
    apply_persistence_setup(plan)
    database_path = plan.sqlite_path or Path("missing")
    with sqlite3.connect(database_path) as connection:
        connection.execute("update zekam_meta set value = 'invalid' where key = 'schema_version'")

    current = status(database_path)
    assert current.integrity_ok
    assert current.schema_version is None


def test_sqlite_migration_ledger_drift_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = plan_persistence_setup(home=home, requested=PersistenceBackend.SQLITE)
    apply_persistence_setup(plan)
    database_path = plan.sqlite_path or Path("missing")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "update schema_migration set checksum = ? where version = 1",
            ("sha256:" + "0" * 64,),
        )

    current = status(database_path)
    assert current.integrity_ok
    assert not current.schema_ok
    with pytest.raises(ConfigurationError, match="migration ledger drift"):
        bootstrap(database_path)


def test_completed_work_rejects_noncanonical_evidence_digest(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = plan_persistence_setup(home=home, requested=PersistenceBackend.SQLITE)
    apply_persistence_setup(plan)
    repository = SQLitePersistence(plan.sqlite_path or Path("missing"))
    project = repository.create_project(slug="demo", display_name="Demo")

    with pytest.raises(ValidationFailed, match="sha256"):
        repository.create_work(
            project_id=project.id,
            kind="task",
            title="Gecersiz kanit",
            state="completed",
            evidence_digest="arbitrary",
        )
