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


def test_wheel_excludes_archived_postgresql_migrations() -> None:
    root = Path(__file__).resolve().parents[2]
    document = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    force_include = document["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include["config"] == "zekam/_config"
    assert "migrations" not in force_include
    assert force_include["modeller"] == "zekam/modeller"
    assert not (root / "migrations").exists()
    assert (root / "legacy-preserved" / "migrations").is_dir()


def test_distribution_and_runtime_version_have_one_value() -> None:
    root = Path(__file__).resolve().parents[2]
    document = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    assert document["project"]["version"] == __import__("zekam").__version__
