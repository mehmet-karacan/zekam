"""Ingestion orchestration, guvenli tarama ve kod/DB adapter'lari.

Ingestion sirasinda build, test, hook, paket kurulumu veya submodule guncellemesi
**calistirilmaz**. Tarama bounded'dir; deny list, ignore kurallari, traversal ve
symlink kacisi fail-closed uygulanir.
"""

from __future__ import annotations

import ast
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
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
    """Arsivi **acmadan** inceler; zip bomb ve traversal fail-closed."""

    limits: ScanLimits = field(default_factory=ScanLimits)

    def inspect(self, archive: Path) -> ScanReport:
        if archive.suffix.lower() == ".zip":
            names, sizes = self._zip(archive)
        elif archive.suffixes[-2:] in ([".tar", ".gz"], [".tar", ".bz2"]) or archive.suffix in {
            ".tar",
            ".tgz",
        }:
            names, sizes = self._tar(archive)
        else:
            raise PolicyViolation("desteklenmeyen arsiv turu")

        total = sum(sizes)
        self.limits.assert_within(
            entries=len(names),
            total_bytes=total,
            compressed_bytes=archive.stat().st_size,
        )
        decisions: list[ScanDecision] = []
        for name in names:
            if name.startswith("/") or ".." in Path(name).parts or "\\" in name:
                decisions.append(ScanDecision(name, False, "arsiv traversal girdisi reddedildi"))
                continue
            if is_denied(name):
                decisions.append(ScanDecision(name, False, "deny list"))
                continue
            decisions.append(ScanDecision(name, True, "izinli"))
        return ScanReport(tuple(decisions), total)

    @staticmethod
    def _zip(archive: Path) -> tuple[list[str], list[int]]:
        with zipfile.ZipFile(archive) as bundle:
            infos = bundle.infolist()
        return [item.filename for item in infos], [item.file_size for item in infos]

    @staticmethod
    def _tar(archive: Path) -> tuple[list[str], list[int]]:
        with tarfile.open(archive) as bundle:
            members = bundle.getmembers()
        return [item.name for item in members], [item.size for item in members]


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
