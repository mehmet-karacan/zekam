"""Forward-only PostgreSQL migration altyapisi.

Sozlesme:

- Migration dosyalari `migrations/NNNN_ad.sql` bicimindedir ve numaralar bosluksuz artar.
- Uygulanan her migration'in sha256'si `core.schema_migrations` icinde saklanir.
- Uygulanmis bir dosyanin icerigi degisirse bu **drift**'tir; sessizce yeniden
  uygulanmaz, acik hata verir.
- Ayni anda yalnizca bir surec migration uygulayabilir (advisory lock).
- Her migration tek transaction icinde calisir; basarisiz olan migration yarim kalmaz.
- Geri alma dosyasi `migrations/NNNN_ad.down.sql` opsiyoneldir ve otomatik calismaz;
  yalnizca exact authorization ile uygulanir.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from zekam.domain.errors import ConfigurationError, ValidationFailed

#: Migration dosya adi bicimi.
MIGRATION_PATTERN = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")

#: Ayni anda tek migration surecini garanti eden advisory lock anahtari.
MIGRATION_LOCK_KEY = 0x5A45_4B41_4D  # "ZEKAM"

BOOTSTRAP_SQL = """
create schema if not exists core;

create table if not exists core.schema_migrations (
    version      integer      primary key,
    name         text         not null,
    checksum     text         not null,
    applied_at   timestamptz  not null default now(),
    applied_by   text         not null default current_user,
    duration_ms  integer      not null
);

comment on table core.schema_migrations is
    'Uygulanan migration ledgeri. Forward-only; satirlar guncellenmez.';
"""


class DriftKind(StrEnum):
    """Migration durumu ile dosya sistemi arasindaki uyusmazlik turu."""

    CHECKSUM_MISMATCH = "checksum-mismatch"
    MISSING_FILE = "missing-file"
    OUT_OF_ORDER = "out-of-order"


@dataclass(frozen=True, slots=True)
class Migration:
    """Dosya sistemindeki tek bir migration."""

    version: int
    name: str
    path: Path
    checksum: str

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"

    @property
    def down_path(self) -> Path:
        return self.path.with_name(f"{self.version:04d}_{self.name}.down.sql")

    @property
    def has_down(self) -> bool:
        return self.down_path.is_file()

    def read_sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    """Veritabaninda kayitli migration."""

    version: int
    name: str
    checksum: str


@dataclass(frozen=True, slots=True)
class DriftFinding:
    """Tespit edilen tek bir drift."""

    kind: DriftKind
    version: int
    detail: str


@dataclass(frozen=True, slots=True)
class MigrationStatus:
    """Migration durumunun tam gorunumu."""

    head: int | None
    applied: tuple[AppliedMigration, ...]
    pending: tuple[Migration, ...]
    drift: tuple[DriftFinding, ...]

    @property
    def is_current(self) -> bool:
        return not self.pending and not self.drift

    def as_dict(self) -> dict[str, Any]:
        return {
            "head": self.head,
            "applied_count": len(self.applied),
            "pending": [migration.label for migration in self.pending],
            "drift": [
                {"kind": finding.kind.value, "version": finding.version, "detail": finding.detail}
                for finding in self.drift
            ],
            "is_current": self.is_current,
        }


def default_migrations_dir() -> Path:
    """Core dagitimindaki migration dizini."""
    from zekam.application.config import core_root

    return core_root() / "migrations"


def checksum_of(sql: str) -> str:
    """Migration icerigi icin kararli sha256 uretir.

    Satir sonu bicimi ve sondaki bosluk normalize edilir; boylece Windows/Linux
    checkout'lari ayni digest'i uretir.
    """
    normalized = sql.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def discover_migrations(directory: Path | None = None) -> tuple[Migration, ...]:
    """Migration dosyalarini okur, dogrular ve siraya dizer."""
    base = directory or default_migrations_dir()
    if not base.is_dir():
        raise ConfigurationError("Migration dizini bulunamadi")

    found: dict[int, Migration] = {}
    for path in sorted(base.glob("*.sql")):
        if path.name.endswith(".down.sql"):
            continue
        match = MIGRATION_PATTERN.match(path.name)
        if match is None:
            raise ValidationFailed(f"Gecersiz migration dosya adi: {path.name}")
        version = int(match.group("version"))
        if version in found:
            raise ValidationFailed(f"Yinelenen migration numarasi: {version}")
        found[version] = Migration(
            version=version,
            name=match.group("name"),
            path=path,
            checksum=checksum_of(path.read_text(encoding="utf-8")),
        )

    ordered = tuple(found[version] for version in sorted(found))
    for index, migration in enumerate(ordered, start=1):
        if migration.version != index:
            raise ValidationFailed(
                f"Migration numaralari bosluksuz artmali; {index} bekleniyordu, "
                f"{migration.version} bulundu"
            )
    return ordered


def ensure_ledger(connection: Any) -> None:
    """Migration ledger tablosunu olusturur (idempotent)."""
    with connection.cursor() as cursor:
        cursor.execute(BOOTSTRAP_SQL)


def read_applied(connection: Any) -> tuple[AppliedMigration, ...]:
    """Uygulanmis migration kayitlarini okur."""
    with connection.cursor() as cursor:
        cursor.execute(
            "select version, name, checksum from core.schema_migrations order by version"
        )
        rows = cursor.fetchall()
    return tuple(AppliedMigration(version=row[0], name=row[1], checksum=row[2]) for row in rows)


def detect_drift(
    applied: Sequence[AppliedMigration], available: Sequence[Migration]
) -> tuple[DriftFinding, ...]:
    """Uygulanmis kayitlar ile dosyalar arasindaki farki hesaplar."""
    by_version = {migration.version: migration for migration in available}
    findings: list[DriftFinding] = []
    for record in applied:
        migration = by_version.get(record.version)
        if migration is None:
            findings.append(
                DriftFinding(
                    kind=DriftKind.MISSING_FILE,
                    version=record.version,
                    detail=f"{record.name} uygulanmis fakat dosyasi yok",
                )
            )
            continue
        if migration.checksum != record.checksum:
            findings.append(
                DriftFinding(
                    kind=DriftKind.CHECKSUM_MISMATCH,
                    version=record.version,
                    detail=f"{migration.label} icerigi uygulandiktan sonra degismis",
                )
            )
    if applied:
        highest_applied = max(record.version for record in applied)
        for migration in available:
            if migration.version < highest_applied and all(
                record.version != migration.version for record in applied
            ):
                findings.append(
                    DriftFinding(
                        kind=DriftKind.OUT_OF_ORDER,
                        version=migration.version,
                        detail=(
                            f"{migration.label} atlanmis; daha yuksek numarali migration "
                            "zaten uygulanmis"
                        ),
                    )
                )
    return tuple(findings)


def status(connection: Any, directory: Path | None = None) -> MigrationStatus:
    """Migration durumunu hesaplar. Hicbir sey uygulamaz."""
    available = discover_migrations(directory)
    applied = read_applied(connection)
    applied_versions = {record.version for record in applied}
    pending = tuple(
        migration for migration in available if migration.version not in applied_versions
    )
    head = max(applied_versions) if applied_versions else None
    return MigrationStatus(
        head=head,
        applied=applied,
        pending=pending,
        drift=detect_drift(applied, available),
    )


@dataclass(frozen=True, slots=True)
class AppliedResult:
    """Tek bir migration uygulamasinin sonucu."""

    version: int
    name: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class RolledBackResult:
    """Exact head migration geri alma sonucu."""

    version: int
    name: str
    duration_ms: int


def upgrade(
    connection: Any,
    directory: Path | None = None,
    *,
    target: int | None = None,
) -> tuple[AppliedResult, ...]:
    """Bekleyen migration'lari sirayla uygular.

    Drift varsa hicbir sey uygulanmaz. Her migration kendi transaction'i icinde
    calisir ve ledger ayni transaction'da yazilir.
    """
    ensure_ledger(connection)
    current = status(connection, directory)
    if current.drift:
        details = "; ".join(finding.detail for finding in current.drift)
        raise ConfigurationError(f"Migration drift tespit edildi: {details}")

    pending = [
        migration for migration in current.pending if target is None or migration.version <= target
    ]
    if not pending:
        return ()

    results: list[AppliedResult] = []
    previous_autocommit = connection.autocommit
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("select pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        try:
            connection.autocommit = False
            for migration in pending:
                started = time.monotonic()
                with connection.cursor() as cursor:
                    cursor.execute(migration.read_sql())
                    duration_ms = int((time.monotonic() - started) * 1000)
                    cursor.execute(
                        "insert into core.schema_migrations"
                        " (version, name, checksum, duration_ms) values (%s, %s, %s, %s)",
                        (migration.version, migration.name, migration.checksum, duration_ms),
                    )
                connection.commit()
                results.append(
                    AppliedResult(
                        version=migration.version, name=migration.name, duration_ms=duration_ms
                    )
                )
        finally:
            connection.rollback()
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("select pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
    finally:
        connection.autocommit = previous_autocommit
    return tuple(results)


def downgrade(
    connection: Any,
    *,
    target: int,
    directory: Path | None = None,
) -> RolledBackResult:
    """Yalniz mevcut head migration'ini tek transaction icinde geri alir.

    Application katmani bu islemi exact authorization arkasinda cagirmalidir.
    Infrastructure katmani out-of-order geri almayi reddeder, down SQL ile migration
    ledger kaydini ayni transaction'da uzlastirir ve advisory lock kullanir.
    """

    previous_autocommit = connection.autocommit
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("select pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
        try:
            current = status(connection, directory)
            if current.drift:
                details = "; ".join(finding.detail for finding in current.drift)
                raise ConfigurationError(f"Migration drift tespit edildi: {details}")
            if current.head != target:
                raise ValidationFailed("Yalniz mevcut migration head geri alinabilir")
            migration = next(
                (item for item in discover_migrations(directory) if item.version == target),
                None,
            )
            if migration is None or not migration.has_down:
                raise ConfigurationError("Exact migration geri alma dosyasi bulunamadi")
            applied = next(
                (item for item in current.applied if item.version == target),
                None,
            )
            if (
                applied is None
                or applied.name != migration.name
                or applied.checksum != migration.checksum
            ):
                raise ConfigurationError("Exact migration ledger binding drift")

            connection.autocommit = False
            started = time.monotonic()
            with connection.cursor() as cursor:
                cursor.execute(migration.down_path.read_text(encoding="utf-8"))
                cursor.execute(
                    "delete from core.schema_migrations where version = %s",
                    (migration.version,),
                )
                cursor.execute(
                    "select count(*) from core.schema_migrations where version = %s",
                    (migration.version,),
                )
                if int(cursor.fetchone()[0]) != 0:
                    raise ConfigurationError("Migration ledger geri alma uzlastirmasi basarisiz")
            connection.commit()
            return RolledBackResult(
                version=migration.version,
                name=migration.name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.autocommit = True
            with connection.cursor() as cursor:
                cursor.execute("select pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
    finally:
        connection.autocommit = previous_autocommit
