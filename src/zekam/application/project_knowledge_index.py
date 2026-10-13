"""Read-only project source indexing and non-semantic lexical baseline helpers.

The source tree is never modified. Secret-bearing, ignored, binary, oversized and
unsafe files are filtered by :mod:`source_discovery` before this module reads any
content. Feature hashing is only a testable non-semantic baseline; production
vector writes require a verified :class:`EmbeddingProvider`.
"""

from __future__ import annotations

import ast
import datetime as dt
import hashlib
import math
import re
import stat
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

from zekam.application.embedding_provider import (
    EmbeddingPolicy,
    EmbeddingProvider,
    verify_document_embedding_batch,
)
from zekam.application.embedding_routing import (
    EmbeddingRouteCandidate,
    EmbeddingRouteDecision,
    EmbeddingRouteKind,
    select_embedding_route,
)
from zekam.application.knowledge_ingestion import IngestionService, pending_version
from zekam.application.knowledge_parsers import default_router
from zekam.application.source_discovery import (
    DiscoveredFile,
    DiscoveryPolicy,
    DiscoveryReport,
    EligibilityDecision,
    EligibilityOutcome,
    FileKind,
    InventoryStatus,
    discover,
)
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes
from zekam.domain.errors import PolicyViolation, ValidationFailed
from zekam.domain.knowledge import (
    ContentUnit,
    IngestionStage,
    Locator,
    NormalizedDocument,
    SourceFormat,
    UnitKind,
)
from zekam.domain.retrieval import Chunk, ChunkProfile, EmbeddingProfile, estimate_tokens
from zekam.infrastructure.storage.local_cas import LocalContentAddressedStore

NON_SEMANTIC_BASELINE_MODEL_REF = "baseline/feature-hashing-v1-non-semantic"
NON_SEMANTIC_BASELINE_DIMENSION = 1024
REAL_EMBEDDING_MODEL_REF = "openai/BAAI/bge-m3"
REAL_EMBEDDING_DIMENSION = 1024
INDEXER_VERSION = "zekam-project-source-indexer/v2"
MAX_CHUNK_TOKENS = 384
MAX_CHUNK_CHARACTERS = 6000

_TOKEN = re.compile(r"\w+", re.UNICODE)
_SUPPORTED_SUFFIXES = frozenset(
    {
        ".css",
        ".dockerignore",
        ".gitignore",
        ".gradle",
        ".graphql",
        ".groovy",
        ".html",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".jrxml",
        ".kt",
        ".kts",
        ".md",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".scss",
        ".sh",
        ".sql",
        ".swift",
        ".toml",
        ".ts",
        ".tsx",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
    }
)
_SUPPORTED_NAMES = frozenset({"dockerfile", "makefile"})


@dataclass(frozen=True, slots=True)
class ProjectIndexPlan:
    project_id: UUID
    project_slug: str
    source_revision: str
    tree_digest: str
    discovery: DiscoveryReport
    manifest: bytes
    document: NormalizedDocument
    chunk_profile: ChunkProfile
    embedding_profile: EmbeddingProfile
    embedding_route: EmbeddingRouteDecision
    chunks: tuple[Chunk, ...]
    selected_file_count: int
    skipped_unsupported: int
    skipped_encoding: int

    @property
    def plan_digest(self) -> str:
        return str(
            digest(
                {
                    "project_id": str(self.project_id),
                    "source_revision": self.source_revision,
                    "tree_digest": self.tree_digest,
                    "document_digest": self.document.content_digest,
                    "chunk_profile_digest": self.chunk_profile.profile_digest,
                    "embedding_profile_digest": self.embedding_profile.profile_digest,
                    "embedding_route_digest": self.embedding_route.decision_digest,
                    "chunk_count": len(self.chunks),
                }
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "zekam-project-source-index-plan/v1",
            "project_id": str(self.project_id),
            "project_slug": self.project_slug,
            "source_revision": self.source_revision,
            "tree_digest": self.tree_digest,
            "selected_file_count": self.selected_file_count,
            "unit_count": self.document.unit_count,
            "chunk_count": len(self.chunks),
            "skipped_unsupported": self.skipped_unsupported,
            "skipped_encoding": self.skipped_encoding,
            "embedding": {
                "model_ref": self.embedding_profile.model_ref,
                "dimension": self.embedding_profile.dimension,
                "profile_digest": self.embedding_profile.profile_digest,
                "mode": self.embedding_route.kind.value,
                "route": self.embedding_route.sanitized(),
                "remote_provider_used": self.embedding_route.kind
                is EmbeddingRouteKind.QUALIFIED_REMOTE,
            },
            "plan_digest": self.plan_digest,
            "source_access": "read-only",
            "source_written": False,
        }


@dataclass(frozen=True, slots=True)
class ProjectIndexResult:
    source_id: UUID
    document_id: UUID
    revision: int
    chunk_count: int
    vector_count: int
    plan: ProjectIndexPlan

    def as_dict(self) -> dict[str, Any]:
        return self.plan.as_dict() | {
            "source_id": str(self.source_id),
            "document_id": str(self.document_id),
            "knowledge_revision": self.revision,
            "vector_count": self.vector_count,
            "lexical_state": "ready",
            "embedding_state": "ready",
            "applied": True,
        }


def feature_hash_baseline_vector(
    value: str, *, dimensions: int = NON_SEMANTIC_BASELINE_DIMENSION
) -> tuple[float, ...]:
    """Return a normalized non-semantic lexical baseline vector.

    This helper must never populate a semantic/dense index or mark an embedding
    profile ready. It exists only for lexical-baseline evaluation.
    """

    if dimensions <= 0 or not value.strip():
        raise ValidationFailed("Feature-hash baseline bos metin/gecersiz boyut kabul etmez")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    tokens = tuple(_TOKEN.findall(normalized))
    features = list(tokens)
    for token in tokens:
        padded = f"^{token}$"
        features.extend(
            f"g:{padded[index : index + 3]}" for index in range(max(0, len(padded) - 2))
        )
    if not features:
        compact = re.sub(r"\s+", " ", normalized).strip()
        padded = f"^{compact}$"
        features.extend(
            f"c:{padded[index : index + 3]}" for index in range(max(1, len(padded) - 2))
        )
    values = [0.0] * dimensions
    for feature in features:
        feature_digest = hashlib.sha256(feature.encode("utf-8")).digest()
        index = int.from_bytes(feature_digest[:4], "big") % dimensions
        values[index] += 1.0 if feature_digest[4] & 1 else -1.0
    norm = math.sqrt(sum(item * item for item in values))
    if norm == 0.0:
        raise ValidationFailed("Feature-hash baseline ozellik uretemedi")
    return tuple(float(f"{item / norm:.12f}") for item in values)


def _is_supported(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    return path.suffix.lower() in _SUPPORTED_SUFFIXES or path.name.lower() in _SUPPORTED_NAMES


def _verified_text(
    root: Path,
    relative_path: str,
    expected_digest: str,
    *,
    expected_size: int | None = None,
    max_bytes: int = DiscoveryPolicy().max_file_bytes,
) -> str | None:
    candidate = root.joinpath(*PurePosixPath(relative_path).parts)
    current = root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        if current.is_symlink():
            raise PolicyViolation("Kaynak indeks yolu symlink kullanamaz")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise PolicyViolation("Kaynak indeks yolu kok disina cikamaz") from exc
    before = resolved.stat()
    size = before.st_size if expected_size is None else expected_size
    if (
        size < 0
        or max_bytes < 0
        or size > max_bytes
        or before.st_size != size
        or not stat.S_ISREG(before.st_mode)
    ):
        raise PolicyViolation("Kaynak dosya boyutu taramadan sonra degisti")
    payload = bytearray()
    with resolved.open("rb") as handle:
        while True:
            chunk = handle.read(min(1 << 20, size + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > size:
                raise PolicyViolation("Kaynak dosya okuma sirasinda buyudu")
    after = resolved.stat()
    if (
        len(payload) != size
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or digest_of_bytes(bytes(payload)) != expected_digest
    ):
        raise PolicyViolation("Kaynak dosya taramadan sonra degisti; yeniden tarama gerekli")
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _line_units(
    relative_path: str, text: str, start_order: int, *, first_line: int = 1
) -> tuple[ContentUnit, ...]:
    lines = text.splitlines()
    if not lines or not text.strip():
        return ()
    units: list[ContentUnit] = []
    buffer: list[str] = []
    start_line = first_line

    def flush(end_line: int) -> None:
        nonlocal start_line
        body = "\n".join(buffer).strip()
        if body:
            units.append(
                ContentUnit(
                    unit_id=f"{relative_path}#L{start_line}-L{end_line}",
                    kind=UnitKind.CODE,
                    text=body,
                    locator=Locator(
                        relative_path=relative_path,
                        line_start=start_line,
                        line_end=end_line,
                    ),
                    order=start_order + len(units),
                )
            )
        buffer.clear()
        start_line = end_line + 1

    for line_number, line in enumerate(lines, start=first_line):
        candidate = "\n".join((*buffer, line))
        if buffer and (
            len(candidate) > MAX_CHUNK_CHARACTERS or estimate_tokens(candidate) > MAX_CHUNK_TOKENS
        ):
            flush(line_number - 1)
            start_line = line_number
        if len(line) > MAX_CHUNK_CHARACTERS or estimate_tokens(line) > MAX_CHUNK_TOKENS:
            if buffer:
                flush(line_number - 1)
                start_line = line_number
            offset = 0
            while offset < len(line):
                cut = min(len(line), offset + MAX_CHUNK_CHARACTERS)
                while estimate_tokens(line[offset:cut]) > MAX_CHUNK_TOKENS:
                    cut = offset + max(1, (cut - offset) // 2)
                piece = line[offset:cut].strip()
                if piece:
                    units.append(
                        ContentUnit(
                            unit_id=f"{relative_path}#L{line_number}-P{offset}",
                            kind=UnitKind.CODE,
                            text=piece,
                            locator=Locator(
                                relative_path=relative_path,
                                line_start=line_number,
                                line_end=line_number,
                            ),
                            order=start_order + len(units),
                        )
                    )
                offset = cut
            start_line = line_number + 1
            continue
        buffer.append(line)
    if buffer:
        flush(first_line + len(lines) - 1)
    return tuple(units)


def _file_units(relative_path: str, text: str, start_order: int) -> tuple[ContentUnit, ...]:
    """Python'da top-level AST sembollerini, diger kodda satir araligini kullan."""
    if PurePosixPath(relative_path).suffix.lower() != ".py":
        return _line_units(relative_path, text, start_order)
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return _line_units(relative_path, text, start_order)

    lines = text.splitlines()
    symbols = [
        node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    if not symbols:
        return _line_units(relative_path, text, start_order)

    units: list[ContentUnit] = []
    next_line = 1
    for node in symbols:
        first = min((node.lineno, *(item.lineno for item in node.decorator_list)))
        last = node.end_lineno or node.lineno
        if first < next_line or last > len(lines) or last < first:
            return _line_units(relative_path, text, start_order)
        if next_line < first:
            units.extend(
                _line_units(
                    relative_path,
                    "\n".join(lines[next_line - 1 : first - 1]),
                    start_order + len(units),
                    first_line=next_line,
                )
            )
        body = "\n".join(lines[first - 1 : last]).strip()
        if body and len(body) <= MAX_CHUNK_CHARACTERS and estimate_tokens(body) <= MAX_CHUNK_TOKENS:
            units.append(
                ContentUnit(
                    unit_id=f"{relative_path}#L{first}-L{last}-S{node.name}",
                    kind=UnitKind.SYMBOL,
                    text=body,
                    locator=Locator(
                        relative_path=relative_path,
                        line_start=first,
                        line_end=last,
                        symbol=node.name,
                    ),
                    order=start_order + len(units),
                )
            )
        else:
            units.extend(
                _line_units(
                    relative_path,
                    "\n".join(lines[first - 1 : last]),
                    start_order + len(units),
                    first_line=first,
                )
            )
        next_line = last + 1
    if next_line <= len(lines):
        units.extend(
            _line_units(
                relative_path,
                "\n".join(lines[next_line - 1 :]),
                start_order + len(units),
                first_line=next_line,
            )
        )
    return tuple(units)


def _select_eligible_files(
    discovery: DiscoveryReport,
    allowed: frozenset[str] | None,
) -> tuple[DiscoveredFile, ...]:
    """Secimi eligibility karar sozlesmesinden yap, `files`'i dogrula.

    Eligible kayitlar `eligibility_decisions`'tan okunur; her eligible yol
    `files`'da tam olarak bir kez (digest/buyukluk/is_text metadata'siyle)
    bulunmak zorundadir. Yinelenmis/kararsiz/tutarsiz veya envanter girdisiyle
    uyumsuz eligible kayit fail-closed olarak plani reddeder.
    """

    decisions: dict[str, EligibilityDecision] = {}
    for item in discovery.eligibility_decisions:
        if item.relative_path in decisions:
            raise ValidationFailed("Eligibility karar kumesi yineleniyor; plan reddedildi")
        decisions[item.relative_path] = item
    by_path: dict[str, DiscoveredFile] = {}
    for item in discovery.files:
        if item.relative_path in by_path:
            raise ValidationFailed("Kesif dosya kumesi tutarsiz; plan reddedildi")
        by_path[item.relative_path] = item
    inventory_by_path: dict[str, Any] = {}
    for entry in discovery.inventory:
        if entry.kind is FileKind.FILE and entry.relative_path in by_path:
            inventory_by_path[entry.relative_path] = entry
    for path, item in by_path.items():
        decision = decisions.get(path)
        if decision is None or decision.outcome is not EligibilityOutcome.ELIGIBLE:
            raise ValidationFailed("Kesif dosyasi eligibility karari tasimiyor; plan reddedildi")
        entry = inventory_by_path.get(path)
        if entry is None or entry.eligibility is None:
            raise ValidationFailed("Eligible dosyanin envanter kaydi yok; plan reddedildi")
        if (
            entry.eligibility.outcome is not EligibilityOutcome.ELIGIBLE
            or entry.eligibility.reason is not None
            or entry.is_text != item.is_text
            or entry.size_bytes != item.size_bytes
        ):
            raise ValidationFailed(
                "Eligibility karari envanter girdisiyle uyumsuz; plan reddedildi"
            )
    eligible_paths = {
        path
        for path, decision in decisions.items()
        if decision.outcome is EligibilityOutcome.ELIGIBLE
    }
    if any(path not in by_path for path in eligible_paths):
        raise ValidationFailed("Eligibility karari kesif dosya kumesiyle tutarsiz; plan reddedildi")
    selected = [by_path[path] for path in sorted(eligible_paths)]
    if allowed is not None:
        selected = [item for item in selected if item.relative_path in allowed]
    return tuple(item for item in selected if item.is_text and _is_supported(item.relative_path))


def build_project_index_plan(
    *,
    project_id: UUID,
    project_slug: str,
    source_root: Path,
    source_revision: str,
    expected_tree_digest: str,
    embedding_candidates: Sequence[EmbeddingRouteCandidate] = (),
    allow_remote_source: bool = False,
    local_model_ref: str = REAL_EMBEDDING_MODEL_REF,
    local_dimension: int = REAL_EMBEDDING_DIMENSION,
    allowed_relative_paths: tuple[str, ...] | None = None,
    discovery_policy: DiscoveryPolicy | None = None,
) -> ProjectIndexPlan:
    """Build a deterministic source index plan without writing anywhere.

    Fail-closed kaynak sinirlari: beklenen tree digest uymazsa, kesif `truncated`
    ise veya metadata envanteri `PARTIAL` ise (dizin/girdi siniri, derinlik
    siniri veya okunamayan girdi kaynakli) plan reddedilir.

    Dosya secimi eligibility karar sozlesmesinden (`eligibility_decisions`)
    yapilir; `files` listesi secim kaynagi degil, kararlarla karsilastirilip
    dogrulanir. Karar kumesi eksik, yinelenmis, tutarsiz (eligible karar
    `files` disinda / files girdisi kararsiz) veya envanter girdisiyle
    uymuyorsa plan reddedilir; eligibility `files`'in varligindan turetilmez.

    """

    root = source_root.resolve(strict=True)
    discovery = discover(root, policy=discovery_policy)
    if discovery.truncated or discovery.tree_digest != expected_tree_digest:
        raise PolicyViolation("Kaynak revision/tree drift; yeniden project scan gerekli")
    if discovery.inventory_status is InventoryStatus.PARTIAL:
        # Kesif, tree digest'ini yalnizca icerigi okunabilan (eligible) dosyalar
        # uzerinden hesapladigindan eksik bir kesif, beklenen digest'le
        # uyusabilir ve `truncated` False kalabilir; eksik kaynaktan tam indeks
        # plani uretilemez. Yalnizca sabit neden kodu hata mesajina girer;
        # checkpoint yolu hassas bilgi tasiyabileceginden raporda kalir.
        reason = (
            discovery.inventory_stop_reason.value
            if discovery.inventory_stop_reason is not None
            else "unknown"
        )
        raise PolicyViolation(
            f"Kaynak envanteri tam degil (partial); indeks plani reddedildi (stop-reason={reason})"
        )
    allowed: frozenset[str] | None = None
    if allowed_relative_paths is not None:
        if (
            not allowed_relative_paths
            or len(set(allowed_relative_paths)) != len(allowed_relative_paths)
            or any(
                PurePosixPath(value).is_absolute()
                or PurePosixPath(value).as_posix() != value
                or ".." in PurePosixPath(value).parts
                for value in allowed_relative_paths
            )
        ):
            raise ValidationFailed("Bounded source allowlist exact portable paths ister")
        allowed = frozenset(allowed_relative_paths)
    selected = _select_eligible_files(discovery, allowed)
    if allowed is not None and {item.relative_path for item in selected} != allowed:
        raise ValidationFailed("Bounded source allowlist eksik/desteklenmeyen dosya tasiyor")
    if not selected:
        raise ValidationFailed("Indekslenebilir kaynak dosyasi bulunamadi")
    units: list[ContentUnit] = []
    manifest_files: list[dict[str, Any]] = []
    skipped_encoding = 0
    for item in selected:
        text = _verified_text(
            root,
            item.relative_path,
            item.content_digest,
            expected_size=item.size_bytes,
            max_bytes=discovery.policy.max_file_bytes,
        )
        if text is None:
            skipped_encoding += 1
            continue
        produced = _file_units(item.relative_path, text, len(units))
        units.extend(produced)
        manifest_files.append(
            {
                "path": item.relative_path,
                "content_digest": item.content_digest,
                "size_bytes": item.size_bytes,
                "unit_count": len(produced),
            }
        )
    if not units:
        raise ValidationFailed("Indekslenebilir kaynak icerigi bulunamadi")
    manifest_document = {
        "schema": "zekam-project-source-manifest/v1",
        "project_id": str(project_id),
        "project_slug": project_slug,
        "source_revision": source_revision,
        "tree_digest": expected_tree_digest,
        "files": manifest_files,
    }
    manifest = canonical_json(manifest_document).encode("utf-8")
    artifact = IngestionService(default_router()).artifact_for(
        manifest,
        name=f"{project_slug}.repository-manifest.json",
        media_type="application/vnd.zekam.source-manifest+json",
        now=dt.datetime.now(dt.UTC),
    )
    document = NormalizedDocument(
        document_id=f"project-{project_id}-{expected_tree_digest[-16:]}",
        artifact_digest=artifact.artifact_digest,
        source_format=SourceFormat.REPOSITORY,
        units=tuple(units),
        parser_ref=INDEXER_VERSION,
        parser_version="1",
        parser_profile={
            "tree_digest": expected_tree_digest,
            "source_revision": source_revision,
            "source_access": "read-only",
            "secret_scan": True,
        },
    )
    chunk_profile = ChunkProfile(
        name="project-source-v1",
        max_tokens=MAX_CHUNK_TOKENS,
        overlap_tokens=0,
        keep_code_whole=True,
    )
    embedding_route = select_embedding_route(
        embedding_candidates,
        local_model_ref=local_model_ref,
        local_dimension=local_dimension,
        remote_source_allowed=allow_remote_source,
    )
    embedding_profile = EmbeddingProfile(
        model_ref=embedding_route.model_ref,
        dimension=embedding_route.dimension,
        distance="cosine",
    )
    chunks = tuple(
        Chunk(
            chunk_id=f"project-{project_id}-{expected_tree_digest[-12:]}-c{index}",
            document_id=document.document_id,
            text=unit.text,
            locator=unit.locator,
            kind=unit.kind,
            token_count=estimate_tokens(unit.text),
            order=index,
            profile_digest=chunk_profile.profile_digest,
        )
        for index, unit in enumerate(document.units)
    )
    return ProjectIndexPlan(
        project_id=project_id,
        project_slug=project_slug,
        source_revision=source_revision,
        tree_digest=expected_tree_digest,
        discovery=discovery,
        manifest=manifest,
        document=document,
        chunk_profile=chunk_profile,
        embedding_profile=embedding_profile,
        embedding_route=embedding_route,
        chunks=chunks,
        selected_file_count=len(manifest_files),
        skipped_unsupported=sum(
            1
            for item in discovery.files
            if (allowed is None or item.relative_path in allowed)
            and (not item.is_text or not _is_supported(item.relative_path))
        ),
        skipped_encoding=skipped_encoding,
    )


def apply_project_index(
    plan: ProjectIndexPlan,
    *,
    connection: Any,
    knowledge: Any,
    retrieval: Any,
    object_store: LocalContentAddressedStore,
    embedding_provider: EmbeddingProvider | None = None,
    embedding_policy: EmbeddingPolicy | None = None,
    now: dt.datetime | None = None,
) -> ProjectIndexResult:
    """Persist immutable CAS manifest and atomically commit all PostgreSQL index rows.

    CAS content is digest-addressed and may safely pre-exist the database transaction;
    a database failure can leave only an unreachable immutable object, never a partial
    active knowledge version.
    """

    moment = now or dt.datetime.now(dt.UTC)
    if plan.embedding_route.kind is EmbeddingRouteKind.QUALIFIED_REMOTE:
        raise PolicyViolation(
            "Remote project embedding yalniz ayri exact provider authorization akisi ile uygulanir"
        )
    if embedding_provider is None or embedding_policy is None:
        raise PolicyViolation("Verified embedding provider/policy olmadan indeks uygulanamaz")
    provider_profile = embedding_provider.describe()
    accepted_model_refs = {
        provider_profile.exact_model_id,
        f"openai/{provider_profile.exact_model_id}",
    }
    if (
        plan.embedding_profile.model_ref not in accepted_model_refs
        or plan.embedding_profile.dimension != provider_profile.dimension
        or plan.embedding_profile.query_prefix != provider_profile.query_prefix
        or plan.embedding_profile.passage_prefix != provider_profile.passage_prefix
        or plan.embedding_profile.provider_profile_digest != provider_profile.profile_digest
        or embedding_policy.expected_profile_digest != provider_profile.profile_digest
    ):
        raise PolicyViolation("Project index/provider profile drift; rebuild required")
    provider_profile.assert_policy(embedding_policy)

    # Provider work finishes before CAS/database mutation. A timeout, partial batch,
    # NaN or dimension drift therefore cannot leave a half-activated generation.
    vectors: dict[str, tuple[float, ...]] = {}
    for offset in range(0, len(plan.chunks), 8):
        chunk_batch = plan.chunks[offset : offset + 8]
        texts = tuple(chunk.text for chunk in chunk_batch)
        embedded = embedding_provider.embed_documents(texts, embedding_policy)
        verify_document_embedding_batch(texts, embedded, provider_profile)
        for chunk, vector in zip(chunk_batch, embedded.vectors, strict=True):
            vectors[chunk.chunk_id] = vector
    if len(vectors) != len(plan.chunks):
        raise PolicyViolation("Project embedding batch eksik/duplicate sonuc uretti")

    service = IngestionService(default_router())
    artifact = service.artifact_for(
        plan.manifest,
        name=f"{plan.project_slug}.repository-manifest.json",
        media_type="application/vnd.zekam.source-manifest+json",
        now=moment,
    )
    if artifact.artifact_digest != plan.document.artifact_digest:
        raise PolicyViolation("Indeks manifest artifact drift")
    stored = object_store.ensure().put(plan.manifest, media_type=artifact.media_type)
    if stored.digest != artifact.content_digest:
        raise PolicyViolation("Indeks manifest CAS digest uyusmazligi")

    source_slug = f"project-{plan.project_slug}-source"
    job = service.start(
        job_id=plan.plan_digest,
        source_id=source_slug,
        artifact=artifact,
        idempotency_key=plan.plan_digest,
    )
    job = service.store(job)
    job = job.advance(IngestionStage.PARSED).advance(IngestionStage.NORMALIZED)
    job = service.index(job)
    pending = pending_version(
        version_id=f"{source_slug}-pending",
        source_id=source_slug,
        revision=1,
        artifact=artifact,
        content_digest=plan.document.content_digest,
        now=moment,
    )
    job, _ = service.activate(job, pending)

    with connection.transaction():
        artifact_id = knowledge.store_artifact(artifact)
        source_id = knowledge.register_source(source_slug, SourceFormat.REPOSITORY, now=moment)
        equivalent = knowledge.equivalent_version(
            source_id,
            artifact_digest=artifact.artifact_digest,
            content_digest=plan.document.content_digest,
        )
        if equivalent is not None and equivalent[2] == "active":
            with connection.cursor() as cursor:
                cursor.execute(
                    "select d.id, count(distinct c.id), count(distinct e.id),"
                    " ep.profile_digest"
                    " from knowledge.normalized_document d"
                    " join knowledge.source_version v on v.id = d.version_id"
                    " join knowledge.document_index_profile dip on dip.document_id=d.id"
                    " join knowledge.embedding_profile ep on ep.id=dip.embedding_profile_id"
                    " left join knowledge.chunk c on c.document_id = d.id"
                    " left join knowledge.chunk_embedding e on e.chunk_id = c.id"
                    " where v.id = %s group by d.id,ep.profile_digest",
                    (equivalent[0],),
                )
                row = cursor.fetchone()
            if row is None or int(row[1]) != len(plan.chunks) or int(row[2]) != len(plan.chunks):
                raise PolicyViolation("Mevcut indeks eksik; sessiz tamamlanmis sayilamaz")
            if str(row[3]) != plan.embedding_profile.profile_digest:
                raise PolicyViolation("Mevcut index provider profile drift; rebuild required")
            return ProjectIndexResult(
                source_id=source_id,
                document_id=row[0],
                revision=equivalent[1],
                chunk_count=int(row[1]),
                vector_count=int(row[2]),
                plan=plan,
            )
        revision = knowledge.next_revision(source_id)
        version = pending_version(
            version_id=f"{source_slug}-r{revision}",
            source_id=source_slug,
            revision=revision,
            artifact=artifact,
            content_digest=plan.document.content_digest,
            now=moment,
        )
        job_id = knowledge.start_job(job, source_id=source_id, artifact_id=artifact_id, now=moment)
        version_id = knowledge.store_version(version, source_id=source_id, artifact_id=artifact_id)
        document_id = knowledge.store_document(plan.document, version_id=version_id, now=moment)
        chunk_profile_id = retrieval.store_chunk_profile(plan.chunk_profile, now=moment)
        embedding_profile_id = retrieval.store_embedding_profile(plan.embedding_profile, now=moment)
        chunk_ids = retrieval.store_chunks(plan.chunks, document_id=document_id, now=moment)
        for chunk in plan.chunks:
            retrieval.store_embedding(
                chunk_ids[chunk.chunk_id],
                embedding_profile_id,
                plan.embedding_profile,
                vectors[chunk.chunk_id],
                now=moment,
            )
        retrieval.store_document_profiles(
            document_id=document_id,
            chunk_profile_id=chunk_profile_id,
            embedding_profile_id=embedding_profile_id,
            embedding_state="ready",
            now=moment,
        )
        knowledge.save_progress(job_id, job, now=moment)
        previous = knowledge.active_version(source_id)
        if previous is not None and previous != version_id:
            knowledge.supersede_version(previous, version_id)
        knowledge.activate_version(version_id)
    return ProjectIndexResult(
        source_id=source_id,
        document_id=document_id,
        revision=revision,
        chunk_count=len(plan.chunks),
        vector_count=len(plan.chunks),
        plan=plan,
    )
