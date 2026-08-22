"""Core doctor kontrollerinin davranisi."""

from __future__ import annotations

from pathlib import Path

import pytest

from zekam.application.config import Settings, load_settings
from zekam.application.diagnostics import CheckStatus, Severity
from zekam.application.home import HomeLayout
from zekam.infrastructure.doctor import core_checks

pytestmark = pytest.mark.unit


def test_version_check_reports_product_and_version() -> None:
    result = core_checks.VersionCheck().run()
    assert result.status is CheckStatus.PASSED
    assert result.evidence["product"] == "Zekam"
    assert result.evidence["version"]


def test_python_runtime_check_passes_on_supported_interpreter() -> None:
    result = core_checks.PythonRuntimeCheck().run()
    assert result.status is CheckStatus.PASSED
    assert result.findings == ()


def test_config_check_lists_sources(settings: Settings) -> None:
    result = core_checks.ConfigCheck(settings=settings).run()
    assert result.status is CheckStatus.PASSED
    assert "core-default" in result.summary


def test_config_check_degrades_without_sources(home_root: Path, tmp_path: Path) -> None:
    empty = load_settings(home=home_root, environ={}, default_file=tmp_path / "yok.yaml")
    result = core_checks.ConfigCheck(settings=empty).run()
    assert result.status is CheckStatus.DEGRADED
    assert result.findings[0].code == "core.config-source-missing"


def test_config_check_evidence_has_no_secret(settings: Settings) -> None:
    rendered = repr(core_checks.ConfigCheck(settings=settings).run().evidence).lower()
    for forbidden in ("password", "token", "secret"):
        assert forbidden not in rendered


def test_home_layout_check_passes_on_prepared_home(layout: HomeLayout, tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.mkdir()
    result = core_checks.HomeLayoutCheck(layout=layout, core_path=core).run()
    assert result.status is CheckStatus.PASSED
    assert result.findings == ()


def test_home_layout_check_degrades_on_missing_directory(
    layout: HomeLayout, tmp_path: Path
) -> None:
    (layout.root / "sandboxlar").rmdir()
    core = tmp_path / "core"
    core.mkdir()
    result = core_checks.HomeLayoutCheck(layout=layout, core_path=core).run()
    assert result.status is CheckStatus.DEGRADED
    assert result.findings[0].code == "core.home-missing-directory"


def test_home_layout_check_fails_when_home_is_inside_core(tmp_path: Path) -> None:
    core = tmp_path / "core"
    core.mkdir()
    layout = HomeLayout(core / "veri").ensure()
    result = core_checks.HomeLayoutCheck(layout=layout, core_path=core).run()
    assert result.status is CheckStatus.FAILED
    codes = {finding.code for finding in result.findings}
    assert "core.home-overlaps-core" in codes
    severities = {finding.severity for finding in result.findings}
    assert Severity.CRITICAL in severities


def test_git_client_check_reports_availability() -> None:
    result = core_checks.GitClientCheck().run()
    assert result.status in {CheckStatus.PASSED, CheckStatus.DEGRADED}
    assert "available" in result.evidence
