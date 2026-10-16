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
import json
import math
import re
import stat
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
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
from zekam.application.model_health_service import ProbeUnavailable
from zekam.application.provider_adapter import (
    ProviderCallCancelled,
    ProviderDeadlineExceeded,
    ProviderHTTPUnavailable,
    ProviderOutcomeUnknown,
    ProviderCallQuotaExceeded,
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
from zekam.domain.canonical import canonical_json, digest, digest_of_bytes, parse_digest
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
LOCAL_E5_MODEL_REF = "intfloat/multilingual-e5-small"
LOCAL_E5_DIMENSION = 384
E5_QUERY_PREFIX = "query: "
E5_PASSAGE_PREFIX = "passage: "
INDEXER_VERSION = "zekam-project-source-indexer/v6"
MAX_CHUNK_TOKENS = 384
MAX_CHUNK_CHARACTERS = 6000
MAX_XML_ELEMENTS = 16_384
MAX_XML_DEPTH = 256
MAX_PROJECT_EMBED_BATCH_SIZE = 8
MAX_PROJECT_EMBED_TEXT_BYTES = 256 * 1024
MAX_PROJECT_EMBED_BATCH_TOKENS = 8_192
MAX_PROJECT_EMBED_BATCH_PAYLOAD_BYTES = 512 * 1024


def embedding_prefixes_for_model(model_ref: str) -> tuple[str, str]:
    """Return the exact text prefixes owned by the selected embedding model."""

    if model_ref == LOCAL_E5_MODEL_REF:
        return E5_QUERY_PREFIX, E5_PASSAGE_PREFIX
    return "", ""


def source_identity_digest(
    project_id: UUID | str,
    source_revision: str,
    tree_digest: str,
    inventory_digest: str | None = None,
) -> str:
    body: dict[str, object] = {
        "schema": "zekam-project-source-identity/v1",
        "project_id": str(project_id),
        "logical_source": "repository",
        "source_revision": source_revision,
        "tree_digest": tree_digest,
    }
    if inventory_digest is not None:
        parse_digest(inventory_digest)
        body["inventory_digest"] = inventory_digest
    return str(digest(body))


def _assert_plan_source_identity(plan: ProjectIndexPlan) -> None:
    """Ensure manifest/profile cannot be reassociated with another source plan."""

    try:
        manifest = json.loads(plan.manifest.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PolicyViolation("Indeks manifest source identity JSON olmali") from exc
    if not isinstance(manifest, dict):
        raise PolicyViolation("Indeks manifest source identity nesne olmali")
    expected = {
        "schema": "zekam-project-source-manifest/v2",
        "project_id": str(plan.project_id),
        "project_slug": plan.project_slug,
        "source_identity_digest": plan.source_identity_digest,
        "source_revision": plan.source_revision,
        "tree_digest": plan.tree_digest,
        "inventory_digest": plan.discovery.inventory_digest,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise PolicyViolation("Indeks manifest source identity drift")
    profile = plan.document.parser_profile
    if not isinstance(profile, dict) or profile.get("source_identity_digest") != (
        plan.source_identity_digest
    ):
        raise PolicyViolation("Indeks document source identity drift")


def _project_embedding_batches(
    chunks: tuple[Chunk, ...], *, batch_size: int, model_id: str = REAL_EMBEDDING_MODEL_REF
) -> tuple[tuple[Chunk, ...], ...]:
    """Build stable count/token/payload bounded batches before provider effects."""

    if type(batch_size) is not int or not 1 <= batch_size <= MAX_PROJECT_EMBED_BATCH_SIZE:
        raise ValidationFailed("Project embedding batch sayi siniri gecersiz")
    fixed_bytes = len(
        json.dumps(
            {"model": model_id, "input": [], "encoding_format": "float"},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    batches: list[tuple[Chunk, ...]] = []
    pending: list[Chunk] = []
    pending_tokens = 0
    pending_bytes = fixed_bytes
    for chunk in chunks:
        raw_bytes = len(chunk.text.encode("utf-8"))
        item_bytes = len(json.dumps(chunk.text, ensure_ascii=True).encode("utf-8"))
        if (
            raw_bytes > MAX_PROJECT_EMBED_TEXT_BYTES
            or chunk.token_count > MAX_PROJECT_EMBED_BATCH_TOKENS
            or fixed_bytes + item_bytes > MAX_PROJECT_EMBED_BATCH_PAYLOAD_BYTES
        ):
            raise ValidationFailed("Project embedding tek chunk byte/token sinirini asiyor")
        next_bytes = pending_bytes + item_bytes + (1 if pending else 0)
        if pending and (
            len(pending) >= batch_size
            or pending_tokens + chunk.token_count > MAX_PROJECT_EMBED_BATCH_TOKENS
            or next_bytes > MAX_PROJECT_EMBED_BATCH_PAYLOAD_BYTES
        ):
            batches.append(tuple(pending))
            pending = []
            pending_tokens = 0
            pending_bytes = fixed_bytes
            next_bytes = fixed_bytes + item_bytes
        pending.append(chunk)
        pending_tokens += chunk.token_count
        pending_bytes = next_bytes
    if pending:
        batches.append(tuple(pending))
    return tuple(batches)


def _check_project_embedding_budget(
    cancel: threading.Event | None, deadline_monotonic: float | None
) -> None:
    if cancel is not None and cancel.is_set():
        raise ProviderCallCancelled("Project embedding cancelled")
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        raise ProviderDeadlineExceeded("Project embedding deadline expired")


def _embed_project_batch_with_retry(
    provider: EmbeddingProvider,
    texts: tuple[str, ...],
    policy: EmbeddingPolicy,
    *,
    cancel: threading.Event | None,
    deadline_monotonic: float | None,
) -> EmbeddingBatch:
    """Retry bounded transport-unavailable calls without hiding unknown effects."""

    for attempt in range(4):
        _check_project_embedding_budget(cancel, deadline_monotonic)
        try:
            batch = provider.embed_documents(texts, policy)
            _check_project_embedding_budget(cancel, deadline_monotonic)
            return batch
        except ProbeUnavailable as exc:
            _check_project_embedding_budget(cancel, deadline_monotonic)
            if (
                attempt == 3
                or isinstance(exc, (ProviderDeadlineExceeded, ProviderOutcomeUnknown))
                or (isinstance(exc, ProviderHTTPUnavailable) and not exc.retryable)
            ):
                raise
            delay = min(2**attempt, 4)
            if deadline_monotonic is not None:
                delay = min(delay, deadline_monotonic - time.monotonic())
                if delay <= 0:
                    raise ProviderDeadlineExceeded("Project embedding deadline expired") from None
            if cancel is None:
                time.sleep(delay)
            elif cancel.wait(delay):
                raise ProviderCallCancelled("Project embedding cancelled") from None
    raise AssertionError("unreachable")


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
        ".pks",
        ".pkb",
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
_STRUCTURAL_CODE_SUFFIXES = frozenset(
    {".groovy", ".java", ".js", ".jsx", ".kt", ".kts", ".php", ".rs", ".swift", ".ts", ".tsx"}
)
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
_STRUCTURAL_DECLARATION = re.compile(
    r"\b(class|interface|function|enum|record|struct|trait|object|protocol|extension|"
    r"actor|impl|module|namespace|fun|fn|func|def)\b"
)
_STRUCTURAL_TYPE_ALIAS = re.compile(r"\btype\s+[A-Za-z_$][A-Za-z0-9_$]*")
_STRUCTURAL_IDENTIFIER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_MARKDOWN_HEADING = re.compile(r"^\s{0,3}(?P<marks>#{1,6})\s+(?P<title>\S.*?)\s*$")
_MARKDOWN_LIST = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+\S")
_MARKDOWN_FENCE = re.compile(r"^\s*(```+|~~~+)")
_XML_UNSAFE_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_SQL_STRUCTURAL_SUFFIXES = frozenset({".sql", ".pks", ".pkb"})
_SQL_DECLARATION = re.compile(
    r"(?m)^[ \t]*create\s+(?:or\s+replace\s+)?"
    r"(?P<kind>package\s+body|package|procedure|function|trigger|view|table|type)\s+"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)?)",
    re.IGNORECASE,
)


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
    def source_identity_digest(self) -> str:
        """Project/logical-source/revision identity, distinct from content digest."""

        return source_identity_digest(
            self.project_id,
            self.source_revision,
            self.tree_digest,
            self.discovery.inventory_digest,
        )

    @property
    def plan_digest(self) -> str:
        return str(
            digest(
                {
                    "project_id": str(self.project_id),
                    "source_identity_digest": self.source_identity_digest,
                    "source_revision": self.source_revision,
                    "tree_digest": self.tree_digest,
                    "inventory_digest": self.discovery.inventory_digest,
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
            "source_identity_digest": self.source_identity_digest,
            "source_revision": self.source_revision,
            "tree_digest": self.tree_digest,
            "inventory_digest": self.discovery.inventory_digest,
            "metadata_inventory_digest": self.discovery.metadata_inventory_digest,
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


def _mask_structural_literals(text: str) -> str | None:
    """Yorum ve dizgeleri kod konumlarini koruyarak boslukla maskele."""
    chars = list(text)
    length = len(chars)
    index = 0
    while index < length:
        character = chars[index]
        if character in {"'", '"', "`"}:
            quote = character
            end = index + 1
            while end < length:
                if chars[end] == "\\":
                    end += 2
                    continue
                if chars[end] == quote:
                    break
                if chars[end] == "\n" and quote != "`":
                    return None
                end += 1
            if end >= length:
                return None
            for position in range(index, end + 1):
                if chars[position] != "\n":
                    chars[position] = " "
            index = end + 1
            continue
        if character == "/" and index + 1 < length and chars[index + 1] == "/":
            end = index
            while end < length and chars[end] != "\n":
                chars[end] = " "
                end += 1
            index = end
            continue
        if character == "/" and index + 1 < length and chars[index + 1] == "*":
            end = text.find("*/", index + 2)
            if end < 0:
                return None
            end += 2
            for position in range(index, end):
                if chars[position] != "\n":
                    chars[position] = " "
            index = end
            continue
        index += 1
    return "".join(chars)


_TYPE_INTRODUCERS = frozenset({":", "<", "|", "&", "(", "[", ","})


def _function_body_opening(masked: str, cursor: int) -> int | None:
    """`function` govdesinin acilis parantezini dogru govdede bul.

    Parametre listesindeki varsayilan nesne/tip ({}) ve TS donus tipindeki
    nesne tipleri govde acilisinda sayilmaz: once generic'ler ve parantez
    listesi atlanir; donus tipinin bir parcasi olan dengeli `{...}` gruplari
    (tip giris karakterini tasiyanlar) atlanir. Duzen kurulamazsa None.
    """
    length = len(masked)
    angle = 0
    while cursor < length:
        character = masked[cursor]
        if angle:
            if character == "<":
                angle += 1
            elif character == ">":
                angle -= 1
                if angle < 0:
                    return None
            cursor += 1
            continue
        if character == "<":
            angle = 1
            cursor += 1
        elif character == "(":
            break
        elif not character.isspace():
            return None
        else:
            cursor += 1
    if cursor >= length:
        return None
    paren = 1
    cursor += 1
    while cursor < length and paren:
        character = masked[cursor]
        if character == "(":
            paren += 1
        elif character == ")":
            paren -= 1
        cursor += 1
    if paren:
        return None
    while cursor < length:
        character = masked[cursor]
        if character == "{":
            back = cursor - 1
            while back >= 0 and masked[back].isspace():
                back -= 1
            if back >= 0 and masked[back] in _TYPE_INTRODUCERS:
                inner = 1
                cursor += 1
                while cursor < length and inner:
                    if masked[cursor] == "{":
                        inner += 1
                    elif masked[cursor] == "}":
                        inner -= 1
                    cursor += 1
                if inner:
                    return None
                continue
            close = cursor + 1
            inner = 1
            while close < length and inner:
                if masked[close] == "{":
                    inner += 1
                elif masked[close] == "}":
                    inner -= 1
                close += 1
            if inner:
                return None
            after = close
            while after < length and masked[after].isspace():
                after += 1
            if after < length and masked[after] in {"|", "&", ">"}:
                return None
            return cursor
        if character == ";":
            return None
        cursor += 1
    return None


def _type_alias_end(masked: str, cursor: int) -> int | None:
    """Bir TypeScript/Flow type alias'in bounded bitisini bul."""
    length = len(masked)
    while cursor < length and masked[cursor].isspace():
        cursor += 1
    if cursor < length and masked[cursor] == "<":
        angle = 1
        cursor += 1
        while cursor < length and angle:
            character = masked[cursor]
            if character == "<":
                angle += 1
            elif character == ">":
                angle -= 1
            cursor += 1
        if angle:
            return None
    while cursor < length and masked[cursor].isspace():
        cursor += 1
    if cursor >= length or masked[cursor] != "=":
        return None
    equals = cursor
    cursor += 1
    braces = 0
    brackets = 0
    parentheses = 0
    while cursor < length:
        character = masked[cursor]
        if character == "{":
            braces += 1
        elif character == "}":
            braces -= 1
            if braces < 0:
                return None
        elif character == "[":
            brackets += 1
        elif character == "]":
            brackets -= 1
            if brackets < 0:
                return None
        elif character == "(":
            parentheses += 1
        elif character == ")":
            parentheses -= 1
            if parentheses < 0:
                return None
        elif character == ";" and not (braces or brackets or parentheses):
            if not masked[equals + 1 : cursor].strip():
                return None
            return cursor + 1
        cursor += 1
    if braces or brackets or parentheses or not masked[equals + 1 :].strip():
        return None
    return length


def _structural_code_units(
    relative_path: str, text: str, start_order: int
) -> tuple[ContentUnit, ...] | None:
    """Dengeli ust duzey Java/JS/TS declaration'lari icin sembol parcasi."""
    masked = _mask_structural_literals(text)
    if masked is None:
        return None
    lines = text.splitlines()
    if not lines:
        return None
    depth_before = [0] * (len(masked) + 1)
    depth = 0
    for index, character in enumerate(masked):
        depth_before[index] = depth
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth < 0:
                return None
    depth_before[len(masked)] = depth
    if depth != 0:
        return None

    candidates: list[tuple[int, str, int, int]] = []
    for match in _STRUCTURAL_DECLARATION.finditer(masked):
        if depth_before[match.start()] != 0:
            continue
        name_start = match.end()
        while name_start < len(masked) and masked[name_start].isspace():
            name_start += 1
        name_match = _STRUCTURAL_IDENTIFIER.match(masked, name_start)
        if name_match is None:
            return None
        name = name_match.group(0)
        if match.group(1) in {"function", "fun", "fn", "func", "def"}:
            opening = _function_body_opening(masked, name_match.end())
        else:
            opening = masked.find("{", name_match.end())
        if opening is None or opening < 0:
            return None
        # A declaration header must not cross another top-level declaration.
        next_declaration = _STRUCTURAL_DECLARATION.search(masked, name_match.end(), opening)
        if next_declaration is not None and depth_before[next_declaration.start()] == 0:
            return None
        nested = 1
        closing = opening + 1
        while closing < len(masked) and nested:
            if masked[closing] == "{":
                nested += 1
            elif masked[closing] == "}":
                nested -= 1
            closing += 1
        if nested:
            return None
        start_line = text.count("\n", 0, match.start()) + 1
        end_line = text.count("\n", 0, closing - 1) + 1
        candidates.append((match.start(), name, start_line, end_line))
    for match in _STRUCTURAL_TYPE_ALIAS.finditer(masked):
        if depth_before[match.start()] != 0:
            continue
        name_start = match.start() + len("type")
        while name_start < len(masked) and masked[name_start].isspace():
            name_start += 1
        name_match = _STRUCTURAL_IDENTIFIER.match(masked, name_start)
        if name_match is None:
            continue
        closing = _type_alias_end(masked, name_match.end())
        if closing is None:
            continue
        start_line = text.count("\n", 0, match.start()) + 1
        end_line = text.count("\n", 0, closing - 1) + 1
        candidates.append((match.start(), name_match.group(0), start_line, end_line))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    if any(previous[3] >= current[2] for previous, current in pairwise(candidates)):
        return None

    units: list[ContentUnit] = []
    next_line = 1
    for _, name, first, last in candidates:
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
                    unit_id=f"{relative_path}#L{first}-L{last}-S{name}",
                    kind=UnitKind.SYMBOL,
                    text=body,
                    locator=Locator(
                        relative_path=relative_path,
                        line_start=first,
                        line_end=last,
                        symbol=name,
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


def _sql_structural_units(
    relative_path: str, text: str, start_order: int
) -> tuple[ContentUnit, ...] | None:
    """SQL/PLSQL top-level CREATE bloklarini bounded sembol parcasi yap."""
    masked = _mask_structural_literals(text)
    if masked is None:
        return None
    masked_lines = masked.splitlines(keepends=True)
    rebuilt: list[str] = []
    for line in masked_lines:
        marker = line.find("--")
        rebuilt.append(line if marker < 0 else line[:marker] + " " * (len(line) - marker))
    masked = "".join(rebuilt)
    matches = tuple(_SQL_DECLARATION.finditer(masked))
    if not matches:
        return None
    lines = text.splitlines()
    units: list[ContentUnit] = []
    cursor = 0

    def line_number(offset: int) -> int:
        return text.count("\n", 0, offset) + 1

    for match_index, match in enumerate(matches):
        start = match.start()
        end = matches[match_index + 1].start() if match_index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        first = line_number(start)
        last = line_number(end - 1)
        if cursor < first:
            units.extend(
                _line_units(
                    relative_path,
                    "\n".join(lines[cursor : first - 1]),
                    start_order + len(units),
                    first_line=cursor + 1,
                )
            )
        name = match.group("name").split(".")[-1]
        if len(body) <= MAX_CHUNK_CHARACTERS and estimate_tokens(body) <= MAX_CHUNK_TOKENS:
            units.append(
                ContentUnit(
                    unit_id=f"{relative_path}#L{first}-L{last}-S{name}",
                    kind=UnitKind.SYMBOL,
                    text=body,
                    locator=Locator(
                        relative_path=relative_path,
                        line_start=first,
                        line_end=last,
                        symbol=name,
                    ),
                    order=start_order + len(units),
                )
            )
        else:
            units.extend(
                _line_units(
                    relative_path,
                    body,
                    start_order + len(units),
                    first_line=first,
                )
            )
        cursor = last
    if cursor < len(lines):
        units.extend(
            _line_units(
                relative_path,
                "\n".join(lines[cursor:]),
                start_order + len(units),
                first_line=cursor + 1,
            )
        )
    return tuple(units) if units else None


def _markdown_units(
    relative_path: str, text: str, start_order: int
) -> tuple[ContentUnit, ...] | None:
    """Markdown baslik, paragraf, liste, tablo ve fenced code birimleri uretir."""
    lines = text.splitlines()
    if not lines:
        return ()
    units: list[ContentUnit] = []
    heading_stack: list[tuple[int, str]] = []

    def append(kind: UnitKind, body_lines: list[str], first: int, last: int) -> None:
        body = "\n".join(body_lines).strip()
        if not body:
            return
        locator = Locator(
            relative_path=relative_path,
            line_start=first,
            line_end=last,
            heading_path=tuple(title for _, title in heading_stack),
        )
        if len(body) <= MAX_CHUNK_CHARACTERS and estimate_tokens(body) <= MAX_CHUNK_TOKENS:
            units.append(
                ContentUnit(
                    unit_id=f"{relative_path}#L{first}-L{last}-{kind.value}",
                    kind=kind,
                    text=body,
                    locator=locator,
                    order=start_order + len(units),
                )
            )
            return
        units.extend(_line_units(relative_path, body, start_order + len(units), first_line=first))

    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        heading = _MARKDOWN_HEADING.match(line)
        if heading:
            level = len(heading.group("marks"))
            title = heading.group("title").strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            append(UnitKind.HEADING, [title], index + 1, index + 1)
            index += 1
            continue
        fence = _MARKDOWN_FENCE.match(line)
        if fence:
            first = index + 1
            body_lines = [line]
            marker = fence.group(1)[0]
            index += 1
            while index < len(lines):
                body_lines.append(lines[index])
                if lines[index].lstrip().startswith(marker * 3):
                    index += 1
                    break
                index += 1
            append(UnitKind.CODE, body_lines, first, index)
            continue
        if "|" in line and index + 1 < len(lines) and "|" in lines[index + 1]:
            first = index + 1
            body_lines = [line]
            index += 1
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                body_lines.append(lines[index])
                index += 1
            append(UnitKind.TABLE, body_lines, first, index)
            continue
        if _MARKDOWN_LIST.match(line):
            first = index + 1
            body_lines = [line]
            index += 1
            while index < len(lines) and (
                _MARKDOWN_LIST.match(lines[index]) or not lines[index].strip()
            ):
                if lines[index].strip():
                    body_lines.append(lines[index])
                index += 1
            append(UnitKind.LIST, body_lines, first, index)
            continue
        first = index + 1
        body_lines = [line]
        index += 1
        while index < len(lines):
            candidate = lines[index]
            if (
                not candidate.strip()
                or _MARKDOWN_HEADING.match(candidate)
                or _MARKDOWN_FENCE.match(candidate)
                or _MARKDOWN_LIST.match(candidate)
                or "|" in candidate
            ):
                break
            body_lines.append(candidate)
            index += 1
        append(UnitKind.PARAGRAPH, body_lines, first, index)
    return tuple(units)


def _xml_structural_units(
    relative_path: str, text: str, start_order: int
) -> tuple[ContentUnit, ...] | None:
    """XML'i DTD/entity olmadan bounded entry-path metadata birimlerine ayirir."""
    if _XML_UNSAFE_DECLARATION.search(text):
        raise PolicyViolation("XML DTD veya entity deklarasyonu reddedildi")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None

    pending: list[tuple[ET.Element, int]] = [(root, 1)]
    element_count = 0
    while pending:
        element, depth = pending.pop()
        element_count += 1
        if element_count > MAX_XML_ELEMENTS:
            raise PolicyViolation("XML element sayisi sinirini asiyor")
        if depth > MAX_XML_DEPTH:
            raise PolicyViolation("XML element derinligi sinirini asiyor")
        pending.extend((child, depth + 1) for child in reversed(list(element)))

    def tag_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    units: list[ContentUnit] = []

    def append(element: ET.Element, entry_path: str) -> None:
        body = ET.tostring(element, encoding="unicode").strip()
        if not body:
            return
        locator = Locator(relative_path=relative_path, entry_path=entry_path)
        if len(body) > MAX_CHUNK_CHARACTERS or estimate_tokens(body) > MAX_CHUNK_TOKENS:
            units.extend(_line_units(relative_path, body, start_order + len(units)))
            return
        units.append(
            ContentUnit(
                unit_id=f"{relative_path}#X{len(units)}",
                kind=UnitKind.METADATA,
                text=body,
                locator=locator,
                order=start_order + len(units),
            )
        )

    root_path = f"xml/{tag_name(root.tag)}"
    children = list(root)
    if not children:
        append(root, root_path)
    else:
        for index, child in enumerate(children, start=1):
            append(child, f"{root_path}/{tag_name(child.tag)}[{index}]")
    return tuple(units)


def _file_units(relative_path: str, text: str, start_order: int) -> tuple[ContentUnit, ...]:
    """Python/code/SQL/Markdown structural; belirsizde bounded line fallback."""
    suffix = PurePosixPath(relative_path).suffix.lower()
    if suffix == ".xml":
        structural = _xml_structural_units(relative_path, text, start_order)
        if structural is not None:
            return structural
    if suffix in _MARKDOWN_SUFFIXES:
        structural = _markdown_units(relative_path, text, start_order)
        if structural is not None:
            return structural
    if suffix in _SQL_STRUCTURAL_SUFFIXES:
        structural = _sql_structural_units(relative_path, text, start_order)
        if structural is not None:
            return structural
    if suffix in _STRUCTURAL_CODE_SUFFIXES:
        structural = _structural_code_units(relative_path, text, start_order)
        if structural is not None:
            return structural
    if suffix != ".py":
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
    for decision_item in discovery.eligibility_decisions:
        if decision_item.relative_path in decisions:
            raise ValidationFailed("Eligibility karar kumesi yineleniyor; plan reddedildi")
        decisions[decision_item.relative_path] = decision_item
    by_path: dict[str, DiscoveredFile] = {}
    for discovered in discovery.files:
        if discovered.relative_path in by_path:
            raise ValidationFailed("Kesif dosya kumesi tutarsiz; plan reddedildi")
        by_path[discovered.relative_path] = discovered
    inventory_by_path: dict[str, Any] = {}
    for inventory_item in discovery.inventory:
        if inventory_item.kind is FileKind.FILE and inventory_item.relative_path in by_path:
            inventory_by_path[inventory_item.relative_path] = inventory_item
    for path, discovered in by_path.items():
        decision = decisions.get(path)
        if decision is None or decision.outcome is not EligibilityOutcome.ELIGIBLE:
            raise ValidationFailed("Kesif dosyasi eligibility karari tasimiyor; plan reddedildi")
        entry = inventory_by_path.get(path)
        if entry is None or entry.eligibility is None:
            raise ValidationFailed("Eligible dosyanin envanter kaydi yok; plan reddedildi")
        if (
            entry.eligibility.outcome is not EligibilityOutcome.ELIGIBLE
            or entry.eligibility.reason is not None
            or entry.is_text != discovered.is_text
            or entry.size_bytes != discovered.size_bytes
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
    local_model_ref: str = LOCAL_E5_MODEL_REF,
    local_dimension: int = LOCAL_E5_DIMENSION,
    allowed_relative_paths: tuple[str, ...] | None = None,
    discovery_policy: DiscoveryPolicy | None = None,
    check_budget: Callable[[], None] | None = None,
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

    if (
        local_model_ref == LOCAL_E5_MODEL_REF
        and local_dimension != LOCAL_E5_DIMENSION
    ):
        raise ValidationFailed("Yerel E5 plan profili 384 boyut ister")
    root = source_root.resolve(strict=True)
    discovery = (
        discover(root, policy=discovery_policy)
        if check_budget is None
        else discover(root, policy=discovery_policy, check_budget=check_budget)
    )
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
        if check_budget is not None:
            check_budget()
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
        "schema": "zekam-project-source-manifest/v2",
        "project_id": str(project_id),
        "project_slug": project_slug,
        "source_identity_digest": source_identity_digest(
            project_id, source_revision, expected_tree_digest, discovery.inventory_digest
        ),
        "source_revision": source_revision,
        "tree_digest": expected_tree_digest,
        "inventory_digest": discovery.inventory_digest,
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
            "source_identity_digest": source_identity_digest(
                project_id, source_revision, expected_tree_digest, discovery.inventory_digest
            ),
            "tree_digest": expected_tree_digest,
            "source_revision": source_revision,
            "inventory_digest": discovery.inventory_digest,
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
    query_prefix, passage_prefix = embedding_prefixes_for_model(embedding_route.model_ref)
    embedding_profile = EmbeddingProfile(
        model_ref=embedding_route.model_ref,
        dimension=embedding_route.dimension,
        distance="cosine",
        query_prefix=query_prefix,
        passage_prefix=passage_prefix,
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
    if check_budget is not None:
        check_budget()
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
    batch_size: int = MAX_PROJECT_EMBED_BATCH_SIZE,
    cancel: threading.Event | None = None,
    deadline_monotonic: float | None = None,
    max_provider_calls: int | None = None,
) -> ProjectIndexResult:
    """Persist immutable CAS manifest and atomically commit all PostgreSQL index rows.

    CAS content is digest-addressed and may safely pre-exist the database transaction;
    a database failure can leave only an unreachable immutable object, never a partial
    active knowledge version.
    """

    moment = now or dt.datetime.now(dt.UTC)
    _assert_plan_source_identity(plan)
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
    if cancel is not None and not isinstance(cancel, threading.Event):
        raise ValidationFailed("Project embedding cancel typed olmali")
    if deadline_monotonic is not None and (
        type(deadline_monotonic) not in (int, float) or not math.isfinite(float(deadline_monotonic))
    ):
        raise ValidationFailed("Project embedding deadline gecersiz")
    if max_provider_calls is not None and (
        type(max_provider_calls) is not int or max_provider_calls < 1
    ):
        raise ValidationFailed("Project embedding provider cagrisi siniri gecersiz")

    # Provider work finishes before CAS/database mutation. A timeout, partial batch,
    # NaN or dimension drift therefore cannot leave a half-activated generation.
    vectors: dict[str, tuple[float, ...]] = {}
    provider_calls = 0
    provider_batch_limit = getattr(embedding_provider, "max_batch_size", batch_size)
    if type(provider_batch_limit) is not int or provider_batch_limit < 1:
        raise ValidationFailed("Embedding provider batch limiti gecersiz")
    effective_batch_size = min(batch_size, provider_batch_limit)
    for chunk_batch in _project_embedding_batches(
        plan.chunks, batch_size=effective_batch_size, model_id=plan.embedding_profile.model_ref
    ):
        _check_project_embedding_budget(cancel, deadline_monotonic)
        texts = tuple(chunk.text for chunk in chunk_batch)
        embedded = _embed_project_batch_with_retry(
            embedding_provider,
            texts,
            embedding_policy,
            cancel=cancel,
            deadline_monotonic=deadline_monotonic,
        )
        _check_project_embedding_budget(cancel, deadline_monotonic)
        verify_document_embedding_batch(texts, embedded, provider_profile)
        provider_calls += embedded.receipt.provider_call_count
        if max_provider_calls is not None and provider_calls > max_provider_calls:
            raise ProviderCallQuotaExceeded("Project embedding provider cagrisi kotasi asildi")
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
