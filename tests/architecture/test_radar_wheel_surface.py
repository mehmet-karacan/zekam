"""AC-A43 radar/evolution wheel surface — mimari testi.

Wheel zaten varsa:
1. ``src/zekam/application/radar_*.py``, ``schemas/radar-*.json`` ve
   ``docs/RADAR_RUNBOOK.md`` wheel icinde sevk edilmis olmali.
2. pyproject.toml'da wheel disinda birakilmis legacy modullerden biri
   kurulu wheel'den import edilememeli.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _find_wheel() -> Path | None:
    candidates = sorted(REPO_ROOT.glob("dist/zekam-0.1.0-*.whl"))
    return candidates[0] if candidates else None


def _wheel_member_names(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as archive:
        return {info.filename for info in archive.infolist()}


@pytest.fixture(scope="session")
def wheel() -> Path | None:
    return _find_wheel()


def test_radar_python_modules_shipped_in_wheel(wheel: Path | None) -> None:
    if wheel is None:
        pytest.skip("wheel yok")
    members = _wheel_member_names(wheel)
    for source in (REPO_ROOT / "src" / "zekam" / "application").glob("radar_*.py"):
        relative = f"zekam/application/{source.name}"
        assert relative in members, f"Radar modulu wheel icinde eksik: {relative}"


def test_radar_schemas_shipped_in_wheel(wheel: Path | None) -> None:
    if wheel is None:
        pytest.skip("wheel yok")
    members = _wheel_member_names(wheel)
    for source in (REPO_ROOT / "schemas").glob("radar-*.json"):
        relative = f"zekam/schemas/{source.name}"
        assert relative in members, f"Radar schema wheel icinde eksik: {relative}"


def test_radar_runbook_shipped_in_wheel(wheel: Path | None) -> None:
    if wheel is None:
        pytest.skip("wheel yok")
    members = _wheel_member_names(wheel)
    assert "zekam/RADAR_RUNBOOK.md" in members, "RADAR_RUNBOOK.md wheel icinde eksik"


def test_excluded_legacy_module_not_importable_from_wheel(wheel: Path | None) -> None:
    if wheel is None:
        pytest.skip("wheel yok")

    module = "zekam.application.measured_loop_runtime"
    with tempfile.TemporaryDirectory(prefix="zekam-wheel-smoke-") as temporary:
        venv_dir = Path(temporary) / "venv"
        venv.create(venv_dir, with_pip=True, system_site_packages=True)
        python = venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--no-deps",
                "--no-index",
                str(wheel),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        completed = subprocess.run(
            [str(python), "-I", "-c", f"import {module}"],
            capture_output=True,
            text=True,
        )
        assert completed.returncode != 0, f"Excluded legacy modul import edildi: {module}"
        error = (completed.stderr or "").lower()
        assert "importerror" in error or "modulenotfounderror" in error, (
            f"Beklenmeyen hata turu: {completed.stderr}"
        )
