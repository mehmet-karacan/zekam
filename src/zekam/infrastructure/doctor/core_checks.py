"""Core doctor kontrolleri: surum, runtime, yapilandirma, ZEKAM_HOME ve Git istemcisi."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from zekam import __version__
from zekam.application.config import Settings, core_root
from zekam.application.diagnostics import CheckResult, CheckStatus, Finding, Severity
from zekam.application.home import HomeLayout, assert_separated_from_core
from zekam.domain.errors import ConfigurationError
from zekam.domain.identity import PRODUCT

CATEGORY = "core"
MINIMUM_PYTHON = (3, 12)


@dataclass(frozen=True, slots=True)
class VersionCheck:
    """Kurulu surumu ve core kokunu raporlar."""

    check_id: str = "core.version"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED,
            summary=f"{PRODUCT.name} {__version__}",
            evidence={
                "product": PRODUCT.name,
                "version": __version__,
                "core_root": str(core_root()),
            },
        )


@dataclass(frozen=True, slots=True)
class PythonRuntimeCheck:
    """Python surumunun desteklenen araligi karsiladigini dogrular."""

    check_id: str = "core.python"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        current = sys.version_info[:3]
        supported = current[:2] >= MINIMUM_PYTHON
        evidence = {
            "python_version": ".".join(str(part) for part in current),
            "minimum": ".".join(str(part) for part in MINIMUM_PYTHON),
            "implementation": sys.implementation.name,
        }
        if supported:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.PASSED,
                summary="Python surumu destekleniyor",
                evidence=evidence,
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.FAILED,
            summary="Python surumu desteklenmiyor",
            findings=(
                Finding(
                    code="core.python-too-old",
                    severity=Severity.CRITICAL,
                    title="Desteklenmeyen Python surumu",
                    detail=f"En az {evidence['minimum']} gerekli",
                    next_action="Desteklenen Python surumunu kurup sanal ortami yeniden olusturun",
                ),
            ),
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class ConfigCheck:
    """Yapilandirmanin cozuldugunu ve secret sizdirmadigini dogrular."""

    settings: Settings
    check_id: str = "core.config"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        evidence = self.settings.sanitized()
        if not self.settings.sources:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.DEGRADED,
                summary="Yapilandirma kaynagi bulunamadi, gomulu varsayilanlar kullaniliyor",
                findings=(
                    Finding(
                        code="core.config-source-missing",
                        severity=Severity.WARNING,
                        title="Yapilandirma dosyasi yok",
                        detail="Ne core varsayilani ne de kullanici override'i yuklendi",
                        next_action=(
                            "config/zekam.default.yaml dosyasinin kurulumda oldugunu dogrulayin"
                        ),
                    ),
                ),
                evidence=evidence,
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED,
            summary=f"Yapilandirma yuklendi ({', '.join(self.settings.sources)})",
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class HomeLayoutCheck:
    """ZEKAM_HOME yerlesimini ve core/user-data ayrimini dogrular."""

    layout: HomeLayout
    core_path: Path
    check_id: str = "core.home-layout"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        evidence = {
            "home": str(self.layout.root),
            "core_root": str(self.core_path),
            "env_var": PRODUCT.data_root_env,
        }
        findings: list[Finding] = []

        try:
            assert_separated_from_core(self.layout.root, self.core_path)
        except ConfigurationError as exc:
            findings.append(
                Finding(
                    code="core.home-overlaps-core",
                    severity=Severity.CRITICAL,
                    title="Kullanici verisi core source ile ic ice",
                    detail=str(exc),
                    next_action=(
                        f"{PRODUCT.data_root_env} degiskenini source agacinin disindaki "
                        "bir dizine ayarlayin"
                    ),
                )
            )

        issues = self.layout.verify()
        for issue in issues:
            findings.append(
                Finding(
                    code=f"core.home-{issue.kind}",
                    severity=Severity.WARNING,
                    title="ZEKAM_HOME yerlesimi eksik",
                    detail=f"{issue.relative}: {issue.detail}",
                    next_action=f"`{PRODUCT.cli} init` calistirin",
                )
            )

        if any(finding.severity is Severity.CRITICAL for finding in findings):
            status = CheckStatus.FAILED
            summary = "ZEKAM_HOME yerlesimi kullanilamaz"
        elif findings:
            status = CheckStatus.DEGRADED
            summary = f"ZEKAM_HOME yerlesiminde {len(findings)} eksik"
        else:
            status = CheckStatus.PASSED
            summary = "ZEKAM_HOME yerlesimi eksiksiz"

        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=status,
            summary=summary,
            findings=tuple(findings),
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class GitClientCheck:
    """Git istemcisinin varligini ve surumunu raporlar."""

    check_id: str = "core.git-client"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        executable = shutil.which("git")
        if executable is None:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.DEGRADED,
                summary="Git istemcisi bulunamadi",
                findings=(
                    Finding(
                        code="core.git-missing",
                        severity=Severity.WARNING,
                        title="Git istemcisi yok",
                        detail="Source binding ve worktree yetenekleri kullanilamaz",
                        next_action="Git kurup PATH uzerinde erisilebilir yapin",
                    ),
                ),
                evidence={"available": False},
            )
        version = _git_version(executable)
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED,
            summary=version or "Git kullanilabilir",
            evidence={"available": True, "version": version},
        )


def _git_version(executable: str) -> str | None:
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - ortam bagimli
        return None
    if completed.returncode != 0:  # pragma: no cover - ortam bagimli
        return None
    return completed.stdout.strip() or None
