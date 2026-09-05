"""Core doctor kontrollerinin davranisi."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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


def test_windows_git_check_requires_schannel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "git.exe")
    monkeypatch.setattr(core_checks, "_git_version", lambda _executable: "git version test")
    monkeypatch.setattr(core_checks, "_git_config_value", lambda *_args: "openssl")
    monkeypatch.setattr(sys, "platform", "win32")

    result = core_checks.GitClientCheck().run()

    assert result.status is CheckStatus.DEGRADED
    assert result.findings[0].code == "core.git-windows-ca-backend"
    assert "sslVerify" in result.findings[0].next_action


def test_windows_git_check_accepts_schannel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "git.exe")
    monkeypatch.setattr(core_checks, "_git_version", lambda _executable: "git version test")
    monkeypatch.setattr(core_checks, "_git_config_value", lambda *_args: "schannel")
    monkeypatch.setattr(sys, "platform", "win32")

    result = core_checks.GitClientCheck().run()

    assert result.status is CheckStatus.PASSED
    assert result.evidence["ssl_backend"] == "schannel"


def test_git_config_lookup_uses_read_only_argument_order(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[str] = []

    def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        observed.extend(argv)
        return SimpleNamespace(returncode=0, stdout="schannel\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    value = core_checks._git_config_value("git.exe", "--global", "http.sslBackend")

    assert value == "schannel"
    assert observed == ["git.exe", "config", "--global", "--get", "http.sslBackend"]


def test_git_repository_check_preserves_first_dirty_path_character(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    values = {
        ("rev-parse", "HEAD"): "a" * 40,
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("status", "--porcelain=v1", "--untracked-files=all"): " M VALIDATION_RESULT.json",
        (
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        ): "origin/main",
        ("rev-parse", "@{upstream}"): "a" * 40,
        ("rev-list", "--left-right", "--count", "@{upstream}...HEAD"): "0 0",
    }
    monkeypatch.setattr(shutil, "which", lambda _name: "git.exe")
    monkeypatch.setattr(
        core_checks,
        "_git_repository_value",
        lambda _executable, _root, *args: values.get(args),
    )

    result = core_checks.GitRepositoryCheck(root=tmp_path).run()

    assert result.evidence["dirty_paths"] == ["VALIDATION_RESULT.json"]
