"""Urun kimligi sabitlerinin korunmasi."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from zekam.domain.identity import PRODUCT

pytestmark = pytest.mark.unit


def test_product_identity_is_zekam() -> None:
    assert PRODUCT.name == "Zekam"
    assert PRODUCT.slug == "zekam"
    assert PRODUCT.python_package == "zekam"
    assert PRODUCT.cli == "zekam"
    assert PRODUCT.data_root_env == "ZEKAM_HOME"


def test_identity_is_immutable() -> None:
    with pytest.raises((AttributeError, TypeError)):
        PRODUCT.name = "Baska"  # type: ignore[misc]


def test_distribution_has_no_compatibility_package_or_console_script() -> None:
    root = Path(__file__).resolve().parents[2]
    removed_slug = "".join(chr(item) for item in (101, 110, 97, 105))
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert not (root / "src" / removed_slug).exists()
    assert f"{removed_slug} =" not in pyproject
    assert f'"src/{removed_slug}"' not in pyproject


def test_wheel_includes_complete_fresh_migration_set() -> None:
    root = Path(__file__).resolve().parents[2]
    document = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = document["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include["config"] == "zekam/_config"
    migrations = sorted((root / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql"))
    up = [path for path in migrations if not path.name.endswith(".down.sql")]
    down = [path for path in migrations if path.name.endswith(".down.sql")]

    assert force_include["migrations"] == "zekam/migrations"
    assert [int(path.name[:4]) for path in up] == list(range(1, 37))
    assert len(down) == 36
