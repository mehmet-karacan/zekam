"""Bound real-source, typed process ve teslim sozlesmesi.

Builder registry'de bagli gercek source rootunda calisir; proje kopyasi veya
detached worktree uretilmez. Yazma exact relative path allowlist icindedir.
Network default-deny'dir. Shell yerine typed argv kullanilir.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from zekam.domain.canonical import digest, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed

MAX_OUTPUT_BYTES = 1_048_576
MAX_TIMEOUT_SECONDS = 3600
DEFAULT_TIMEOUT_SECONDS = 300

#: Calistirilabilir dosya alaninda gizli bir shell komut satirini ele veren
#: karakterler. Argumanlara uygulanmaz: `shell=False` oldugundan bir argumanin
#: icindeki `;` veya `$` kabuga hic ulasmaz ve mesru olabilir (ornegin
#: `python -c "import os; ..."`).
_SHELL_METACHARACTERS = re.compile(r"[;&|><`$]")

#: Satir sonu her alanda reddedilir: argv'ye satir sonu koymak neredeyse her
#: zaman yanlis ayristirilmis bir komut satirinin isaretidir.
_LINE_BREAK = re.compile(r"[\n\r]")
_SENSITIVE_ENV = re.compile(
    r"(?:secret|credential|password|parola|api[-_ ]?key|private[-_ ]?key|token)",
    re.IGNORECASE,
)


class WorkspaceState(StrEnum):
    PREPARED = "prepared"
    ACTIVE = "active"
    DELIVERED = "delivered"
    DISCARDED = "discarded"


class DeliveryOutcome(StrEnum):
    APPLIED = "applied"
    REJECTED = "rejected"
    DRIFTED = "drifted"


def assert_relative_path(value: str, label: str = "path") -> str:
    """Portable, relative ve traversal'siz bir yol dogrular."""

    if not value.strip():
        raise ValidationFailed(f"{label} bos olamaz")
    if "\\" in value:
        raise PolicyViolation(f"{label} yalniz posix ayirici kullanabilir")
    if value.startswith("/") or PureWindowsPath(value).is_absolute():
        raise PolicyViolation(f"{label} absolute path olamaz")
    parts = PurePosixPath(value).parts
    if ".." in parts:
        raise PolicyViolation(f"{label} traversal tasiyamaz")
    if any(part in {".", ""} for part in parts):
        raise ValidationFailed(f"{label} normalize degil")
    return value


@dataclass(frozen=True, slots=True)
class PathAllowlist:
    """Yazilabilir exact relative path kumesi.

    Allowlist bos olamaz: "her yere yazabilir" bir sandbox degildir. Bir girdi
    dizin ise altindaki yollar da izinlidir; disindaki her yol reddedilir.
    """

    entries: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.entries:
            raise PolicyViolation("path allowlist bos olamaz")
        for entry in self.entries:
            assert_relative_path(entry, "allowlist girdisi")
        if len(set(self.entries)) != len(self.entries):
            raise ValidationFailed("allowlist girdileri tekrar edemez")

    def permits(self, path: str) -> bool:
        assert_relative_path(path, "hedef path")
        target = PurePosixPath(path)
        for entry in self.entries:
            allowed = PurePosixPath(entry)
            if target == allowed or allowed in target.parents:
                return True
        return False

    def assert_permits(self, path: str) -> str:
        if not self.permits(path):
            raise PolicyViolation("hedef path allowlist disinda")
        return path

    def as_dict(self) -> dict[str, Any]:
        return {"entries": sorted(self.entries)}


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    """Default-deny network. Izin exact host ve operasyon ister."""

    allowed_hosts: frozenset[str] = frozenset()
    allowed_operations: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for host in self.allowed_hosts:
            if not host or host != host.strip().lower() or "/" in host:
                raise ValidationFailed("host girdisi normalize degil")
        if self.allowed_hosts and not self.allowed_operations:
            raise PolicyViolation("host allowlist exact operasyon listesi ister")

    @property
    def is_default_deny(self) -> bool:
        return not self.allowed_hosts

    def permits(self, host: str, operation: str) -> bool:
        return host in self.allowed_hosts and operation in self.allowed_operations

    def assert_permits(self, host: str, operation: str) -> None:
        if not self.permits(host, operation):
            raise PolicyViolation("network erisimi default-deny tarafindan reddedildi")

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed_hosts": sorted(self.allowed_hosts),
            "allowed_operations": sorted(self.allowed_operations),
            "default_deny": self.is_default_deny,
        }


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    """Tek bir builder icin yazma ve network sinirlari."""

    allowlist: PathAllowlist
    network: NetworkPolicy = field(default_factory=NetworkPolicy)
    main_tree_read_only: bool = False

    def __post_init__(self) -> None:
        if self.main_tree_read_only:
            raise PolicyViolation("kod mutation'i bagli gercek source rootunda yapilmalidir")

    @property
    def policy_digest(self) -> str:
        return digest(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowlist": self.allowlist.as_dict(),
            "network": self.network.as_dict(),
            "main_tree_read_only": False,
            "direct_source_write": True,
            "project_copy": False,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceSpec:
    """Bagli gercek source rootunda exact mutation istegi."""

    workspace_id: str
    project_ref: str
    work_ref: str
    source_revision: str
    policy: SandboxPolicy
    detached: bool = False

    def __post_init__(self) -> None:
        if self.detached:
            raise PolicyViolation("project mutation icin detached worktree yasaktir")
        for label, value in (
            ("workspace_id", self.workspace_id),
            ("project_ref", self.project_ref),
            ("work_ref", self.work_ref),
            ("source_revision", self.source_revision),
        ):
            if not value.strip():
                raise ValidationFailed(f"{label} bos olamaz")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-workspace-spec/v1",
            "workspace_id": self.workspace_id,
            "project_ref": self.project_ref,
            "work_ref": self.work_ref,
            "source_revision": self.source_revision,
            "policy": self.policy.as_dict(),
            "detached": False,
            "direct_source_write": True,
        }

    @property
    def spec_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class TreeFingerprint:
    """Bagli gercek source tree'nin islem oncesi/sonrasi durumu."""

    head: str
    tree_digest: str
    dirty: bool

    def __post_init__(self) -> None:
        parse_digest(self.tree_digest)
        if not self.head.strip():
            raise ValidationFailed("HEAD bos olamaz")

    def matches(self, other: TreeFingerprint) -> bool:
        return (
            self.head == other.head
            and self.tree_digest == other.tree_digest
            and self.dirty == other.dirty
        )

    def as_dict(self) -> dict[str, Any]:
        return {"head": self.head, "tree_digest": self.tree_digest, "dirty": self.dirty}


# -- typed process ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    """Shell'siz komut tanimi.

    `argv` ilk ogesi calistirilabilir dosyadir. Shell string'i, metakarakter,
    absolute olmayan calisma dizini ve sinirsiz timeout reddedilir.
    """

    argv: tuple[str, ...]
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    env: tuple[tuple[str, str], ...] = ()
    max_output_bytes: int = MAX_OUTPUT_BYTES

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValidationFailed("argv bos olamaz")
        for argument in self.argv:
            if not isinstance(argument, str) or not argument:
                raise ValidationFailed("argv ogeleri bos olmayan metin olmali")
            if _LINE_BREAK.search(argument):
                raise PolicyViolation("argv satir sonu tasiyamaz")
        executable = self.argv[0]
        if _SHELL_METACHARACTERS.search(executable) or executable != executable.strip():
            raise PolicyViolation("calistirilabilir alan shell komut satiri tasiyamaz")
        if " " in executable and not executable.lower().endswith((".exe", ".bat", ".cmd")):
            raise PolicyViolation("calistirilabilir alan bosluklu komut satiri olamaz")
        if not 1 <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ValidationFailed(f"timeout 1..{MAX_TIMEOUT_SECONDS} araliginda olmali")
        if not 1 <= self.max_output_bytes <= MAX_OUTPUT_BYTES:
            raise ValidationFailed("cikti siniri gecersiz")
        seen: set[str] = set()
        for name, value in self.env:
            if not name or not name.replace("_", "").isalnum():
                raise ValidationFailed("env adi gecersiz")
            if name in seen:
                raise ValidationFailed("env adi tekrar edemez")
            seen.add(name)
            if _SENSITIVE_ENV.search(name) or _SENSITIVE_ENV.search(value):
                raise PolicyViolation("env secret benzeri deger tasiyamaz")

    @property
    def executable(self) -> str:
        return self.argv[0]

    def body(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "timeout_seconds": self.timeout_seconds,
            "env_names": sorted(name for name, _ in self.env),
            "max_output_bytes": self.max_output_bytes,
        }

    @property
    def spec_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Typed calistirma sonucu. Ham cikti kanonik kayda girmez, digest girer."""

    spec_digest: str
    exit_code: int
    duration_ms: int
    stdout_digest: str
    stderr_digest: str
    truncated: bool
    timed_out: bool

    def __post_init__(self) -> None:
        parse_digest(self.spec_digest)
        parse_digest(self.stdout_digest)
        parse_digest(self.stderr_digest)
        if self.duration_ms < 0:
            raise ValidationFailed("sure negatif olamaz")
        if self.timed_out and self.exit_code == 0:
            raise ValidationFailed("timeout sonucu basarili olamaz")

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def as_dict(self) -> dict[str, Any]:
        return {
            "spec_digest": self.spec_digest,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "stdout_digest": self.stdout_digest,
            "stderr_digest": self.stderr_digest,
            "truncated": self.truncated,
            "timed_out": self.timed_out,
            "succeeded": self.succeeded,
        }

    @property
    def result_digest(self) -> str:
        return digest(self.as_dict())


# -- patch teslim -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PatchArtifact:
    """Bagli gercek source degisikliginden uretilmis kanit artifact'i."""

    artifact_id: str
    workspace_id: str
    base_revision: str
    changed_paths: tuple[str, ...]
    patch_digest: str
    created_at: dt.datetime

    def __post_init__(self) -> None:
        parse_digest(self.patch_digest)
        if not self.changed_paths:
            raise ValidationFailed("bos yama teslim edilemez")
        for path in self.changed_paths:
            assert_relative_path(path, "yama yolu")
        if len(set(self.changed_paths)) != len(self.changed_paths):
            raise ValidationFailed("yama yollari tekrar edemez")
        if self.created_at.tzinfo is None:
            raise ValidationFailed("zaman damgasi timezone-aware olmali")

    def assert_within(self, allowlist: PathAllowlist) -> None:
        outside = tuple(path for path in self.changed_paths if not allowlist.permits(path))
        if outside:
            raise PolicyViolation("yama allowlist disinda yol degistiriyor")

    def body(self) -> dict[str, Any]:
        return {
            "schema": "zekam-patch-artifact/v1",
            "artifact_id": self.artifact_id,
            "workspace_id": self.workspace_id,
            "base_revision": self.base_revision,
            "changed_paths": sorted(self.changed_paths),
            "patch_digest": self.patch_digest,
        }

    @property
    def artifact_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True, slots=True)
class DeliveryDecision:
    """Patch teslim karari. Receipt yalniz burada `applied` ise yazilir."""

    artifact_digest: str
    outcome: DeliveryOutcome
    apply_check_passed: bool
    tests_passed: bool
    verifier_ref: str
    builder_ref: str
    detail: str = ""

    def __post_init__(self) -> None:
        parse_digest(self.artifact_digest)
        if self.verifier_ref == self.builder_ref:
            raise PolicyViolation("verifier builder ile ayni kimlik olamaz")
        if self.outcome is DeliveryOutcome.APPLIED and not (
            self.apply_check_passed and self.tests_passed
        ):
            raise PolicyViolation("apply-check ve test gecmeden teslim applied olamaz")
        if self.outcome is not DeliveryOutcome.APPLIED and not self.detail.strip():
            raise ValidationFailed("basarisiz teslim gerekce ister")

    def as_dict(self) -> dict[str, Any]:
        return {
            "artifact_digest": self.artifact_digest,
            "outcome": str(self.outcome),
            "apply_check_passed": self.apply_check_passed,
            "tests_passed": self.tests_passed,
            "verifier_ref": self.verifier_ref,
            "builder_ref": self.builder_ref,
            "detail": self.detail,
        }

    @property
    def decision_digest(self) -> str:
        return digest(self.as_dict())


def assert_no_drift(
    *,
    planned_revision: str,
    current_revision: str,
    planned_paths: tuple[str, ...],
    changed_paths: tuple[str, ...],
) -> None:
    """Teslim aninda plan kapsaminin hala gecerli oldugunu dogrular."""

    if planned_revision != current_revision:
        raise PolicyViolation("source revision drift; plan yeniden dogrulanmali")
    extra = tuple(path for path in changed_paths if path not in planned_paths)
    if extra:
        raise PolicyViolation("yama plan disinda yol degistiriyor")
