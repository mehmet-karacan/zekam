"""Harici kaynak agacinin guvenli, salt okunur kesfi.

Guvenlik sinirlari:

- Kok disina cikan yol, symlink veya `..` bileseni reddedilir (fail-closed).
- Symlink, junction ve reparse point'ler asla izlenmez; kok disina veya kendi
  ustune cakan baglantilar fail-closed olarak kesilir (dongu/cikis yok).
- Sistem deny list, `.gitignore` ve `.zekamignore` birlikte uygulanir.
- Ikili ve asiri buyuk dosyalar icerik olarak okunmaz.
- Secret iceren dosyalar indeks disi birakilir; deger hicbir yere yazilmaz.
- Toplam dosya, bayt ve derinlik sinirleri archive-bomb benzeri durumlara
  karsi fail-closed'dir.

Metadata envanteri (WP-04.01/02): Kesif, icerik eligibility'nden ayri, sinirli
bir metadata envanteri tutar. Yoksayilan agaclari (`.git`, `node_modules`,
`.env` ...) icine metadata icin inilir; cocuklarin yolu/turu/buyuklugu
kaydedilir AMA icerikleri okunmaz, digest/secret taramasi yapilmaz, secret
degeri envantere yazilmaz. Ikili, uretilmis ve sinir/asiri dosyalar da metadata
olarak tasinir. Okunamayan dizin, buyuk dizin girdi siniri, derinlik siniri ve
yalnizca global girdi sinirinin ASIMI (cap+1 girdinin varligi kanitlaninca)
envanteri `partial` yapar ve bir devam checkpoint'i uretir; sinirler tarama
SIRASINDA zorlanir (dev dizinler bellege yuklenmez) ve envanter asla yalnizca
`complete` ilan edilmez. Icerik eligibility sinirlari (`max_total_files`,
`max_total_bytes`) envanteri kesmez: envanter tam tarandiysa `complete` kalir;
eligibility sinirlari `truncated` bayragina ve `LIMIT_REACHED` olarak atlanan
icerik girdilerine yansir. `read_content=False` secenegi icerik okumayi
tamamen kapatir; metadata envanteri tutulur, digest/secret/binary tespiti ve
icerik limitleri uygulanmaz. Sinirlar fail-closed dogrulanir: icerik/envanter
sinirlari pozitif olmak zorundadir; `max_depth=0` gecerlidir.

Icerik eligibility karar sozlesmesi (WP-04.01): Eligibility, metadata envanterinden
AYRI bir karar katmidir. Dosya turlerindeki her kesfedilen dosya girdisi icin
`EligibilityDecision` kaydi uretilir (`eligible`/`skipped` + `SkipReason`);
envanter bu kararlari dogrulamak icin girdi bazinda tasinir. Eligible olmayan
girdiler (yoksayim, ikili, buyuk, secret, sinir, derinlik, erisim)
metadata olarak kalinir ama icerigi okunmaz; eligible girdi envanter girdisinde
`is_text` metadata'sini tasir. `files` tuple'i eligible alt kumenin indekse
hazir yapisidir; eligibility karari `files`'in varligindan veya yoklugundan
turetilmez.

Kesif hicbir kosulda kaynak agacina yazmaz, komut calistirmaz, build/test
tetiklemez veya paket kurmaz.
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from zekam.application.ignore_rules import IgnoreMatcher, system_deny_matcher
from zekam.application.secret_detection import SecretFinding, scan_text
from zekam.domain.canonical import DIGEST_PREFIX
from zekam.domain.errors import PolicyViolation
from zekam.domain.knowledge import is_denied

ZEKAM_IGNORE_FILE = ".zekamignore"
GIT_IGNORE_FILE = ".gitignore"

#: Ikili tespiti icin dosyanin basindan okunan boyut.
BINARY_PROBE_BYTES = 8192

#: Eligible dosya icerigi icin kullanilan parca boyutu (bellek sinirli streaming).
_CHUNK_SIZE = 1 << 20

#: Envanterin bellekte tutulacak en fazla girdi sayisi (fail-closed sinir).
MAX_INVENTORY_ENTRIES = 200_000

#: Tek bir dizinden kesifte islenecek en fazla girdi sayisi (fail-closed sinir).
MAX_DIR_ENTRIES = 10_000


class SkipReason(StrEnum):
    """Bir yolun neden disarida birakildigi."""

    IGNORED = "ignored"
    DENY = "deny"
    SYMLINK = "symlink"
    OUTSIDE_ROOT = "outside-root"
    TOO_LARGE = "too-large"
    BINARY = "binary"
    SECRET = "secret"
    UNREADABLE = "unreadable"
    LIMIT_REACHED = "limit-reached"
    CONTENT_DISABLED = "content-disabled"


class FileKind(StrEnum):
    """Envanter girdisinin tasiyacagi tur."""

    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"


class InventoryStatus(StrEnum):
    """Metadata envanterinin tamamlanma durumu."""

    COMPLETE = "complete"
    PARTIAL = "partial"


class StopReason(StrEnum):
    """Envanterin neden `partial` kaldigi (devam checkpoint'inin nedeni)."""

    FILE_LIMIT = "file-limit"
    BYTE_LIMIT = "byte-limit"
    DEPTH_LIMIT = "depth-limit"
    UNREADABLE_DIRECTORY = "unreadable-directory"
    UNREADABLE_FILE = "unreadable-file"
    INVENTORY_CAP = "inventory-cap"
    DIR_ENTRY_CAP = "dir-entry-cap"


_MAX_CHECKPOINT_PATH_BYTES = 4096
_MAX_CHECKPOINT_SNAPSHOT_ENTRIES = 8
_CheckpointSnapshotEntry = tuple[str, str, int, int]


class _InventoryCheckpoint(tuple[str, ...]):
    """Root-bound tuple that remains compatible with the v1 path contract."""

    root_identity_digest: str
    source_snapshot: tuple[_CheckpointSnapshotEntry, ...]

    def __new__(
        cls,
        paths: tuple[str, ...],
        root_identity_digest: str,
        source_snapshot: tuple[_CheckpointSnapshotEntry, ...] = (),
    ) -> _InventoryCheckpoint:
        value = super().__new__(cls, paths)
        value.root_identity_digest = root_identity_digest
        value.source_snapshot = source_snapshot
        return value


@dataclass(frozen=True, slots=True)
class DiscoveryPolicy:
    """Kesif sinirlari. Varsayilanlar fail-closed tarafta secilmistir."""

    max_file_bytes: int = 2 * 1024 * 1024
    max_total_files: int = 50_000
    max_total_bytes: int = 512 * 1024 * 1024
    follow_symlinks: bool = False
    scan_secrets: bool = True
    max_depth: int = 100
    max_inventory_entries: int = MAX_INVENTORY_ENTRIES
    max_dir_entries: int = MAX_DIR_ENTRIES
    read_content: bool = True

    def __post_init__(self) -> None:
        self._validate()

    @staticmethod
    def _require_int(name: str, value: object) -> None:
        """Sinirin tam sayi oldugunu dogrular (bool reddedilir)."""
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"Kesif siniri {name} tam sayi olmali")

    def _validate(self) -> None:
        """Sinirlari fail-closed dogrular.

        Icerik/envanter sinirlari POZITIF olmali; sifir veya negatif deger
        fail-closed olarak reddedilir. `max_depth`'ta 0 gecerlidir (yalniz kok
        seviyesi taranir, alt dizinlere inilmez). `follow_symlinks` sabit
        `False` olmak zorundadir (symlink/junction asla izlenmez). Hata
        mesajlari alan adini tasir; deger veya dosya yolu tasimaz.
        """
        for name, value in (
            ("max_file_bytes", self.max_file_bytes),
            ("max_total_files", self.max_total_files),
            ("max_total_bytes", self.max_total_bytes),
            ("max_depth", self.max_depth),
            ("max_inventory_entries", self.max_inventory_entries),
            ("max_dir_entries", self.max_dir_entries),
        ):
            self._require_int(name, value)
        if self.max_file_bytes <= 0:
            raise ValueError("max_file_bytes pozitif olmali")
        if self.max_total_files <= 0:
            raise ValueError("max_total_files pozitif olmali")
        if self.max_total_bytes <= 0:
            raise ValueError("max_total_bytes pozitif olmali")
        if self.max_depth < 0:
            raise ValueError("max_depth negatif olamaz (0 gecerlidir)")
        if self.max_inventory_entries <= 0:
            raise ValueError("max_inventory_entries pozitif olmali")
        if self.max_dir_entries <= 0:
            raise ValueError("max_dir_entries pozitif olmali")
        if self.follow_symlinks:
            raise ValueError("follow_symlinks fail-closed oldugu icin False olmali")
        if not isinstance(self.read_content, bool):
            raise ValueError("read_content bool olmali")

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_file_bytes": self.max_file_bytes,
            "max_total_files": self.max_total_files,
            "max_total_bytes": self.max_total_bytes,
            "follow_symlinks": self.follow_symlinks,
            "scan_secrets": self.scan_secrets,
            "max_depth": self.max_depth,
            "max_inventory_entries": self.max_inventory_entries,
            "max_dir_entries": self.max_dir_entries,
            "read_content": self.read_content,
        }


@dataclass(frozen=True, slots=True)
class DiscoveredFile:
    """Kesfedilen tek bir dosya (icerik eligibility'li, indekslenebilir)."""

    relative_path: str
    size_bytes: int
    content_digest: str
    is_text: bool

    @property
    def extension(self) -> str:
        _, _, suffix = self.relative_path.rpartition(".")
        return suffix.lower() if "." in self.relative_path else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "size_bytes": self.size_bytes,
            "content_digest": self.content_digest,
            "is_text": self.is_text,
        }


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    """Metadata envanterinde kaydedilen tek bir girdi.

    Yalnizca yol, tur, buyukluk ve eligibility karari tasiyor; icerik,
    digest veya secret deger hicbir kosulda burada bulunmaz. `is_text`
    yalnizca icerigi sinirli okunmus eligible dosyalar icin anlamlidir;
    yoksayilan/okunamayan/kesilen girdilerde `None`dir.
    `eligibility`, dosya girdileri icin ayri eligibility karar sozlesmesinin
    envantere yansiridir; dizin/symlink girdilerde `None`dir.
    """

    relative_path: str
    kind: FileKind
    size_bytes: int
    is_text: bool | None = None
    skip_reason: SkipReason | None = None
    eligibility: EligibilityDecision | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "kind": self.kind.value,
            "size_bytes": self.size_bytes,
            "is_text": self.is_text,
            "skip_reason": self.skip_reason.value if self.skip_reason is not None else None,
            "eligibility": self.eligibility.as_dict() if self.eligibility is not None else None,
        }


@dataclass(frozen=True, slots=True)
class SkippedPath:
    """Disarida birakilan yol."""

    relative_path: str
    reason: SkipReason
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.relative_path, "reason": self.reason.value, "detail": self.detail}


class EligibilityOutcome(StrEnum):
    """Bir dosya girdisinin icerik uygunluk kararinin sonucu."""

    ELIGIBLE = "eligible"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    """Icerik eligibility karar sozlesmesinin tekil kaydi.

    Eligibility metadata envanterinden ayri bir karar katmidir: bu kayit
    girdi bazinda "icerik okunsun mu?" sorusunu cevaplar ve metadata
    envanterinden dogrulanabilir. `outcome=eligible` icin `reason` None'dir
    ve envanter girdisinde `is_text` metadata'si bulunur; `outcome=skipped`
    icin `reason` bir `SkipReason` tasir. Secret degeri, ham icerik veya
    digest bu kayda yazilmaz.
    """

    relative_path: str
    outcome: EligibilityOutcome
    reason: SkipReason | None = None

    @classmethod
    def eligible(cls, relative_path: str) -> EligibilityDecision:
        return cls(relative_path=relative_path, outcome=EligibilityOutcome.ELIGIBLE, reason=None)

    @classmethod
    def skipped(cls, relative_path: str, reason: SkipReason) -> EligibilityDecision:
        return cls(relative_path=relative_path, outcome=EligibilityOutcome.SKIPPED, reason=reason)

    def __post_init__(self) -> None:
        # Fail-closed tutarlilik: eligible kararda neden olamaz, skipped
        # kararda neden zorunludur.
        if self.outcome is EligibilityOutcome.ELIGIBLE and self.reason is not None:
            raise ValueError("Eligible kararda neden bulundurulamaz")
        if self.outcome is EligibilityOutcome.SKIPPED and self.reason is None:
            raise ValueError("Skipped kararda neden zorunludur")

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.relative_path,
            "outcome": self.outcome.value,
            "reason": self.reason.value if self.reason is not None else None,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    """Kesif sonucu."""

    root_label: str
    tree_digest: str
    files: tuple[DiscoveredFile, ...]
    skipped: tuple[SkippedPath, ...]
    secrets: tuple[SecretFinding, ...]
    policy: DiscoveryPolicy
    truncated: bool = False
    extensions: dict[str, int] = field(default_factory=dict)
    eligibility_decisions: tuple[EligibilityDecision, ...] = field(default_factory=tuple)
    inventory: tuple[InventoryEntry, ...] = field(default_factory=tuple)
    inventory_status: InventoryStatus = InventoryStatus.COMPLETE
    inventory_stop_reason: StopReason | None = None
    inventory_checkpoint: tuple[str, ...] = field(default_factory=tuple)
    inventory_bytes: int = 0

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)

    @property
    def inventory_count(self) -> int:
        return len(self.inventory)

    @property
    def inventory_file_paths(self) -> tuple[str, ...]:
        """Envanterdeki dosya (kind=file) girdilerinin sirali yollari."""
        return tuple(
            sorted(item.relative_path for item in self.inventory if item.kind is FileKind.FILE)
        )

    def skipped_by(self, reason: SkipReason) -> tuple[SkippedPath, ...]:
        return tuple(item for item in self.skipped if item.reason is reason)

    def as_dict(self) -> dict[str, Any]:
        counts = dict.fromkeys(FileKind, 0)
        for item in self.inventory:
            counts[item.kind] += 1
        return {
            "root_label": self.root_label,
            "tree_digest": self.tree_digest,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "truncated": self.truncated,
            "extensions": dict(sorted(self.extensions.items())),
            "skipped": [item.as_dict() for item in self.skipped],
            "secrets": [item.as_dict() for item in self.secrets],
            "policy": self.policy.as_dict(),
            "eligibility": {
                "decision_count": len(self.eligibility_decisions),
                "eligible_count": sum(
                    1
                    for item in self.eligibility_decisions
                    if item.outcome is EligibilityOutcome.ELIGIBLE
                ),
                "skipped_count": sum(
                    1
                    for item in self.eligibility_decisions
                    if item.outcome is EligibilityOutcome.SKIPPED
                ),
            },
            "inventory": {
                "status": self.inventory_status.value,
                "stop_reason": (
                    self.inventory_stop_reason.value
                    if self.inventory_stop_reason is not None
                    else None
                ),
                "checkpoint": list(self.inventory_checkpoint),
                "entry_count": self.inventory_count,
                "entries_by_kind": {kind.value: count for kind, count in counts.items()},
                "bytes": self.inventory_bytes,
            },
        }


def _is_binary(payload: bytes) -> bool:
    return b"\x00" in payload[:BINARY_PROBE_BYTES]


def _read_ignore_file(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:  # pragma: no cover - okunamayan ignore dosyasi yoksayilir
        return []


def build_matcher(root: Path) -> IgnoreMatcher:
    """Sistem, `.gitignore` ve `.zekamignore` kurallarini birlestirir."""
    matcher = system_deny_matcher()
    git_ignore = root / GIT_IGNORE_FILE
    if git_ignore.is_file():
        matcher = matcher.extended(IgnoreMatcher.from_lines(_read_ignore_file(git_ignore)))
    product_ignore = root / ZEKAM_IGNORE_FILE
    if product_ignore.is_file():
        matcher = matcher.extended(IgnoreMatcher.from_lines(_read_ignore_file(product_ignore)))
    return matcher


def assert_within_root(candidate: Path, root: Path) -> Path:
    """Yolun kok icinde kaldigini dogrular; aksi halde fail-closed hata verir."""
    resolved_root = root.resolve(strict=True)
    resolved = candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise PolicyViolation("Yol kaynak kokunun disina cikiyor")
    return resolved


def _is_symlink_or_junction(path: Path) -> bool:
    """Yolun symlink/junction/reparse point olup olmadigini dogrular.

    `pathlib.is_symlink()` Windows'ta junction (reparse point) icin `False`
    dondugundan, Windows'ta `lstat` + `FILE_ATTRIBUTE_REPARSE_POINT` ile ikinci
    bir kontrol yapilir. Baglanti hedefi asla cozulmez/cokulmez.
    """
    if path.is_symlink():
        return True
    if os.name == "nt":
        try:
            return bool(
                os.lstat(path).st_file_attributes & stat_module.FILE_ATTRIBUTE_REPARSE_POINT
            )
        except OSError:
            return False
    return False


def resolve_source_root(root: Path) -> Path:
    """Kok reparse/symlink degilse canonical dizin yolunu dondurur.

    Son path bileşenini `resolve()` etmeden once lstat tabanli kontrol ederiz;
    boylece kaynak koku bir symlink veya Windows reparse point ise hedefe
    takip edilmez.
    """

    candidate = Path(os.path.abspath(os.fspath(root)))
    if _is_symlink_or_junction(candidate):
        raise PolicyViolation("Kaynak koku symlink/junction/reparse olmamali")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir():
        raise PolicyViolation("Kaynak koku bir dizin olmali")
    return resolved


def _root_identity_digest(root: Path) -> str:
    normalized = os.path.normcase(str(root))
    identity = os.lstat(root)
    material = f"{normalized}\x00{identity.st_dev}\x00{identity.st_ino}"
    return DIGEST_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _checkpoint_kind(path: Path, info: os.stat_result) -> str:
    if _is_symlink_or_junction(path):
        return FileKind.SYMLINK.value
    if stat_module.S_ISDIR(info.st_mode):
        return FileKind.DIRECTORY.value
    if stat_module.S_ISREG(info.st_mode):
        return FileKind.FILE.value
    return "other"


def _build_checkpoint_snapshot(
    root: Path, checkpoint: tuple[str, ...]
) -> tuple[_CheckpointSnapshotEntry, ...]:
    snapshot: list[_CheckpointSnapshotEntry] = []
    for relative in checkpoint[:_MAX_CHECKPOINT_SNAPSHOT_ENTRIES]:
        candidate = root.joinpath(*PurePosixPath(relative).parts)
        try:
            info = os.lstat(candidate)
        except OSError:
            snapshot.append((relative, "missing", -1, -1))
            continue
        snapshot.append(
            (
                relative,
                _checkpoint_kind(candidate, info),
                int(info.st_size),
                int(info.st_mtime_ns),
            )
        )
    return tuple(snapshot)


def _validate_checkpoint_snapshot(
    root: Path, snapshot: tuple[_CheckpointSnapshotEntry, ...]
) -> None:
    if not isinstance(snapshot, tuple) or len(snapshot) > _MAX_CHECKPOINT_SNAPSHOT_ENTRIES:
        raise PolicyViolation("Discovery checkpoint snapshot bounded siniri asiyor")
    for row in snapshot:
        if not isinstance(row, tuple) or len(row) != 4:
            raise PolicyViolation("Discovery checkpoint snapshot gecersiz")
        relative, expected_kind, expected_size, expected_mtime_ns = row
        if not isinstance(relative, str) or not relative:
            raise PolicyViolation("Discovery checkpoint snapshot yolu gecersiz")
        if len(relative.encode("utf-8")) > _MAX_CHECKPOINT_PATH_BYTES:
            raise PolicyViolation("Discovery checkpoint snapshot yolu bounded siniri asiyor")
        if "\x00" in relative or "\\" in relative:
            raise PolicyViolation("Discovery checkpoint snapshot yolu root-relative olmali")
        posix = PurePosixPath(relative)
        windows = PureWindowsPath(relative)
        if (
            posix.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or posix.as_posix() != relative
            or any(part in {"", ".", ".."} for part in posix.parts)
        ):
            raise PolicyViolation("Discovery checkpoint snapshot yolu root-relative olmali")
        candidate = root.joinpath(*posix.parts)
        try:
            assert_within_root(candidate, root)
            info = os.lstat(candidate)
        except (OSError, PolicyViolation) as exc:
            raise PolicyViolation("Discovery checkpoint source drift") from exc
        if not isinstance(expected_kind, str) or not isinstance(expected_size, int):
            raise PolicyViolation("Discovery checkpoint snapshot gecersiz")
        if not isinstance(expected_mtime_ns, int):
            raise PolicyViolation("Discovery checkpoint snapshot gecersiz")
        if (
            _checkpoint_kind(candidate, info) != expected_kind
            or int(info.st_size) != expected_size
            or int(info.st_mtime_ns) != expected_mtime_ns
        ):
            raise PolicyViolation("Discovery checkpoint source drift")


def _validate_resume_checkpoint(root: Path, checkpoint: tuple[str, ...]) -> None:
    if not isinstance(checkpoint, tuple):
        raise PolicyViolation("Discovery checkpoint tuple olmali")
    recorded_root = getattr(checkpoint, "root_identity_digest", None)
    if recorded_root is not None and recorded_root != _root_identity_digest(root):
        raise PolicyViolation("Discovery checkpoint source root drift")
    if len(checkpoint) > 1:
        raise PolicyViolation("Discovery checkpoint tek root-relative yol tasimali")
    for relative in checkpoint:
        if not isinstance(relative, str) or not relative:
            raise PolicyViolation("Discovery checkpoint yolu gecersiz")
        if len(relative.encode("utf-8")) > _MAX_CHECKPOINT_PATH_BYTES:
            raise PolicyViolation("Discovery checkpoint yolu bounded siniri asiyor")
        if "\x00" in relative or "\\" in relative:
            raise PolicyViolation("Discovery checkpoint yolu root-relative olmali")
        posix = PurePosixPath(relative)
        windows = PureWindowsPath(relative)
        if (
            posix.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or posix.as_posix() != relative
            or any(part in {"", ".", ".."} for part in posix.parts)
        ):
            raise PolicyViolation("Discovery checkpoint yolu root-relative olmali")
        candidate = root.joinpath(*posix.parts)
        try:
            assert_within_root(candidate, root)
        except (OSError, PolicyViolation) as exc:
            raise PolicyViolation("Discovery checkpoint source root drift") from exc
        if not candidate.exists():
            raise PolicyViolation("Discovery checkpoint source drift")
        cursor = root
        for part in posix.parts:
            cursor /= part
            if _is_symlink_or_junction(cursor):
                raise PolicyViolation("Discovery checkpoint symlink/junction reddedildi")
    snapshot = getattr(checkpoint, "source_snapshot", None)
    if snapshot is not None:
        _validate_checkpoint_snapshot(root, snapshot)


class _DirectoryTooLarge(Exception):
    """Tek bir dizin, `max_dir_entries` sinirini asiyor."""


def _list_bounded(directory: Path, limit: int) -> list[Path]:
    """Tek dizinin girdilerini kararli (isim) sirayla ve SINIRLI listeler.

    `limit` girdi toplaninca dizin okumasi durur; dev dizinler bellege
    yuklenmez ve `_DirectoryTooLarge` firlatilir (tarama partial kesilir).
    """
    entries: list[Path] = []
    for item in directory.iterdir():
        if len(entries) >= limit:
            raise _DirectoryTooLarge(str(directory))
        entries.append(item)
    entries.sort(key=lambda path: path.name)
    return entries


@dataclass
class _InventoryBuilder:
    """Tarama sirasinda sinirli envanter toplayici.

    Global girdi siniri tarama sirasinda (sonradan kesme degil) zorlanir;
    sinir asilirsa `cap_stopped` kurulur ve tarama kesilir.
    """

    cap: int
    entries: list[InventoryEntry] = field(default_factory=list)
    cap_stopped: bool = False
    last_path: str | None = None

    def add(
        self,
        relative: str,
        kind: FileKind,
        size: int,
        skip_reason: SkipReason | None = None,
        *,
        is_text: bool | None = None,
        eligibility: EligibilityDecision | None = None,
    ) -> bool:
        """Girdi ekler; global sinir asilirsa True doner (tarama kesilmeli)."""
        if len(self.entries) >= self.cap:
            self.cap_stopped = True
            self.last_path = relative
            return True
        self.entries.append(
            InventoryEntry(
                relative_path=relative,
                kind=kind,
                size_bytes=size,
                is_text=is_text,
                skip_reason=skip_reason,
                eligibility=eligibility,
            )
        )
        self.last_path = relative
        return False


def _process_file(
    resolved: Path,
    relative: str,
    size: int,
    *,
    policy: DiscoveryPolicy,
    files: list[DiscoveredFile],
    skipped: list[SkippedPath],
    secrets: list[SecretFinding],
    extensions: dict[str, int],
) -> tuple[int, DiscoveredFile | None]:
    """Tek bir eligible dosyayi byte sinirli okuyup isler.

    Donen kope `(okunan_bayt, eldisi_dosya)`. `eldisi_dosya` `None` ise dosya
    eligibility disinda birakilmistir (orn. secret bulgusu) ve `okunan_bayt` 0'dir.
    Sinir kontrolu cagiran `discover` fonksiyonunda yapilir.
    """
    hasher = hashlib.sha256()
    payload = bytearray()
    with resolved.open("rb") as handle:
        while True:
            chunk = handle.read(min(_CHUNK_SIZE, size + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > size or len(payload) > policy.max_file_bytes:
                raise OSError("Kaynak dosya okuma sirasinda buyudu")
            hasher.update(chunk)
    if len(payload) != size:
        raise OSError("Kaynak dosya okuma sirasinda degisti")
    probe = bytes(payload[:BINARY_PROBE_BYTES])
    digest = DIGEST_PREFIX + hasher.hexdigest()

    if _is_binary(probe):
        discovered = DiscoveredFile(
            relative_path=relative, size_bytes=size, content_digest=digest, is_text=False
        )
        files.append(discovered)
        _count_extension(extensions, relative)
        return size, discovered

    text = payload.decode("utf-8", errors="replace")

    if policy.scan_secrets:
        found = scan_text(text, relative_path=relative)
        if found:
            secrets.extend(found)
            skipped.append(SkippedPath(relative, SkipReason.SECRET, f"{len(found)} bulgu"))
            return 0, None

    discovered = DiscoveredFile(
        relative_path=relative, size_bytes=size, content_digest=digest, is_text=True
    )
    files.append(discovered)
    _count_extension(extensions, relative)
    return size, discovered


def discover(
    root: Path,
    *,
    policy: DiscoveryPolicy | None = None,
    matcher: IgnoreMatcher | None = None,
) -> DiscoveryReport:
    """Kaynak agacini salt okunur olarak tarar.

    Icerik eligibility (`files`) ve metadata envanteri (`inventory`) ayni
    taramada uretilir ama bamsiz kalir: envanter yoksayilan/ikili/uretilmis
    girdiler dahil tum kesfedilen girdileri metadata olarak tutar; eligibility
    ise yalnizca icerigi sinirli okunup indekslenebilir dosyalari kapsar.
    """
    active_policy = policy or DiscoveryPolicy()
    resolved_root = resolve_source_root(root)
    rules = matcher or build_matcher(resolved_root)

    files: list[DiscoveredFile] = []
    skipped: list[SkippedPath] = []
    secrets: list[SecretFinding] = []
    extensions: dict[str, int] = {}
    total_bytes = 0
    truncated = False

    # Icerik eligibility sinirleri: dosya siniri asilirdi mi, bayt siniri mi?
    content_limit: StopReason | None = None
    depth_stopped = False
    unreadable_directory: str | None = None
    unreadable_file: str | None = None
    dir_cap_directory: str | None = None
    last_eligible_path: str | None = None
    last_pending_path: str | None = None

    builder = _InventoryBuilder(cap=active_policy.max_inventory_entries)

    # BFS kuyrugu. Her girdi: (yol, derinlik, bagli_yoksayim). Derinlik kok'e
    # gore yol bilesen sayisidir (kok 0, README.md 1, src/ana.py 2).
    # `bagli_yoksayim`, girdinin bir yoksayilan dizin icinde oldugunu tasir;
    # bu girdiler icerik olarak okunmaz ama metadata envanteri gorunur kalir.
    queue: deque[tuple[Path, int, bool]] = deque([(resolved_root, 0, False)])
    while (
        queue
        and unreadable_directory is None
        and dir_cap_directory is None
        and not builder.cap_stopped
    ):
        directory, depth, dir_ignored = queue.popleft()
        relative = directory.relative_to(resolved_root).as_posix()
        try:
            entries = _list_bounded(directory, active_policy.max_dir_entries)
        except _DirectoryTooLarge:
            # Dizin cok buyuk: sinirla okunamadi, tarama partial kesilir
            # (devam checkpoint'i uretilir, sahte complete yok).
            if not dir_ignored:
                skipped.append(SkippedPath(relative, SkipReason.LIMIT_REACHED, "buyuk dizin"))
            if builder.add(relative, FileKind.DIRECTORY, 0, skip_reason=SkipReason.LIMIT_REACHED):
                break
            dir_cap_directory = relative
            break
        except OSError:
            # Okunamayan dizin: metadata olarak kaydedilir, tarama durur ve
            # envanteri partial yapan bir devam checkpoint'i uretilir (sesli
            # fail-closed; hata sessiz yutulmaz).
            if builder.add(relative, FileKind.DIRECTORY, 0, skip_reason=SkipReason.UNREADABLE):
                break
            unreadable_directory = relative
            break

        # Izlenen (normal) dizin envantere metadata olarak girer (pop aninda).
        # Koku (".") bir "kesfedilen girdi" degildir; kok tasinmaz. Push aninda
        # kayit yapilmasinin tek istisnasi derinlik sinirinde kalan dizindir.
        if relative != "." and builder.add(
            relative,
            FileKind.DIRECTORY,
            0,
            skip_reason=SkipReason.IGNORED if dir_ignored else None,
        ):
            break

        for entry in reversed(entries):
            entry_relative = entry.relative_to(resolved_root).as_posix()

            # Bir sonraki girdi envantere sigmayacaksa icerigine dokunma.
            if len(builder.entries) >= builder.cap:
                builder.cap_stopped = True
                builder.last_path = entry_relative
                break

            if _is_symlink_or_junction(entry):
                # Symlink/junction asla izlenmez; hedef cozulmez (fail-closed).
                skipped.append(
                    SkippedPath(entry_relative, SkipReason.SYMLINK, "symlink/junction izlenmiyor")
                )
                if builder.add(
                    entry_relative,
                    FileKind.SYMLINK,
                    0,
                    skip_reason=SkipReason.SYMLINK,
                ):
                    break
                continue

            if entry.is_dir():
                if depth + 1 > active_policy.max_depth:
                    # Derinlik siniri: bu dizinin icine inilmez (icerigi daha
                    # derin olurdu), envanteri partial yap. Kayit burada yapilir
                    # (dizin kuyruga konulmadigi icin pop aninda gorusmez);
                    # derinlik yoksayim durumundan bagimsiz ayni sekilde uygulanir.
                    if builder.add(
                        entry_relative,
                        FileKind.DIRECTORY,
                        0,
                        skip_reason=SkipReason.LIMIT_REACHED,
                    ):
                        break
                    if not depth_stopped:
                        depth_stopped = True
                        last_pending_path = entry_relative
                    continue
                if rules.is_path_ignored(entry_relative, is_directory=True):
                    # Yoksayilan dizin: icine inilir AMA bagli yoksayim flag'i
                    # ile; cocuklar metadata olarak tasinir, icerikleri okunmaz,
                    # digest/secret taramasi yapilmaz. Kayit pop aninda yapilir.
                    skipped.append(
                        SkippedPath(entry_relative, SkipReason.IGNORED, "yoksayma kurali")
                    )
                    queue.append((entry, depth + 1, True))
                    continue
                # Izlenen normal dizin: bagli yoksayim flag'i alt agaca
                # tasinir; kayit pop aninda yapilir.
                queue.append((entry, depth + 1, dir_ignored))
                continue

            # Dosya.
            if (
                is_denied(entry_relative)
                or rules.is_path_ignored(entry_relative, is_directory=False)
                or dir_ignored
            ):
                # Yoksayilan dosya (orn. `.env` veya yoksayilan agac icindeki
                # dosya): metadata olarak kaydedilir, icerigi okunmaz, secret
                # degeri envantere yazilmaz, digest/secret taramasi yok.
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                skipped.append(SkippedPath(entry_relative, SkipReason.IGNORED, "yoksayma kurali"))
                decision = EligibilityDecision.skipped(
                    entry_relative,
                    SkipReason.DENY if is_denied(entry_relative) else SkipReason.IGNORED,
                )
                if builder.add(
                    entry_relative,
                    FileKind.FILE,
                    size,
                    skip_reason=SkipReason.IGNORED,
                    eligibility=decision,
                ):
                    break
                continue

            if depth + 1 > active_policy.max_depth:
                # Derinlik siniri: dosyaya kadar inilmez, envanteri partial yap.
                decision = EligibilityDecision.skipped(entry_relative, SkipReason.LIMIT_REACHED)
                if builder.add(
                    entry_relative,
                    FileKind.FILE,
                    0,
                    skip_reason=SkipReason.LIMIT_REACHED,
                    eligibility=decision,
                ):
                    break
                if not depth_stopped:
                    depth_stopped = True
                    last_pending_path = entry_relative
                continue

            try:
                resolved = assert_within_root(entry, resolved_root)
            except PolicyViolation:
                skipped.append(
                    SkippedPath(entry_relative, SkipReason.OUTSIDE_ROOT, "kok disi hedef")
                )
                decision = EligibilityDecision.skipped(entry_relative, SkipReason.OUTSIDE_ROOT)
                if builder.add(
                    entry_relative,
                    FileKind.FILE,
                    0,
                    skip_reason=SkipReason.OUTSIDE_ROOT,
                    eligibility=decision,
                ):
                    break
                continue

            try:
                file_stat = resolved.stat()
                if not stat_module.S_ISREG(file_stat.st_mode):
                    raise OSError("Duzenli dosya degil")
                size = file_stat.st_size
            except OSError:
                skipped.append(SkippedPath(entry_relative, SkipReason.UNREADABLE, "stat basarisiz"))
                decision = EligibilityDecision.skipped(entry_relative, SkipReason.UNREADABLE)
                if builder.add(
                    entry_relative,
                    FileKind.FILE,
                    0,
                    skip_reason=SkipReason.UNREADABLE,
                    eligibility=decision,
                ):
                    break
                if unreadable_file is None:
                    unreadable_file = entry_relative
                continue

            if not active_policy.read_content:
                skipped.append(
                    SkippedPath(
                        entry_relative,
                        SkipReason.CONTENT_DISABLED,
                        "icerik okuma politikasi kapali",
                    )
                )
                decision = EligibilityDecision.skipped(entry_relative, SkipReason.CONTENT_DISABLED)
                if builder.add(
                    entry_relative,
                    FileKind.FILE,
                    size,
                    skip_reason=SkipReason.CONTENT_DISABLED,
                    eligibility=decision,
                ):
                    break
                continue

            if len(files) >= active_policy.max_total_files:
                if content_limit is None:
                    content_limit = StopReason.FILE_LIMIT
                truncated = True
                skipped.append(
                    SkippedPath(entry_relative, SkipReason.LIMIT_REACHED, "dosya siniri")
                )
                decision = EligibilityDecision.skipped(entry_relative, SkipReason.LIMIT_REACHED)
                if builder.add(
                    entry_relative,
                    FileKind.FILE,
                    size,
                    skip_reason=SkipReason.LIMIT_REACHED,
                    eligibility=decision,
                ):
                    break
                last_pending_path = entry_relative
                continue

            if size > active_policy.max_file_bytes:
                skipped.append(SkippedPath(entry_relative, SkipReason.TOO_LARGE, f"{size} bayt"))
                decision = EligibilityDecision.skipped(entry_relative, SkipReason.TOO_LARGE)
                if builder.add(
                    entry_relative,
                    FileKind.FILE,
                    size,
                    skip_reason=SkipReason.TOO_LARGE,
                    eligibility=decision,
                ):
                    break
                continue

            if total_bytes + size > active_policy.max_total_bytes:
                if content_limit is None:
                    content_limit = StopReason.BYTE_LIMIT
                truncated = True
                skipped.append(
                    SkippedPath(entry_relative, SkipReason.LIMIT_REACHED, "toplam bayt siniri")
                )
                decision = EligibilityDecision.skipped(entry_relative, SkipReason.LIMIT_REACHED)
                if builder.add(
                    entry_relative,
                    FileKind.FILE,
                    size,
                    skip_reason=SkipReason.LIMIT_REACHED,
                    eligibility=decision,
                ):
                    break
                last_pending_path = entry_relative
                continue

            if content_limit is not None:
                # Dosya/bayt eligibility siniri asildi: envanter metadata olarak
                # tasinir (gercek buyuklukle) ama icerik okuma durur.
                reason = (
                    "dosya siniri"
                    if content_limit is StopReason.FILE_LIMIT
                    else "toplam bayt siniri"
                )
                skipped.append(SkippedPath(entry_relative, SkipReason.LIMIT_REACHED, reason))
                decision = EligibilityDecision.skipped(entry_relative, SkipReason.LIMIT_REACHED)
                if builder.add(
                    entry_relative,
                    FileKind.FILE,
                    size,
                    skip_reason=SkipReason.LIMIT_REACHED,
                    eligibility=decision,
                ):
                    break
                last_pending_path = entry_relative
                continue

            try:
                read_bytes, discovered = _process_file(
                    resolved,
                    entry_relative,
                    size=size,
                    policy=active_policy,
                    files=files,
                    skipped=skipped,
                    secrets=secrets,
                    extensions=extensions,
                )
            except OSError:
                skipped.append(
                    SkippedPath(entry_relative, SkipReason.UNREADABLE, "okuma basarisiz")
                )
                decision = EligibilityDecision.skipped(entry_relative, SkipReason.UNREADABLE)
                if builder.add(
                    entry_relative,
                    FileKind.FILE,
                    size,
                    skip_reason=SkipReason.UNREADABLE,
                    eligibility=decision,
                ):
                    break
                if unreadable_file is None:
                    unreadable_file = entry_relative
                continue

            if discovered is None:
                # Eligibility disinda (orn. secret); envantere metadata olarak girer.
                decision = EligibilityDecision.skipped(entry_relative, SkipReason.SECRET)
                if builder.add(
                    entry_relative,
                    FileKind.FILE,
                    size,
                    skip_reason=SkipReason.SECRET,
                    eligibility=decision,
                ):
                    break
                continue

            total_bytes += read_bytes
            last_eligible_path = entry_relative
            decision = EligibilityDecision.eligible(entry_relative)
            if builder.add(
                entry_relative,
                FileKind.FILE,
                size,
                is_text=discovered.is_text,
                eligibility=decision,
            ):
                break

    inventory, inventory_status, inventory_stop_reason, inventory_bytes = _assemble_inventory(
        builder.entries,
        cap_stopped=builder.cap_stopped,
        depth_stopped=depth_stopped,
        unreadable_directory=unreadable_directory,
        unreadable_file=unreadable_file,
        dir_cap_directory=dir_cap_directory,
    )
    checkpoint = _build_checkpoint(
        inventory_status=inventory_status,
        content_limit=content_limit,
        depth_stopped=depth_stopped,
        unreadable_directory=unreadable_directory,
        unreadable_file=unreadable_file,
        dir_cap_directory=dir_cap_directory,
        last_eligible_path=last_eligible_path,
        last_pending_path=last_pending_path,
        inventory=inventory,
        cap_pending_path=builder.last_path if builder.cap_stopped else None,
    )
    if checkpoint:
        checkpoint = _InventoryCheckpoint(
            checkpoint,
            _root_identity_digest(resolved_root),
            _build_checkpoint_snapshot(resolved_root, checkpoint),
        )

    return DiscoveryReport(
        root_label=resolved_root.name,
        tree_digest=compute_tree_digest(files),
        files=tuple(sorted(files, key=lambda item: item.relative_path)),
        skipped=tuple(sorted(skipped, key=lambda item: item.relative_path)),
        secrets=tuple(secrets),
        policy=active_policy,
        truncated=truncated,
        extensions=extensions,
        eligibility_decisions=tuple(
            sorted(
                (entry.eligibility for entry in inventory if entry.eligibility is not None),
                key=lambda item: item.relative_path,
            )
        ),
        inventory=inventory,
        inventory_status=inventory_status,
        inventory_stop_reason=inventory_stop_reason,
        inventory_checkpoint=checkpoint,
        inventory_bytes=inventory_bytes,
    )


def resume_discovery(
    root: Path,
    checkpoint: tuple[str, ...],
    *,
    policy: DiscoveryPolicy | None = None,
    matcher: IgnoreMatcher | None = None,
) -> DiscoveryReport:
    """Resume a bounded discovery from a root-bound inventory checkpoint.

    Discovery is provider-free and read-only.  The checkpoint is validated
    against the current source root and its bounded frontier snapshot before a
    fresh bounded traversal rebuilds the complete report under the supplied
    policy.  A fresh traversal is intentional because the public checkpoint
    contract carries only a root-relative frontier, not a partial inventory.
    """

    resolved_root = resolve_source_root(root)
    _validate_resume_checkpoint(resolved_root, checkpoint)
    return discover(resolved_root, policy=policy, matcher=matcher)


def _assemble_inventory(
    entries: list[InventoryEntry],
    *,
    cap_stopped: bool,
    depth_stopped: bool,
    unreadable_directory: str | None,
    unreadable_file: str | None,
    dir_cap_directory: str | None,
) -> tuple[tuple[InventoryEntry, ...], InventoryStatus, StopReason | None, int]:
    """Envanter girdilerini siralar ve durumu belirler.

    Global girdi siniri tarama SIRASINDA `cap_stopped` ile belirlenir; burada
    yeniden kesme (slice) yapilmaz.
    """
    ordered = tuple(sorted(entries, key=lambda item: item.relative_path))
    inventory_bytes = sum(item.size_bytes for item in ordered if item.kind is FileKind.FILE)

    if cap_stopped:
        status, stop_reason = InventoryStatus.PARTIAL, StopReason.INVENTORY_CAP
    elif unreadable_directory is not None:
        status, stop_reason = InventoryStatus.PARTIAL, StopReason.UNREADABLE_DIRECTORY
    elif unreadable_file is not None:
        status, stop_reason = InventoryStatus.PARTIAL, StopReason.UNREADABLE_FILE
    elif dir_cap_directory is not None:
        status, stop_reason = InventoryStatus.PARTIAL, StopReason.DIR_ENTRY_CAP
    elif depth_stopped:
        status, stop_reason = InventoryStatus.PARTIAL, StopReason.DEPTH_LIMIT
    else:
        status, stop_reason = InventoryStatus.COMPLETE, None

    return ordered, status, stop_reason, inventory_bytes


def _build_checkpoint(
    *,
    inventory_status: InventoryStatus,
    content_limit: StopReason | None,
    depth_stopped: bool,
    unreadable_directory: str | None,
    unreadable_file: str | None,
    dir_cap_directory: str | None,
    last_eligible_path: str | None,
    last_pending_path: str | None,
    inventory: tuple[InventoryEntry, ...],
    cap_pending_path: str | None,
) -> tuple[str, ...]:
    """Devam checkpoint'ini (yeniden baslatilacak son nokta) belirler.

    `complete` envanter icin bos tuple doner (devam edilecek nokta yok). Aksi
    halde taramanin kesildigi son nokta verilir: okunamayan/buyuk dizin,
    derinlik sinirindeki girdi, sinir nedeniyle bekleyen dosya ya da envanterin
    son girdisi.
    """
    if inventory_status is not InventoryStatus.PARTIAL:
        return ()
    if cap_pending_path is not None:
        return (cap_pending_path,)
    if unreadable_directory is not None:
        return (unreadable_directory,)
    if unreadable_file is not None:
        return (unreadable_file,)
    if dir_cap_directory is not None:
        return (dir_cap_directory,)
    if depth_stopped and last_pending_path is not None:
        return (last_pending_path,)
    if content_limit is not None and last_pending_path is not None:
        return (last_pending_path,)
    if last_eligible_path is not None:
        return (last_eligible_path,)
    if not inventory:
        return ()
    return (inventory[-1].relative_path,)


def compute_tree_digest(files: list[DiscoveredFile] | tuple[DiscoveredFile, ...]) -> str:
    """Dosya yolu ve icerik digest'lerinden kararli agac digest'i uretir."""
    hasher = hashlib.sha256()
    for item in sorted(files, key=lambda entry: entry.relative_path):
        hasher.update(item.relative_path.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(item.content_digest.encode("ascii"))
        hasher.update(b"\n")
    return DIGEST_PREFIX + hasher.hexdigest()


def _count_extension(counter: dict[str, int], relative: str) -> None:
    name = os.path.basename(relative)
    suffix = name.rpartition(".")[2].lower() if "." in name else ""
    key = suffix or "(uzantisiz)"
    counter[key] = counter.get(key, 0) + 1
