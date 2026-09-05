"""Uygulama composition koku.

CLI, API, scheduler ve worker ayni context ve ayni servisleri kullanir. Yuzeyler
kendi urun kurallarini tanimlamaz.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from zekam.application.config import Settings, core_root, load_settings
from zekam.application.diagnostics import DoctorCheck, DoctorService
from zekam.application.environment import environment_value
from zekam.application.home import HomeLayout, resolve_home


@dataclass(frozen=True, slots=True)
class ApplicationContext:
    """Butun yuzeylerin paylastigi cozulmus baglam."""

    settings: Settings
    layout: HomeLayout
    core_path: Path

    @property
    def home(self) -> Path:
        return self.layout.root


def build_context(
    *,
    home: str | os.PathLike[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ApplicationContext:
    """Yapilandirmayi ve ZEKAM_HOME yerlesimini cozer. Disk uzerinde degisiklik yapmaz."""
    environ = os.environ if environ is None else environ
    resolved_home = resolve_home(
        home if home is not None else environment_value(environ, "ZEKAM_HOME")
    )
    settings = load_settings(home=resolved_home, environ=environ)
    return ApplicationContext(
        settings=settings,
        layout=HomeLayout(resolved_home),
        core_path=core_root(),
    )


def build_doctor_checks(context: ApplicationContext) -> tuple[DoctorCheck, ...]:
    """Kayitli doctor kontrollerini olusturur.

    Yeni fazlar bu listeye kendi kontrollerini ekler. Uygulanmamis bir yetenek icin
    burada kontrol tanimlanmaz; boylece rapor sahte `passed` uretmez.
    """
    from zekam.infrastructure.doctor import (
        core_checks,
        runtime_checks,
        sqlite_checks,
        storage_checks,
    )

    checks: list[DoctorCheck] = [
        core_checks.VersionCheck(),
        core_checks.PythonRuntimeCheck(),
        core_checks.ConfigCheck(settings=context.settings),
        core_checks.HomeLayoutCheck(layout=context.layout, core_path=context.core_path),
        core_checks.GitClientCheck(),
        core_checks.GitRepositoryCheck(root=context.core_path),
    ]
    checks.extend(
        (
            sqlite_checks.PersistenceCheck(
                path=context.settings.database.sqlite_path(context.home),
                path_ref=f"ZEKAM_HOME/{context.settings.database.sqlite_relative_path}",
            ),
            sqlite_checks.LocalCoreStoresCheck(
                context=context,
            ),
            sqlite_checks.CapabilityCheck(),
        )
    )
    checks.append(
        storage_checks.ObjectStoreCheck(root=context.home / context.settings.object_store_relative),
    )
    checks.extend(
        (
            runtime_checks.ClientsCheck(executables=_client_executables(context)),
            runtime_checks.OpenCodeSpoolCheck(home=context.home),
            runtime_checks.CommandSurfaceCheck(),
        )
    )
    return tuple(checks)


def _client_executables(context: ApplicationContext) -> tuple[tuple[str, str], ...]:
    """Yapilandirilmis istemci calistirilabilir dosyalari.

    Yapilandirma yoksa bos doner ve ilgili kontrol `skipped` olur; var olmayan
    bir yetenek icin sahte `passed` uretilmez.
    """

    return tuple((client.name, str(client.executable)) for client in context.settings.clients)


def build_doctor(context: ApplicationContext) -> DoctorService:
    """Doctor servisini olusturur."""
    return DoctorService(build_doctor_checks(context))
