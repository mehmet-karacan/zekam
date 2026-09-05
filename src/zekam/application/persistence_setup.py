"""Ilk kurulumda tek seferlik persistence secimi ve bootstrap plani."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from zekam.application.config import (
    CONFIG_SCHEMA,
    USER_CONFIG_FILE,
    PersistenceBackend,
    load_settings,
    resolve_sqlite_path,
)
from zekam.domain.errors import ConfigurationError


class DatabaseBootstrap(Protocol):
    def __call__(self, path: Path) -> object: ...


@dataclass(frozen=True, slots=True)
class PersistenceSetupPlan:
    home: Path
    backend: PersistenceBackend
    config_exists: bool
    write_config: bool
    sqlite_relative_path: str = "state/operational.db"
    legacy_config: bool = False

    @property
    def sqlite_path(self) -> Path | None:
        return resolve_sqlite_path(self.home, self.sqlite_relative_path)


def _user_document(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError("Kullanici config dosyasi okunamadi") from exc
    if not isinstance(loaded, dict):
        raise ConfigurationError("Kullanici config kok nesnesi mapping olmali")
    return dict(loaded)


def plan_persistence_setup(
    *, home: Path, requested: PersistenceBackend | None
) -> PersistenceSetupPlan:
    """Secimi mutation olmadan cozer; mevcut secim degistirilemez."""
    config_path = home / USER_CONFIG_FILE
    if config_path.exists():
        document = _user_document(config_path)
        database_document = document.get("database")
        database_mapping = database_document if isinstance(database_document, dict) else {}
        explicit_backend = "backend" in database_mapping
        selected = load_settings(home=home, environ={}).database.backend
        sqlite_relative_path = load_settings(home=home, environ={}).database.sqlite_relative_path
        if not explicit_backend and requested is not None:
            selected = requested
        if requested is not None and requested is not selected:
            raise ConfigurationError(
                f"Persistence daha once {selected.value} olarak secildi; "
                "sessiz motor degisimi yasak"
            )
        return PersistenceSetupPlan(
            home,
            selected,
            config_exists=True,
            write_config=not explicit_backend and requested is not None,
            sqlite_relative_path=sqlite_relative_path,
            legacy_config=not explicit_backend,
        )
    return PersistenceSetupPlan(
        home,
        requested or PersistenceBackend.SQLITE,
        config_exists=False,
        write_config=True,
    )


def apply_persistence_setup(plan: PersistenceSetupPlan, *, bootstrap: DatabaseBootstrap) -> None:
    """Yeni secimi secret-free config'e yazar ve secilen motoru gercekten kurar."""
    plan.home.mkdir(parents=True, exist_ok=True)
    config_path = plan.home / USER_CONFIG_FILE
    if plan.backend is PersistenceBackend.SQLITE:
        sqlite_path = plan.sqlite_path
        assert sqlite_path is not None
        bootstrap(sqlite_path)
    if plan.write_config:
        if plan.legacy_config:
            _publish_legacy_selection(plan, config_path)
        else:
            document = (
                f"schema: {CONFIG_SCHEMA}\n"
                "database:\n"
                f"  backend: {plan.backend.value}\n"
                f"  sqlite_relative_path: {plan.sqlite_relative_path}\n"
            )
            _publish_new_config(plan, config_path, document)


def _write_staged(home: Path, document: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=home)
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _publish_new_config(plan: PersistenceSetupPlan, config_path: Path, document: str) -> None:
    staged = _write_staged(plan.home, document)
    try:
        try:
            os.link(staged, config_path)
        except FileExistsError:
            observed = plan_persistence_setup(home=plan.home, requested=plan.backend)
            if observed.backend is not plan.backend:
                raise ConfigurationError(
                    "Persistence secimi concurrent drift nedeniyle reddedildi"
                ) from None
    finally:
        staged.unlink(missing_ok=True)


def _publish_legacy_selection(plan: PersistenceSetupPlan, config_path: Path) -> None:
    lock_path = plan.home / ".persistence-selection.lock"
    try:
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ConfigurationError("Persistence secimi baska bir islem tarafindan kilitli") from None
    os.close(descriptor)
    staged: Path | None = None
    try:
        document = _user_document(config_path)
        database = document.setdefault("database", {})
        if not isinstance(database, dict):
            raise ConfigurationError("Legacy database config mapping olmali")
        existing = database.get("backend")
        if existing is not None and str(existing).lower() != plan.backend.value:
            raise ConfigurationError("Persistence secimi concurrent drift nedeniyle reddedildi")
        database["backend"] = plan.backend.value
        database.setdefault("sqlite_relative_path", plan.sqlite_relative_path)
        rendered = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        staged = _write_staged(plan.home, rendered)
        os.replace(staged, config_path)
        staged = None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)
