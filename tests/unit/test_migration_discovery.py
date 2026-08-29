"""Migration kesfi, checksum ve drift hesaplamasi (veritabani gerektirmez)."""

from __future__ import annotations

from pathlib import Path

import pytest

from zekam.domain.errors import ConfigurationError, ValidationFailed
from zekam.infrastructure.postgres.migrations import (
    AppliedMigration,
    DriftKind,
    checksum_of,
    default_migrations_dir,
    detect_drift,
    discover_migrations,
)

pytestmark = pytest.mark.unit

PRE_CONTRACTION_CHECKSUMS: tuple[str, ...] = (
    "1c82857f107c2df47cf03549a8b42a8d5fc9ad33f7dc2d19661c7763f63a09ec",
    "4df3cbba810c2046eed798aab13c97ac22e1b9fab39650547c14d2e5c6eedc63",
    "4d18c3bb6ef8a4a2c4fe2c1f29cf89fd79d23961c012e3719aa5f6b5ed3d7f9b",
    "60b6afe8f79d8dfd6d8e6af333b0d344dd12fd260a2ede66a6ec2fb2842eb0e0",
    "583e3243bc688b6989ebe37ef6cd256ceffd8e3f9fc9ecd31ac9f1f5ff9c2c9c",
    "65b5255d17feef798c6acfbc034b10355843acafb4fdacd856a06b611aa28ec4",
    "b1b8050d5703b1196fb1b1d37e4950891b5bd7db14e98a33f0e2d5678383c785",
    "9c2755e67f12f0d3ddbe1e6264b05a5399116685089145ccfab5470a93e194a9",
    "f3dac356c8960358f1df9689aa578f049cced1dc05709db01d1566039425ce36",
    "4d9e3deff6779874b6bce55e38fea4ae601f4a4620f033ee6755c32aa75bebc0",
    "227b7e4f2662bac061b43a0ea3f9dca25f245fcf773e093ea8e9573ea0698ce0",
    "549e8f329e8e305ff676e7c77c75bb57105a50a7a0ba30ec8962f91212feec63",
    "c46a0246ef401ee3069803184f44f62a772300de887da662edfd9b1eb25b984e",
    "f446c06eeb57acf612f0cdb1ff5a1c51595913b05115d778e4f5ef7d4734e40e",
    "e8fcc6e02a061d57979d32df8abaf9eec2a1d6a24fed50b87c31a7fe66d486f1",
    "d46eb17c191965a722a11303eba3bc8d0e55efbfbe9b0895ab52973b4f3daeb0",
    "98f4180d692b936f215609369af6ba35d112de67f1e602996750f79368a4dbe8",
    "d578989b4e8ed8c7e905552345c28659115e5696d9e0db27b838481394a4b1eb",
    "8c4ab3f9441af26ed7964fc620e1a187852d5d113df5e6493d63b330ed363ba9",
    "3dee84c269fd6c9cf3a9d3e62bcfb2f003c07774ee1712f26943b67a69f31a39",
    "ec8f3b19eb8f0765b6f281074ceecdaa84925d53f230803062ac2150b605ba9b",
    "ea67f9b7dbd3bdc1d4354777115da0d61572cc9540f8b38168148d6b32c7fa0b",
    "7bdcb0e97550a31154eca0ee11c0c95a7df220295ed3fc641e54e3d7015c7474",
)


def _write(directory: Path, name: str, body: str = "select 1;\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def test_shipped_migrations_are_discoverable_and_ordered() -> None:
    found = discover_migrations()
    assert found, "Dagitimda en az bir migration olmali"
    assert [migration.version for migration in found] == list(range(1, len(found) + 1))
    assert all(migration.has_down for migration in found), (
        "Her migration geri alma dosyasi tasimali"
    )


def test_projection_close_dirty_hydration_source_migration_is_exact() -> None:
    migration = (
        default_migrations_dir() / "0075_projection_close_dirty_hydration_source.sql"
    ).read_text(encoding="utf-8")

    assert migration.count("hydration_event.event_body->>'source_revision'") == 4
    dirty_source_binding = (
        "then substring(hydration_event.event_body->>'source_revision' from 5 for 40)"
    )
    assert dirty_source_binding in migration
    assert "0075 refused: projection hydration source baseline drift" in migration


def test_shipped_migrations_use_only_zekam_database_identity() -> None:
    removed_slug = "".join(chr(item) for item in (101, 110, 97, 105))
    migrations = tuple(default_migrations_dir().glob("*.sql"))

    assert migrations
    assert all(removed_slug not in path.read_text(encoding="utf-8").lower() for path in migrations)
    baseline = (default_migrations_dir() / "0001_core_baseline.sql").read_text(encoding="utf-8")
    assert "zekam_app" in baseline
    assert "zekam.realm_id" in baseline


def test_pre_contraction_head_23_ledger_is_rejected_as_drift() -> None:
    available = discover_migrations()
    applied = tuple(
        AppliedMigration(
            version=index,
            name=available[index - 1].name if index <= len(available) else f"removed-{index}",
            checksum=checksum,
        )
        for index, checksum in enumerate(PRE_CONTRACTION_CHECKSUMS, start=1)
    )

    findings = detect_drift(applied, available)

    mismatch_versions = {
        finding.version for finding in findings if finding.kind is DriftKind.CHECKSUM_MISMATCH
    }
    missing_versions = {
        finding.version for finding in findings if finding.kind is DriftKind.MISSING_FILE
    }
    assert mismatch_versions == {*range(1, 20), 21, 22, 23}
    assert missing_versions == set()


def test_shipped_migration_directory_is_resolvable() -> None:
    assert default_migrations_dir().is_dir()


def test_checksum_ignores_line_ending_and_trailing_whitespace() -> None:
    assert checksum_of("select 1;\n") == checksum_of("select 1;\r\n")
    assert checksum_of("select 1;\n") == checksum_of("select 1;\n\n\n")
    assert checksum_of("select 1;\n") != checksum_of("select 2;\n")


def test_down_files_are_not_treated_as_migrations(tmp_path: Path) -> None:
    _write(tmp_path, "0001_bir.sql")
    _write(tmp_path, "0001_bir.down.sql")
    found = discover_migrations(tmp_path)
    assert len(found) == 1
    assert found[0].has_down


def test_invalid_file_name_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "ilk_migration.sql")
    with pytest.raises(ValidationFailed, match="dosya adi"):
        discover_migrations(tmp_path)


def test_gap_in_numbering_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "0001_bir.sql")
    _write(tmp_path, "0003_uc.sql")
    with pytest.raises(ValidationFailed, match="bosluksuz"):
        discover_migrations(tmp_path)


def test_missing_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        discover_migrations(tmp_path / "yok")


def test_no_drift_when_checksums_match(tmp_path: Path) -> None:
    _write(tmp_path, "0001_bir.sql")
    available = discover_migrations(tmp_path)
    applied = [AppliedMigration(version=1, name="bir", checksum=available[0].checksum)]
    assert detect_drift(applied, available) == ()


def test_changed_applied_file_is_checksum_drift(tmp_path: Path) -> None:
    _write(tmp_path, "0001_bir.sql")
    available = discover_migrations(tmp_path)
    applied = [AppliedMigration(version=1, name="bir", checksum="0" * 64)]
    findings = detect_drift(applied, available)
    assert [finding.kind for finding in findings] == [DriftKind.CHECKSUM_MISMATCH]


def test_deleted_applied_file_is_missing_drift(tmp_path: Path) -> None:
    _write(tmp_path, "0001_bir.sql")
    available = discover_migrations(tmp_path)
    applied = [
        AppliedMigration(version=1, name="bir", checksum=available[0].checksum),
        AppliedMigration(version=2, name="iki", checksum="0" * 64),
    ]
    findings = detect_drift(applied, available)
    assert DriftKind.MISSING_FILE in {finding.kind for finding in findings}


def test_skipped_lower_version_is_out_of_order_drift(tmp_path: Path) -> None:
    _write(tmp_path, "0001_bir.sql")
    _write(tmp_path, "0002_iki.sql")
    available = discover_migrations(tmp_path)
    applied = [AppliedMigration(version=2, name="iki", checksum=available[1].checksum)]
    findings = detect_drift(applied, available)
    assert DriftKind.OUT_OF_ORDER in {finding.kind for finding in findings}
