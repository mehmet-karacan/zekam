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
from zekam.domain.canonical import digest
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
        configuration_sources = tuple(
            source for source in self.settings.sources if source != "managed-policy"
        )
        if not configuration_sources:
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
    """Git istemcisini ve Windows sistem sertifika deposu kullanimini raporlar."""

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
        ssl_backend = _git_config_value(executable, "--global", "http.sslBackend")
        evidence = {
            "available": True,
            "version": version,
            "platform": sys.platform,
            "ssl_backend": ssl_backend,
        }
        if sys.platform == "win32" and (ssl_backend or "").casefold() != "schannel":
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.DEGRADED,
                summary="Git Windows sertifika deposunu kullanmiyor",
                findings=(
                    Finding(
                        code="core.git-windows-ca-backend",
                        severity=Severity.WARNING,
                        title="Windows Git TLS backend ayari eksik",
                        detail=(
                            "Git HTTPS baglantilari Windows sertifika deposu yerine "
                            f"{ssl_backend or 'varsayilan backend'} kullaniyor"
                        ),
                        next_action=(
                            "`git config --global http.sslBackend schannel` calistirin; "
                            "sslVerify ayarini kapatmayin"
                        ),
                        authority_required=True,
                    ),
                ),
                evidence=evidence,
            )
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=CheckStatus.PASSED,
            summary=version or "Git kullanilabilir",
            evidence=evidence,
        )


@dataclass(frozen=True, slots=True)
class GitRepositoryCheck:
    """Core repository'nin cached upstream, dirty ve divergence durumunu okur."""

    root: Path
    check_id: str = "core.git-repository"
    category: str = CATEGORY

    def run(self) -> CheckResult:
        executable = shutil.which("git")
        if executable is None:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.SKIPPED,
                summary="Git istemcisi olmadigi icin repository kontrolu atlandi",
                evidence={"available": False, "network_checked": False},
            )
        head = _git_repository_value(executable, self.root, "rev-parse", "HEAD")
        branch = _git_repository_value(executable, self.root, "rev-parse", "--abbrev-ref", "HEAD")
        if head is None or branch is None:
            return CheckResult(
                check_id=self.check_id,
                category=self.category,
                status=CheckStatus.SKIPPED,
                summary="Core root Git repository olarak okunamadi",
                findings=(
                    Finding(
                        code="core.git-repository-unavailable",
                        severity=Severity.WARNING,
                        title="Git repository durumu okunamadi",
                        detail="Core root icin HEAD veya branch cozumlenemedi",
                        next_action="Core root ve Git checkout durumunu dogrulayin",
                    ),
                ),
                evidence={"available": False, "network_checked": False},
            )
        dirty_output = _git_repository_value(
            executable, self.root, "status", "--porcelain=v1", "--untracked-files=all"
        )
        dirty_paths = tuple(line[3:] for line in (dirty_output or "").splitlines() if len(line) > 3)
        upstream = _git_repository_value(
            executable,
            self.root,
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
        )
        upstream_head = (
            None
            if upstream is None
            else _git_repository_value(executable, self.root, "rev-parse", "@{upstream}")
        )
        ahead = 0
        behind = 0
        if upstream_head is not None:
            counts = _git_repository_value(
                executable,
                self.root,
                "rev-list",
                "--left-right",
                "--count",
                "@{upstream}...HEAD",
            )
            if counts is not None:
                left, right = counts.split()
                behind, ahead = int(left), int(right)
        plan_digest = digest(
            {
                "schema": "zekam-git-repository-repair-plan/v1",
                "branch": branch,
                "head": head,
                "upstream": upstream,
                "upstream_head": upstream_head,
                "ahead": ahead,
                "behind": behind,
                "dirty_paths": list(dirty_paths),
                "network_checked": False,
            }
        )
        evidence = {
            "available": True,
            "branch": branch,
            "head": head,
            "upstream": upstream,
            "upstream_head": upstream_head,
            "ahead": ahead,
            "behind": behind,
            "dirty_count": len(dirty_paths),
            "dirty_paths": list(dirty_paths),
            "network_checked": False,
            "cached_remote_state": True,
            "repair_plan_digest": plan_digest,
            "grants_authority": False,
        }
        findings: list[Finding] = []
        if upstream is None:
            findings.append(
                Finding(
                    code="core.git-upstream-missing",
                    severity=Severity.WARNING,
                    title="Git upstream tanimli degil",
                    detail=f"{branch} dali bir upstream ref izlemiyor",
                    next_action="Exact remote/branch ile upstream binding planini inceleyin",
                    authority_required=True,
                )
            )
        if dirty_paths:
            findings.append(
                Finding(
                    code="core.git-worktree-dirty",
                    severity=Severity.WARNING,
                    title="Git worktree temiz degil",
                    detail=f"{len(dirty_paths)} path pull/fast-forward islemini bloke ediyor",
                    next_action="Dirty path'leri commit, stash veya geri alinabilir sekilde ayirin",
                    authority_required=True,
                    evidence={"dirty_paths": list(dirty_paths)},
                )
            )
        if ahead and behind:
            findings.append(
                Finding(
                    code="core.git-upstream-diverged",
                    severity=Severity.ERROR,
                    title="Git dali upstream ile ayrismis",
                    detail=f"{behind} behind, {ahead} ahead; otomatik pull reddedilir",
                    next_action="Merge/rebase kararini ayri Work/Plan ile verin",
                    authority_required=True,
                )
            )
        elif behind:
            findings.append(
                Finding(
                    code="core.git-upstream-behind",
                    severity=Severity.WARNING,
                    title="Git dali cached upstream gerisinde",
                    detail=f"Fast-forward adayi: {behind} commit",
                    next_action=(
                        f"`zekam doctor --repair-plan` ile remote HEAD'i yenileyin; "
                        f"exact plan digest: {plan_digest}"
                    ),
                    authority_required=True,
                )
            )
        elif ahead:
            findings.append(
                Finding(
                    code="core.git-upstream-ahead",
                    severity=Severity.WARNING,
                    title="Git dali upstream ilerisinde",
                    detail=f"Remote'da olmayan {ahead} local commit var",
                    next_action="Push icin ayri exact authorization ve verifier kapisini kullanin",
                    authority_required=True,
                )
            )
        if any(item.severity is Severity.ERROR for item in findings):
            status = CheckStatus.FAILED
            summary = "Git repository upstream ile ayrismis"
        elif findings:
            status = CheckStatus.DEGRADED
            summary = "Git repository pull/fast-forward icin hazir degil"
        else:
            status = CheckStatus.PASSED
            summary = "Git repository cached upstream ile senkron ve temiz"
        return CheckResult(
            check_id=self.check_id,
            category=self.category,
            status=status,
            summary=summary,
            findings=tuple(findings),
            evidence=evidence,
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


def _git_config_value(executable: str, scope: str, key: str) -> str | None:
    """Tek bir Git config degerini shell kullanmadan salt okunur getirir."""

    try:
        completed = subprocess.run(
            [executable, "config", scope, "--get", key],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - ortam bagimli
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def _git_repository_value(executable: str, root: Path, *args: str) -> str | None:
    """Repository icinde tek bir salt okunur Git komutu calistirir."""

    try:
        completed = subprocess.run(
            [executable, "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.rstrip()
