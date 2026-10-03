"""Test-only aday patch sozlesmesi, korumalar ve calisma alani portu (W05, U12, D18).

Aday patch atomik uygulanir; reddedilende YALNIZ gorevin kendi degisikligi
preimage/ownership kontroluyle geri alinir. Kullanicinin dirty edit'ine, production
koduna, POM/build/coverage ayarina ve verifier varliklarina dokunulmaz (test-only mod).
Production degisikligi bu modulun kapsami disindadir: ayri refactor onerisi + izin ister.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Protocol

from zekam.domain.canonical import digest, digest_of_bytes, parse_digest
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.unit_test_engineering import UnitTestRequest, normalize_relative_source

MAX_FILES: Final = 20
MAX_FILE_BYTES: Final = 256 * 1024
MAX_PATCH_BYTES: Final = 1024 * 1024

_PROTECTED_BASENAMES: Final = frozenset(
    {"pom.xml", "mvnw", "mvnw.cmd", "settings.xml", "toolchains.xml", "build.gradle", "jvm.config"}
)
_PROTECTED_SEGMENTS: Final = frozenset({".mvn", ".github", ".git", "verifier", "verifiers"})
_PROTECTED_NAME_RE: Final = re.compile(r"(?i)(jacoco|surefire|failsafe|pitest|\.pom$)")


_SHORT_NAME_RE: Final = re.compile(r"^[^~]{1,6}~\d+(?:\..*)?$")
_DEVICE_RE: Final = re.compile(r"^(?:con|prn|aux|nul|conin\$|conout\$|(?:com|lpt)[1-9])$")


def fold_path(path: str) -> str:
    """Karsilastirma anahtari: casefold + her bilesende sondaki nokta/bosluk atilir (Windows)."""

    return "/".join(segment.rstrip(". ").casefold() for segment in path.split("/"))


@dataclass(frozen=True, slots=True)
class FileChange:
    """Tek dosya degisikligi. ``preimage_digest=None`` yeni dosya (var olmamali) demektir."""

    path: str
    content: str
    preimage_digest: str | None

    def __post_init__(self) -> None:
        normalize_relative_source(self.path)
        if type(self.content) is not str or "\x00" in self.content:
            raise ValidationFailed("Dosya icerigi NUL icermeyen metin olmali")
        if len(self.content.encode("utf-8")) > MAX_FILE_BYTES:
            raise PolicyViolation("Dosya boyut siniri asildi")
        if self.preimage_digest is not None:
            parse_digest(self.preimage_digest)

    @property
    def content_bytes(self) -> bytes:
        return self.content.encode("utf-8")

    @property
    def postimage_digest(self) -> str:
        return digest_of_bytes(self.content_bytes)


@dataclass(frozen=True, slots=True)
class TestPatch:
    __test__ = False

    files: tuple[FileChange, ...]

    def __post_init__(self) -> None:
        if not self.files:
            raise ValidationFailed("Patch en az bir dosya tasimali")
        if len(self.files) > MAX_FILES:
            raise PolicyViolation("Patch dosya sayisi siniri asildi")
        paths = [f.path for f in self.files]
        if len(set(paths)) != len(paths) or len({p.casefold() for p in paths}) != len(paths):
            raise ValidationFailed("Patch tekrar eden/buyuk-kucuk harf cakisan yol tasiyor")
        if sum(len(f.content_bytes) for f in self.files) > MAX_PATCH_BYTES:
            raise PolicyViolation("Patch toplam boyut siniri asildi")

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(f.path for f in self.files))


def parse_patch(document: Mapping[str, Any]) -> TestPatch:
    if not isinstance(document, Mapping) or set(document) != {"files"}:
        raise ValidationFailed("Patch yalniz 'files' tasimali")
    raw = document["files"]
    if not isinstance(raw, list | tuple):
        raise ValidationFailed("files liste olmali")
    changes: list[FileChange] = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) != {"path", "content", "preimage_digest"}:
            raise ValidationFailed("Dosya degisikligi path/content/preimage_digest tasimali")
        if not isinstance(item["content"], str):
            raise ValidationFailed("Dosya icerigi metin olmali")
        changes.append(FileChange(str(item["path"]), item["content"], item["preimage_digest"]))
    return TestPatch(tuple(changes))


class GuardViolation(PolicyViolation):
    """Test-only mod ihlali; ``code`` makine-okunur ayrintidir."""

    def __init__(self, reason: str, path: str) -> None:
        super().__init__(f"Test-only mod: {reason}")
        self.reason = reason
        self.path = path


@dataclass(frozen=True, slots=True)
class TestOnlyPolicy:
    __test__ = False

    source_files: frozenset[str]
    allowed_test_paths: tuple[str, ...] = ()
    forbidden_paths: frozenset[str] = frozenset()
    verifier_assets: frozenset[str] = frozenset()
    frozen_paths: frozenset[str] = frozenset()
    protected_dirty_paths: frozenset[str] = frozenset()
    #: Bu gorevin onceki kabul edilmis dosyalari: yalniz bunlar (digest eslesirse) degistirilebilir.
    owned_paths: frozenset[str] = frozenset()

    @classmethod
    def for_request(
        cls,
        request: UnitTestRequest,
        *,
        verifier_assets: Iterable[str] = (),
        frozen_paths: Iterable[str] = (),
        protected_dirty_paths: Iterable[str] = (),
        owned_paths: Iterable[str] = (),
    ) -> TestOnlyPolicy:
        return cls(
            source_files=frozenset(request.source_files),
            allowed_test_paths=request.allowed_test_paths,
            forbidden_paths=frozenset(request.forbidden_paths),
            verifier_assets=frozenset(verifier_assets),
            frozen_paths=frozenset(frozen_paths),
            protected_dirty_paths=frozenset(protected_dirty_paths),
            owned_paths=frozenset(owned_paths),
        )

    def _under_allowed(self, folded: str) -> bool:
        if self.allowed_test_paths:
            return any(
                folded == a or folded.startswith(a.rstrip("/") + "/")
                for a in (fold_path(x) for x in self.allowed_test_paths)
            )
        segments = folded.split("/")
        in_test_tree = any(
            segments[i] == "src" and segments[i + 1] == "test" for i in range(len(segments) - 1)
        )
        return in_test_tree and folded.endswith(".java")

    def check_path(self, path: str, *, modifies_existing: bool) -> None:
        """Windows/macOS dosya sistemleri icin buyuk-kucuk harf ve sondaki nokta/bosluk duyarsiz."""

        try:
            normalize_relative_source(path)
        except ValidationFailed as exc:
            raise GuardViolation("invalid-path", path) from exc
        folded = fold_path(path)
        segments = folded.split("/")
        if any(seg == "" or ":" in seg for seg in segments):
            raise GuardViolation("invalid-path-component", path)
        for segment in segments:
            if _SHORT_NAME_RE.match(segment):
                # Windows 8.3 kisa adi (SETTIN~1.XML): gercek adi gizleyebilir.
                raise GuardViolation("windows-short-name", path)
            if _DEVICE_RE.match(segment.split(".")[0].rstrip(". ")):
                raise GuardViolation("windows-device-name", path)
        base = segments[-1]
        in_main = any(
            segments[i] == "src" and segments[i + 1] == "main" for i in range(len(segments) - 1)
        )
        if folded in {fold_path(x) for x in self.source_files} or in_main:
            raise GuardViolation("production-path", path)
        if base in _PROTECTED_BASENAMES or _PROTECTED_NAME_RE.search(base):
            raise GuardViolation("build-or-coverage-config", path)
        if any(seg in _PROTECTED_SEGMENTS for seg in segments):
            raise GuardViolation("protected-directory", path)
        if folded in {fold_path(x) for x in (*self.forbidden_paths, *self.verifier_assets)}:
            raise GuardViolation("forbidden-or-verifier-asset", path)
        if folded in {fold_path(x) for x in self.frozen_paths}:
            raise GuardViolation("reproducer-frozen", path)
        if not self._under_allowed(folded):
            raise GuardViolation("outside-allowed-test-paths", path)
        if modifies_existing and folded in {fold_path(x) for x in self.protected_dirty_paths}:
            raise GuardViolation("user-dirty-path", path)

    def check_patch(self, patch: TestPatch) -> None:
        for change in patch.files:
            self.check_path(change.path, modifies_existing=change.preimage_digest is not None)


class SmellCode(StrEnum):
    SLEEP = "sleep"
    PRINT_ONLY = "print-only"
    NO_ASSERTION = "no-assertion"
    TAUTOLOGY = "tautology"
    REFLECTION = "reflection"
    DISABLED = "disabled"


_TEST_ANN: Final = re.compile(r"@(?:Test|ParameterizedTest|RepeatedTest|Property)\b")
_ASSERT_TOKEN: Final = re.compile(
    r"\bassert\w*\s*\(|\bverify\w*\s*\(|\bassertThrows\b|\bfail\s*\(|\.should|\bexpect\w*\s*\("
)
_TAUTOLOGY: Final = re.compile(
    r"assertTrue\s*\(\s*true\s*\)|assertEquals\s*\(\s*(\w+)\s*,\s*\1\s*\)"
)


@dataclass(frozen=True, slots=True)
class SmellCandidate:
    """Statik kural ADAY bulur; sonuc semantik reviewer'in (verifier) kararidir."""

    code: SmellCode
    path: str


def find_smell_candidates(patch: TestPatch) -> tuple[SmellCandidate, ...]:
    found: list[SmellCandidate] = []
    for change in patch.files:
        text = change.content
        has_tests = bool(_TEST_ANN.search(text))
        if "Thread.sleep(" in text:
            found.append(SmellCandidate(SmellCode.SLEEP, change.path))
        if "setAccessible(true)" in text:
            found.append(SmellCandidate(SmellCode.REFLECTION, change.path))
        if _TAUTOLOGY.search(text):
            found.append(SmellCandidate(SmellCode.TAUTOLOGY, change.path))
        if re.search(r"@(?:Disabled|Ignore)\b", text):
            found.append(SmellCandidate(SmellCode.DISABLED, change.path))
        if has_tests and not _ASSERT_TOKEN.search(text):
            code = SmellCode.PRINT_ONLY if "System.out.print" in text else SmellCode.NO_ASSERTION
            found.append(SmellCandidate(code, change.path))
    return tuple(found)


def has_executable_test(patch: TestPatch) -> bool:
    """Final suite icin: en az bir dosya @Test + assertion-benzeri dogrulama tasir."""

    return any(_TEST_ANN.search(f.content) and _ASSERT_TOKEN.search(f.content) for f in patch.files)


# -- aday manifest ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidateFile:
    path: str
    preimage_digest: str | None
    postimage_digest: str


@dataclass(frozen=True, slots=True)
class CandidateManifest:
    """Claim oncesi nesne deposuna yazilir; candidate_digest bu manifestin digest'idir."""

    parent_state_digest: str
    files: tuple[CandidateFile, ...]
    kind: str = "patch"

    def to_payload(self) -> dict[str, Any]:
        return {
            "contract": "zekam-unit-test-candidate/v1",
            "kind": self.kind,
            "parent_state_digest": self.parent_state_digest,
            "files": [[f.path, f.preimage_digest, f.postimage_digest] for f in self.files],
        }

    @property
    def candidate_digest(self) -> str:
        return digest(self.to_payload())

    @classmethod
    def from_payload(cls, p: Mapping[str, Any]) -> CandidateManifest:
        return cls(
            parent_state_digest=str(p["parent_state_digest"]),
            files=tuple(CandidateFile(str(a), b, str(c)) for a, b, c in p["files"]),
            kind=str(p["kind"]),
        )

    @classmethod
    def for_patch(cls, parent_state_digest: str, patch: TestPatch) -> CandidateManifest:
        files = tuple(
            CandidateFile(f.path, f.preimage_digest, f.postimage_digest)
            for f in sorted(patch.files, key=lambda f: f.path)
        )
        return cls(parent_state_digest, files)

    @property
    def changed_files(self) -> tuple[tuple[str, str], ...]:
        return tuple((f.path, f.postimage_digest) for f in self.files)


def accepted_state_digest(owned: Mapping[str, str]) -> str:
    """Kabul edilmis, gorev-sahipli test dosyalarinin kumulatif durumu."""

    return digest({"owned": sorted(owned.items())})


# -- calisma alani portu ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppliedFile:
    path: str
    preimage_digest: str | None
    postimage_digest: str


@dataclass(frozen=True, slots=True)
class AppliedPatch:
    files: tuple[AppliedFile, ...]

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(f.path for f in self.files)


@dataclass(frozen=True, slots=True)
class RevertReceipt:
    restored: tuple[str, ...]
    #: Gorevin yazdigi digest'ten farkli (kullanici/baska surec degistirdi): DOKUNULMADI.
    foreign_modified: tuple[str, ...] = ()
    already_original: tuple[str, ...] = ()
    failed: tuple[str, ...] = field(default_factory=tuple)

    @property
    def clean(self) -> bool:
        return not self.foreign_modified and not self.failed


class PreimageMismatch(PolicyViolation):
    """Dosya, patch'in beklediği preimage'ta degil (kullanici duzenledi); hicbir sey yazilmadi."""

    def __init__(self, path: str) -> None:
        super().__init__("Preimage uyusmuyor; atomik patch reddedildi")
        self.path = path


class TestPatchWorkspace(Protocol):
    """Gercek kaynak kokunde atomik uygulama/geri alma; kopya/worktree yoktur."""

    def digest_paths(self, paths: Iterable[str]) -> dict[str, str | None]: ...

    def read_bytes(self, paths: Iterable[str]) -> dict[str, bytes]:
        """Mevcut dosyalarin icerigi (preimage yedegi icin); olmayan yol sonuca girmez."""
        ...

    def apply(self, patch: TestPatch, *, policy: TestOnlyPolicy | None = None) -> AppliedPatch: ...

    def revert(self, applied: AppliedPatch, *, preimages: Mapping[str, bytes]) -> RevertReceipt: ...
