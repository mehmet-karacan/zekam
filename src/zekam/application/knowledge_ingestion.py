"""Ingestion orchestration, guvenli tarama ve kod/DB adapter'lari.

Ingestion sirasinda build, test, hook, paket kurulumu veya submodule guncellemesi
**calistirilmaz**. Tarama bounded'dir; deny list, ignore kurallari, traversal ve
symlink kacisi fail-closed uygulanir.
"""

from __future__ import annotations

import ast
import re
import stat
import struct
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from zekam.application.knowledge_parsers import ParserRouter
from zekam.domain.canonical import digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import (
    Artifact,
    CodeSymbol,
    ContentUnit,
    DatabaseObject,
    IngestionJob,
    IngestionStage,
    Locator,
    NormalizedDocument,
    ScanDecision,
    ScanLimits,
    SourceFormat,
    SourceVersion,
    UnitKind,
    VersionState,
    assert_safe_relative,
    is_denied,
)

#: Ingestion sirasinda asla acilmayan dizinler.
SKIPPED_DIRECTORIES = frozenset(
    {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
)

#: Metin disi kabul edilen uzantilar; icerik olarak ingest edilmez.
BINARY_SUFFIXES = frozenset(
    {".exe", ".dll", ".so", ".dylib", ".class", ".jar", ".pyc", ".bin", ".zip", ".gz", ".7z"}
)

# `package body` iki kelimelik bir turdur; `body` nesne adi degildir.
_PLSQL_OBJECT = re.compile(
    r"create\s+(?:or\s+replace\s+)?"
    r"(?P<kind>package\s+body|package|procedure|function|trigger|view|materialized\s+view)\s+"
    r"(?:(?P<schema>[a-z0-9_]+)\.)?(?P<name>[a-z0-9_]+)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ScanReport:
    """Tarama karari: neyin alindigi ve neyin neden alinmadigi."""

    decisions: tuple[ScanDecision, ...]
    total_bytes: int

    @property
    def included(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.decisions if item.included)

    @property
    def excluded(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.decisions if not item.included)

    def reason_for(self, path: str) -> str:
        for item in self.decisions:
            if item.path == path:
                return item.reason
        raise KeyError(path)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decisions": [item.as_dict() for item in self.decisions],
            "total_bytes": self.total_bytes,
            "included_count": len(self.included),
            "excluded_count": len(self.excluded),
        }


@dataclass(frozen=True, slots=True)
class DirectoryScanner:
    """Izinli kok altinda bounded, calistirmasiz dizin taramasi."""

    allowed_root: Path
    limits: ScanLimits = field(default_factory=ScanLimits)
    ignore_names: frozenset[str] = frozenset()

    def scan(self) -> ScanReport:
        root = self.allowed_root.resolve()
        if not root.is_dir():
            raise PolicyViolation("izinli kok bulunamadi")
        decisions: list[ScanDecision] = []
        total = 0
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                # Symlink karari resolve() **oncesinde** verilir; kok disina cikan
                # bir hedef aksi halde goreli yolu bozar ve gerekce kaybolur.
                relative = path.relative_to(root).as_posix()
                decisions.append(ScanDecision(relative, False, "symlink izlenmez"))
                continue
            if path.is_dir():
                continue
            try:
                relative = path.resolve().relative_to(root).as_posix()
            except ValueError:
                decisions.append(
                    ScanDecision(path.name, False, "izinli kok disina cikan yol reddedildi")
                )
                continue
            decision = self._decide(path, relative)
            decisions.append(decision)
            if decision.included:
                total += path.stat().st_size
        if total > self.limits.max_total_bytes:
            raise PolicyViolation("dizin toplam boyut sinirini asiyor")
        if sum(1 for item in decisions if item.included) > self.limits.max_entries:
            raise PolicyViolation("dizin girdi sayisi sinirini asiyor")
        return ScanReport(tuple(decisions), total)

    def _decide(self, path: Path, relative: str) -> ScanDecision:
        if path.is_symlink():
            return ScanDecision(relative, False, "symlink izlenmez")
        parts = set(Path(relative).parts)
        if parts & SKIPPED_DIRECTORIES:
            return ScanDecision(relative, False, "atlanan dizin")
        if is_denied(relative):
            return ScanDecision(relative, False, "deny list: secret veya kimlik dosyasi")
        if path.name in self.ignore_names:
            return ScanDecision(relative, False, "ignore kurali")
        if path.suffix.lower() in BINARY_SUFFIXES:
            return ScanDecision(relative, False, "ikili dosya icerik olarak alinmaz")
        return ScanDecision(relative, True, "izinli")


@dataclass(frozen=True, slots=True)
class ArchiveInspector:
    """Arsivi **acmadan** inceler; zip bomb ve traversal fail-closed.

    Limitler arsiv metadata'si okunurken uygulanir; buyuk infolist/getmembers
    listesi bellege alinmadan kayit kayit yurunur. Guvenli olmayan her girdi
    fail-closed reddedilir: symlink, absolute/drive yolu, dotdot traversal,
    backslash ve yinelenen adlar.
    """

    limits: ScanLimits = field(default_factory=ScanLimits)

    def inspect(self, archive: Path) -> ScanReport:
        if archive.suffix.lower() == ".zip":
            entries = self._zip(archive)
        elif archive.suffixes[-2:] in ([".tar", ".gz"], [".tar", ".bz2"]) or archive.suffix in {
            ".tar",
            ".tgz",
        }:
            entries = self._tar(archive)
        else:
            raise PolicyViolation("desteklenmeyen arsiv turu")

        total = sum(size for _, size, _ in entries)
        self.limits.assert_within(
            entries=len(entries),
            total_bytes=total,
            compressed_bytes=archive.stat().st_size,
        )
        decisions: list[ScanDecision] = []
        seen: set[str] = set()
        for name, _, is_link in entries:
            if name in seen:
                # Yinelenen ad, iki ayri icerigin ayni yola yazilmasi; hangisi
                # gecerli oldugu belirsizdir. Tum arsiv fail-closed reddedilir.
                raise PolicyViolation("arsiv yinelenen girdi adi tasiyor")
            seen.add(name)
            if is_link:
                decisions.append(ScanDecision(name, False, "arsiv baglantisi reddedildi"))
                continue
            if (
                name.startswith("/")
                or ".." in PurePosixPath(name).parts
                or "\\" in name
                or re.match(r"^[A-Za-z]:", name)
            ):
                decisions.append(ScanDecision(name, False, "arsiv traversal girdisi reddedildi"))
                continue
            if is_denied(name):
                decisions.append(ScanDecision(name, False, "deny list"))
                continue
            decisions.append(ScanDecision(name, True, "izinli"))
        return ScanReport(tuple(decisions), total)

    def _zip(self, archive: Path) -> list[tuple[str, int, bool]]:
        """Read ZIP metadata with bounded reads, preserving raw entry names."""
        entries: list[tuple[str, int, bool]] = []
        total = 0
        archive_bytes = archive.stat().st_size
        with archive.open("rb") as stream:
            # EOCD is at most 22 + 65535 bytes from the end (ZIP comment).
            tail_size = min(archive_bytes, 22 + 65535)
            stream.seek(archive_bytes - tail_size)
            tail = stream.read(tail_size)
            eocd = tail.rfind(b"PK\x05\x06")
            if eocd < 0 or eocd + 22 > len(tail):
                raise PolicyViolation("gecersiz ZIP arsivi")
            disk, central_disk, disk_count, count, central_size, central_offset, comment = (
                struct.unpack_from("<4H2IH", tail, eocd + 4)
            )
            if (
                disk != 0
                or central_disk != 0
                or disk_count != count
                or count == 0xFFFF
                or central_size == 0xFFFFFFFF
                or central_offset == 0xFFFFFFFF
                or eocd + 22 + comment != len(tail)
                or central_offset + central_size != archive_bytes - tail_size + eocd
            ):
                raise PolicyViolation("desteklenmeyen veya gecersiz ZIP dizini")
            if count > self.limits.max_entries:
                raise PolicyViolation("arsiv girdi sayisi sinirini asiyor")
            central_end = central_offset + central_size
            stream.seek(central_offset)
            for _ in range(count):
                fixed = stream.read(46)
                if len(fixed) != 46 or fixed[:4] != b"PK\x01\x02":
                    raise PolicyViolation("gecersiz ZIP merkezi dizini")
                flags = struct.unpack_from("<H", fixed, 8)[0]
                packed, size = struct.unpack_from("<II", fixed, 20)
                name_len, extra_len, comment_len = struct.unpack_from("<3H", fixed, 28)
                mode, offset = struct.unpack_from("<II", fixed, 38)
                if (
                    name_len > 4096
                    or size == 0xFFFFFFFF
                    or packed == 0xFFFFFFFF
                    or offset == 0xFFFFFFFF
                    or stream.tell() + name_len + extra_len + comment_len > central_end
                ):
                    raise PolicyViolation("ZIP metadata siniri veya ZIP64 destegi asildi")
                if flags & 0x0001:
                    # Sifreli girdinin icerigi bu metadata akisinda dogrulanamaz;
                    # sifreli arsivleri izinli gostermek guvenli olmayan bir
                    # eligibility karari uretir. Fail-closed reddet.
                    raise PolicyViolation("sifreli ZIP girdisi desteklenmiyor")
                name_bytes = stream.read(name_len)
                if len(name_bytes) != name_len:
                    raise PolicyViolation("gecersiz ZIP girdi adi")
                stream.seek(extra_len + comment_len, 1)
                central_cursor = stream.tell()
                try:
                    name = name_bytes.decode("utf-8" if flags & 0x800 else "cp437")
                except UnicodeDecodeError as exc:
                    raise PolicyViolation("gecersiz ZIP girdi adi") from exc
                total += size
                self.limits.assert_within(
                    entries=len(entries) + 1,
                    total_bytes=total,
                    compressed_bytes=archive_bytes,
                )
                if size and (packed == 0 or size > packed * self.limits.max_compression_ratio):
                    raise PolicyViolation("arsiv girdi sikistirma orani sinirini asiyor")
                if offset + 30 > central_offset:
                    raise PolicyViolation("gecersiz ZIP yerel girdisi")
                stream.seek(offset)
                local = stream.read(30)
                if len(local) != 30 or local[:4] != b"PK\x03\x04":
                    raise PolicyViolation("gecersiz ZIP yerel girdisi")
                local_name_len, local_extra_len = struct.unpack_from("<HH", local, 26)
                if offset + 30 + local_name_len + local_extra_len + packed > central_offset:
                    raise PolicyViolation("gecersiz ZIP yerel girdi siniri")
                if stream.read(local_name_len) != name_bytes:
                    raise PolicyViolation("arsiv girdi adi tutarsiz")
                stream.seek(central_cursor)
                high_mode = mode >> 16
                entries.append((name, size, high_mode != 0 and stat.S_ISLNK(high_mode)))
            if stream.tell() != central_end:
                raise PolicyViolation("gecersiz ZIP merkezi dizini")
        return entries

    def _tar(self, archive: Path) -> list[tuple[str, int, bool]]:
        entries: list[tuple[str, int, bool]] = []
        total = 0
        archive_bytes = archive.stat().st_size
        with tarfile.open(archive) as bundle:
            # getmembers() tum listeyi bellege almadan streaming yurunur;
            # limit asimi metadata okunurken kesilir.
            for item in bundle:
                total += item.size
                self.limits.assert_within(
                    entries=len(entries) + 1,
                    total_bytes=total,
                    compressed_bytes=archive_bytes,
                )
                entries.append((item.name, item.size, item.issym() or item.islnk()))
        return entries


@dataclass(frozen=True, slots=True)
class PythonSymbolExtractor:
    """AST temelli sembol cikarimi. Kod **calistirilmaz**, yalniz ayristirilir."""

    def extract(self, source: str, *, relative_path: str, revision: str) -> tuple[CodeSymbol, ...]:
        assert_safe_relative(relative_path, "kaynak yolu")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            raise ValidationFailed("kaynak ayristirilamadi") from exc

        imports = self._imports(tree)
        symbols: list[CodeSymbol] = []
        for node in ast.walk(tree):
            kind = _symbol_kind(node)
            if kind is None:
                continue
            end = getattr(node, "end_lineno", None) or node.lineno  # type: ignore[attr-defined]
            symbols.append(
                CodeSymbol(
                    name=node.name,  # type: ignore[attr-defined]
                    kind=kind,
                    relative_path=relative_path,
                    line_start=node.lineno,  # type: ignore[attr-defined]
                    line_end=end,
                    revision=revision,
                    dependencies=imports,
                )
            )
        return tuple(sorted(symbols, key=lambda item: (item.line_start, item.name)))

    @staticmethod
    def _imports(tree: ast.AST) -> tuple[str, ...]:
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
        return tuple(sorted(found))


def _symbol_kind(node: ast.AST) -> str | None:
    if isinstance(node, ast.ClassDef):
        return "class"
    if isinstance(node, ast.AsyncFunctionDef):
        return "async-function"
    if isinstance(node, ast.FunctionDef):
        return "function"
    return None


@dataclass(frozen=True, slots=True)
class PlSqlObjectExtractor:
    """PL/SQL kaynagindan nesne bildirimlerini metadata olarak cikarir."""

    def extract(self, source: str, *, revision: str) -> tuple[DatabaseObject, ...]:
        found: list[DatabaseObject] = []
        seen: set[str] = set()
        for match in _PLSQL_OBJECT.finditer(source):
            schema = (match.group("schema") or "public").lower()
            name = match.group("name").lower()
            key = f"{schema}.{name}"
            if key in seen:
                continue
            seen.add(key)
            found.append(
                DatabaseObject(
                    schema_name=schema,
                    object_name=name,
                    object_kind=" ".join(match.group("kind").lower().split()),
                    revision=revision,
                )
            )
        return tuple(found)


def database_units(objects: tuple[DatabaseObject, ...]) -> tuple[ContentUnit, ...]:
    """DB metadata'sini locator tasiyan icerik birimlerine cevirir."""

    units: list[ContentUnit] = []
    for index, item in enumerate(objects):
        columns = ", ".join(item.columns) if item.columns else "-"
        units.append(
            ContentUnit(
                unit_id=f"db-{index}",
                kind=UnitKind.DB_OBJECT,
                text=f"{item.object_kind} {item.qualified_name} (sutunlar: {columns})",
                locator=Locator(object_name=item.qualified_name),
                order=index,
            )
        )
    if not units:
        raise ValidationFailed("DB metadata'si bos")
    return tuple(units)


@dataclass(frozen=True, slots=True)
class IngestionService:
    """Asamali, idempotent ingestion akisi."""

    router: ParserRouter

    def start(
        self, *, job_id: str, source_id: str, artifact: Artifact, idempotency_key: str
    ) -> IngestionJob:
        return IngestionJob(
            job_id=job_id,
            source_id=source_id,
            artifact_digest=artifact.artifact_digest,
            idempotency_key=idempotency_key,
        ).advance(IngestionStage.VALIDATED)

    def store(self, job: IngestionJob) -> IngestionJob:
        return job.advance(IngestionStage.STORED)

    def parse(
        self,
        job: IngestionJob,
        *,
        document_id: str,
        source_format: SourceFormat,
        payload: bytes,
    ) -> tuple[IngestionJob, NormalizedDocument]:
        parser = self.router.resolve(source_format)
        units = parser.parse(payload)
        document = NormalizedDocument(
            document_id=document_id,
            artifact_digest=job.artifact_digest,
            source_format=source_format,
            units=units,
            parser_ref=parser.parser_ref,
            parser_version=parser.parser_version,
            parser_profile=parser.parser_profile,
        )
        return job.advance(IngestionStage.PARSED).advance(IngestionStage.NORMALIZED), document

    def index(self, job: IngestionJob) -> IngestionJob:
        return job.advance(IngestionStage.INDEXED)

    def activate(
        self, job: IngestionJob, version: SourceVersion
    ) -> tuple[IngestionJob, SourceVersion]:
        """Atomik aktivasyon: once ingestion tamamlanir, sonra surum aktif olur."""

        completed = job.advance(IngestionStage.ACTIVATED)
        return completed, version.activate(completed)

    def artifact_for(self, payload: bytes, *, name: str, media_type: str, now: Any) -> Artifact:
        return Artifact(
            artifact_id=digest_of_bytes(payload).removeprefix("sha256:")[:32],
            content_digest=digest_of_bytes(payload),
            byte_size=len(payload),
            media_type=media_type,
            original_name=name,
            stored_at=now,
        )


def pending_version(
    *,
    version_id: str,
    source_id: str,
    revision: int,
    artifact: Artifact,
    content_digest: str,
    now: Any,
) -> SourceVersion:
    return SourceVersion(
        version_id=version_id,
        source_id=source_id,
        revision=revision,
        artifact_digest=artifact.artifact_digest,
        content_digest=content_digest,
        state=VersionState.PENDING,
        created_at=now,
    )
